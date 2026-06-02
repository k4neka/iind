"""MPS, production and purchasing planner + daily dispatcher.

Fixes vs previous versions:

* ASAP scheduling: production is scheduled as early as possible
  (current_day forward) so a 2-day-deadline order starts on day 0.
* Inventory-aware purchasing: before placing a new purchase the planner
  consults the live inventory table AND the unplaced rows already in
  purchase_plan, so we never over-order.
* W1 / W2 capacity respected: both warehouses are capped at
  WAREHOUSE_CAPACITY at every planning day.
* Purchase deduplication across repeated replan() calls.
"""
import math
from collections import defaultdict

from config import (BOM, SUPPLIERS, MAX_UNLOAD_PER_DAY,
                    WAREHOUSE_CAPACITY, PLANNING_HORIZON_DAYS,
                    BASELINE_STOCK)
from time_model import estimate_production_days
from database import (
    get_pending_order_lines, get_production_plan, get_purchase_plan,
    add_production_entry, add_purchase_entry,
    production_due_on, purchases_arriving_on,
    mark_production_dispatched, mark_purchase_placed,
    save_penalty_cost, get_inventory, update_inventory,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _empty_calendars(current_day: int):
    horizon = range(current_day, current_day + PLANNING_HORIZON_DAYS + 1)
    return (defaultdict(int, {d: 0 for d in horizon}),
            defaultdict(int, {d: 0 for d in horizon}))


def _existing_production_by_line():
    out = defaultdict(int)
    for r in get_production_plan():
        if r["order_line_id"] is None:
            continue
        out[r["order_line_id"]] += r["quantity"]
    return out


def _existing_purchases_unplaced():
    """{(arrival_day, material): qty} for purchase rows still pending
    (placed=FALSE). These are already committed."""
    out = defaultdict(int)
    for p in get_purchase_plan():
        if not p["placed"]:
            out[(p["arrival_day"], p["material"])] += p["quantity"]
    return out


def _current_raw_inventory():
    """Live stock in W1 according to the ERP inventory table."""
    stock = {"Wood": 0, "Metal": 0}
    for row in get_inventory():
        if row["piece_type"] in stock:
            stock[row["piece_type"]] = int(row["quantity"])
    return stock


def _pick_delivery_day(ddate, unload_occ, horizon_end):
    day = ddate
    while unload_occ[day] >= MAX_UNLOAD_PER_DAY:
        day += 1
        if day > horizon_end:
            return day
    return day


def _pick_production_day(delivery_day, w2_occ, current_day):
    """Earliest day in [current_day, delivery_day] that respects W2
    capacity for every day between prod_day and delivery_day."""
    for prod_day in range(current_day, delivery_day + 1):
        ok = all(w2_occ[d] < WAREHOUSE_CAPACITY
                 for d in range(prod_day, delivery_day + 1))
        if ok:
            return prod_day
    return None


def _w1_occupancy_from_purchases(current_day: int):
    """Project W1 occupancy by day across the horizon using purchases
    already in the plan but not yet consumed. The model is simple:
    arrivals add stock, but we don't yet know exact consumption days,
    so we cap any single day."""
    horizon_end = current_day + PLANNING_HORIZON_DAYS
    occ = defaultdict(int)
    for p in get_purchase_plan():
        if p["placed"]:
            continue
        d = max(p["arrival_day"], current_day)
        if d <= horizon_end:
            occ[d] += int(p["quantity"])
    return occ


# ---------------------------------------------------------------------------
# Supplier selection (cost optimisation)
# ---------------------------------------------------------------------------

def _choose_supplier(material: str, net_qty: int, days_slack: int):
    """Pick the cheapest *feasible* supplier for `net_qty` of `material`.

    Feasible = its lead time fits within `days_slack`. The comparison is
    on TOTAL batch cost (after rounding up to the supplier's minimum
    batch), not on raw unit price, so a cheap bulk supplier that forces a
    large min-batch is only chosen when it actually wins on total cost.

    Returns (supplier_name, info_dict, batch_qty, total_cost).
    """
    best = None
    for name, table in SUPPLIERS.items():
        info = table[material]
        if days_slack < info["lead"]:
            continue  # cannot arrive in time
        batch = math.ceil(net_qty / info["min"]) * info["min"]
        cost = batch * info["price"]
        # Tie-break on smaller batch (less overstock), then name.
        cand = (cost, batch, name, info)
        if best is None or cand[:2] < best[:2]:
            best = cand

    if best is None:
        # Nothing meets the deadline: fall back to the lowest-lead
        # supplier so the order is only as late as unavoidable.
        name = min(SUPPLIERS,
                   key=lambda s: SUPPLIERS[s][material]["lead"])
        info = SUPPLIERS[name][material]
        batch = math.ceil(net_qty / info["min"]) * info["min"]
        best = (batch * info["price"], batch, name, info)

    cost, batch, name, info = best
    return name, info, batch, cost


def _choose_supplier_cost_benefit(material, net_qty, current_day,
                                  prod_day, ddate, penalty_per_day,
                                  prod_days):
    """Pick the supplier minimising TOTAL cost = purchase + expected
    late-delivery penalty.

    A piece can only start production once its material has arrived, so
    each supplier's lead time shifts the estimated completion day
    (arrival + machine processing time). When that pushes completion past
    `ddate`, the resulting penalty is charged against that supplier. This
    is what lets the planner pay Supplier A's immediate-delivery premium
    only when it actually saves more penalty than it costs — and take
    Supplier B's cheaper price whenever the deadline still holds.

    Returns (supplier, info, batch, purchase_cost, exp_penalty, days_late).
    """
    best = None
    for name, table in SUPPLIERS.items():
        info = table[material]
        batch = math.ceil(net_qty / info["min"]) * info["min"]
        purchase = batch * info["price"]
        arrival_day = max(prod_day, current_day + info["lead"])
        completion_day = arrival_day + prod_days
        days_late = max(0, completion_day - ddate)
        penalty = days_late * penalty_per_day
        total = purchase + penalty
        cand = (total, purchase, batch, name, info, penalty, days_late)
        if best is None or cand[0] < best[0]:
            best = cand
    total, purchase, batch, name, info, penalty, days_late = best
    return name, info, batch, purchase, penalty, days_late


# ---------------------------------------------------------------------------
# Predictive pre-ordering
# ---------------------------------------------------------------------------

def ensure_baseline_stock(current_day: int):
    """Keep a baseline raw-material buffer in W1 regardless of orders.

    Reactive purchasing only fires once a client order exists, which adds
    a full supplier lead time before production can start. This pre-orders
    wood and metal up to BASELINE_STOCK so a buffer is always on hand.

    Counts live inventory PLUS purchases already in flight (placed=FALSE)
    so repeated calls don't stack duplicate buffer orders. Tops up any
    deficit from the cheapest supplier.
    """
    stock = _current_raw_inventory()
    inflight = defaultdict(int)
    for p in get_purchase_plan():
        if not p["placed"] and p["material"] in BASELINE_STOCK:
            inflight[p["material"]] += int(p["quantity"])

    for material, target in BASELINE_STOCK.items():
        have = stock.get(material, 0) + inflight[material]
        deficit = target - have
        if deficit <= 0:
            continue

        # Generous slack so the cheap bulk supplier is eligible: a buffer
        # has no hard deadline, we just want it cheap.
        supplier, info, batch, cost = _choose_supplier(
            material, deficit, days_slack=PLANNING_HORIZON_DAYS)
        arrival_day = current_day + info["lead"]
        order_day = current_day
        add_purchase_entry(order_day, arrival_day, supplier,
                           material, batch, cost)
        print(f"[plan] pre-order baseline: {batch} {material} from "
              f"{supplier} (arrives day {arrival_day}, cost {cost:.2f}€, "
              f"deficit was {deficit})")


# ---------------------------------------------------------------------------
# Main planner
# ---------------------------------------------------------------------------

def replan(current_day: int):
    # Predictive buffer first, so it runs even with no client orders.
    ensure_baseline_stock(current_day)

    pending = get_pending_order_lines()
    if not pending:
        return

    pending.sort(key=lambda x: x["ddate"])
    w2_occ, unload_occ = _empty_calendars(current_day)
    horizon_end = current_day + PLANNING_HORIZON_DAYS

    already_scheduled = _existing_production_by_line()
    already_purchased = _existing_purchases_unplaced()
    raw_stock = _current_raw_inventory()
    w1_proj = _w1_occupancy_from_purchases(current_day)

    # Track raw material we mentally "consume" against current stock
    # while planning, so two pieces planned in the same call don't both
    # claim the same wood log.
    stock_remaining = dict(raw_stock)

    demand: dict[tuple, int] = defaultdict(int)
    # Per-(prod_day, material) deadline/penalty/production-time context so
    # the second pass can run the cost-benefit supplier decision.
    demand_meta: dict[tuple, dict] = {}
    prod_entries: list[tuple] = []

    # -----------------------------------------------------------------
    # First pass: schedule each piece ASAP and accumulate gross demand.
    # -----------------------------------------------------------------
    for line in pending:
        line_id = line["id"]
        piece = line["piece_type"]
        remaining = (line["quantity"] - line["produced"]
                     - already_scheduled.get(line_id, 0))
        ddate = max(line["ddate"], current_day)
        penalty = float(line["penalty"])

        if remaining <= 0:
            continue
        if piece not in BOM:
            print(f"[plan] WARN: unknown piece type '{piece}'")
            continue
        bom = BOM[piece]

        for _ in range(remaining):
            delivery_day = _pick_delivery_day(ddate, unload_occ,
                                              horizon_end)
            if delivery_day > horizon_end:
                print(f"[plan] WARN: line {line_id} cannot fit unload "
                      f"schedule; piece will be late.")
                continue

            prod_day = _pick_production_day(delivery_day, w2_occ,
                                            current_day)
            if prod_day is None:
                print(f"[plan] WARN: line {line_id} cannot be produced "
                      f"in horizon; retry next day.")
                continue

            unload_occ[delivery_day] += 1
            for d in range(prod_day, delivery_day + 1):
                w2_occ[d] += 1

            # Charge against existing W1 stock first.
            for material, qty_needed in bom.items():
                if qty_needed <= 0:
                    continue
                from_stock = min(stock_remaining.get(material, 0),
                                 qty_needed)
                stock_remaining[material] = (
                    stock_remaining.get(material, 0) - from_stock
                )
                net_needed = qty_needed - from_stock
                if net_needed > 0:
                    key = (prod_day, material)
                    demand[key] += net_needed
                    meta = demand_meta.setdefault(
                        key, {"ddate": ddate, "penalty": 0.0,
                              "prod_days": 0})
                    meta["ddate"] = min(meta["ddate"], ddate)
                    meta["penalty"] = max(meta["penalty"], penalty)
                    meta["prod_days"] = max(
                        meta["prod_days"],
                        estimate_production_days(piece))

            prod_entries.append((prod_day, piece, line_id))

            if delivery_day > ddate:
                days_late = delivery_day - ddate
                penalty_cost = days_late * penalty
                print(f"[plan] line {line_id}: late {days_late} day(s),"
                      f" penalty = {penalty_cost:.2f} €")
                save_penalty_cost(line_id, delivery_day, penalty_cost)

    # -----------------------------------------------------------------
    # Second pass: place purchase orders only for *net* unmet demand,
    # subtracting both stock-on-hand and purchases already in flight.
    # -----------------------------------------------------------------
    for (prod_day, material), gross_qty in sorted(demand.items()):
        key = (prod_day, material)
        already_for_key = already_purchased.get(key, 0)
        net_qty = max(0, gross_qty - already_for_key)

        if net_qty <= 0:
            print(f"[plan] skip purchase: {material} day {prod_day} "
                  f"demand={gross_qty} "
                  f"already_in_flight={already_for_key}")
            continue

        # Respect W1 capacity at arrival day.
        if w1_proj[prod_day] + net_qty > WAREHOUSE_CAPACITY:
            print(f"[plan] WARN: W1 cap exceeded on day {prod_day} "
                  f"for {material}; deferring purchase.")
            # Try next available day with room.
            shifted = prod_day
            while (shifted <= horizon_end
                   and w1_proj[shifted] + net_qty > WAREHOUSE_CAPACITY):
                shifted += 1
            if shifted > horizon_end:
                print(f"[plan] ERROR: no W1 capacity in horizon for "
                      f"{material} ({net_qty} units).")
                continue
            prod_day = shifted

        meta = demand_meta.get((prod_day, material))
        if meta:
            (supplier, info, batch, cost,
             exp_penalty, days_late) = _choose_supplier_cost_benefit(
                material, net_qty, current_day, prod_day,
                meta["ddate"], meta["penalty"], meta["prod_days"])
        else:
            days_slack = prod_day - current_day
            supplier, info, batch, cost = _choose_supplier(
                material, net_qty, days_slack)
            exp_penalty, days_late = 0.0, 0

        arrival_day = max(prod_day, current_day + info["lead"])
        order_day = max(current_day, arrival_day - info["lead"])
        add_purchase_entry(order_day, arrival_day, supplier,
                           material, batch, cost)

        already_purchased[key] = already_for_key + batch
        w1_proj[arrival_day] += batch
        print(f"[plan] purchase: {batch} {material} from {supplier} "
              f"(order day {order_day}, arrives day {arrival_day}, "
              f"cost {cost:.2f}€, net_demand={net_qty}; "
              f"cost-benefit: exp_penalty={exp_penalty:.2f}€, "
              f"days_late={days_late})")

    # -----------------------------------------------------------------
    # Commit production entries.
    # -----------------------------------------------------------------
    for prod_day, piece, line_id in prod_entries:
        add_production_entry(prod_day, piece, 1, line_id)

    print(f"[plan] replan done: {len(prod_entries)} pieces scheduled, "
          f"{len(demand)} material demands evaluated, "
          f"stock_at_start=wood:{raw_stock['Wood']} "
          f"metal:{raw_stock['Metal']}")


# ---------------------------------------------------------------------------
# Daily dispatcher
# ---------------------------------------------------------------------------

def dispatch_today(current_day: int, mqtt_bridge):
    rows = production_due_on(current_day)
    if rows:
        total = sum(r["quantity"] for r in rows)
        if total > MAX_UNLOAD_PER_DAY:
            print(f"[dispatch] WARN: production on day {current_day} "
                  f"totals {total} pieces; truncating.")
            kept, accum = [], 0
            for r in rows:
                if accum + r["quantity"] <= MAX_UNLOAD_PER_DAY:
                    kept.append(r)
                    accum += r["quantity"]
                else:
                    remaining = MAX_UNLOAD_PER_DAY - accum
                    if remaining > 0:
                        rc = dict(r)
                        rc["quantity"] = remaining
                        kept.append(rc)
                    break
            rows = kept

        items = [
            {"production_plan_id": r["id"],
             "order_line_id": r["order_line_id"],
             "piece_type": r["piece_type"],
             "quantity": r["quantity"]}
            for r in rows
        ]
        mqtt_bridge.send_production_order(current_day, items)
        mqtt_bridge.send_delivery_order(current_day, items)
        mark_production_dispatched([r["id"] for r in rows])
        print(f"[dispatch] day {current_day}: dispatched "
              f"{sum(r['quantity'] for r in rows)} pieces to MES")

    arrivals = purchases_arriving_on(current_day)
    for p in arrivals:
        # Raw material physically lands in W1 today: reflect it in the
        # inventory table so the ERP DB stops reading 0.
        update_inventory(p["material"], int(p["quantity"]))
        print(f"[dispatch] day {current_day}: sending material_load "
              f"{p['quantity']} {p['material']} to MES "
              f"(W1 inventory updated)")
        mqtt_bridge.send_material_load_command(p["material"],
                                               p["quantity"])
    if arrivals:
        mark_purchase_placed([p["id"] for p in arrivals])