"""MPS, production and purchasing planner + daily dispatcher.

Key behaviours:

* ASAP scheduling: production is targeted at current_day so a tight order
  starts immediately (material lead times are handled via purchase arrivals).
* Urgency-ordered: pending lines are scheduled by descending slack-based
  urgency; dispatch emits production orders in that order too.
* Stock-first: finished goods on hand fulfil orders before new production,
  guarded by stock_reservations against double-claiming.
* Inventory-aware purchasing with a rolling W1 (<= 32) projection.
* W2 is NOT tracked here — the MES is the authority on W2 occupancy.
"""
import math
from collections import defaultdict

from config import (BOM, SUPPLIERS, MAX_UNLOAD_PER_DAY,
                    WAREHOUSE_CAPACITY, PLANNING_HORIZON_DAYS,
                    BASELINE_STOCK, FINAL_PRODUCTS, PRODUCT_PRICES)
from time_model import expected_production_days
from database import (
    get_pending_order_lines, get_production_plan, get_purchase_plan,
    add_production_entry, add_purchase_entry,
    production_due_on, purchases_arriving_on,
    mark_production_dispatched, mark_purchase_placed,
    save_penalty_cost, get_inventory, update_inventory,
    reserve_stock, get_reserved, release_reservation,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _empty_unload_calendar(current_day: int):
    horizon = range(current_day, current_day + PLANNING_HORIZON_DAYS + 1)
    return defaultdict(int, {d: 0 for d in horizon})


def _existing_production_by_line():
    out = defaultdict(int)
    for r in get_production_plan():
        if r["order_line_id"] is None:
            continue
        out[r["order_line_id"]] += r["quantity"]
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


def _current_finished_inventory():
    """Live finished-goods stock in W2 according to the inventory table
    ({piece_type: qty} restricted to final products)."""
    stock = {}
    for row in get_inventory():
        if row["piece_type"] in FINAL_PRODUCTS:
            stock[row["piece_type"]] = int(row["quantity"])
    return stock


def _project_w1_stock(current_day: int, current_inv: dict):
    """Rolling projection of total W1 raw stock (Wood + Metal) per day across
    the horizon (Item 4):

        projected[d] = current raw inventory
                     + Σ purchase arrivals on days <= d
                     - Σ scheduled raw consumption on days <= d

    Arrivals = purchase_plan rows with placed=FALSE; consumption = BOM raw of
    undispatched production_plan rows (from_stock rows consume nothing). The
    planner caps any purchase against this so projected W1 stays <= 32 every
    day (fixes the purchases-only projection that could exceed 32)."""
    horizon_end = current_day + PLANNING_HORIZON_DAYS
    base = current_inv.get("Wood", 0) + current_inv.get("Metal", 0)
    stock = {d: base for d in range(current_day, horizon_end + 1)}

    for p in get_purchase_plan():
        if p["placed"]:
            continue
        d = max(int(p["arrival_day"]), current_day)
        for day in range(d, horizon_end + 1):
            stock[day] += int(p["quantity"])

    for prod in get_production_plan():
        if prod["dispatched"] or prod.get("from_stock"):
            continue
        raw = sum(BOM.get(prod["piece_type"], {}).values()) * int(prod["quantity"])
        d = max(int(prod["sim_day"]), current_day)
        for day in range(d, horizon_end + 1):
            stock[day] -= raw

    return stock


# ---------------------------------------------------------------------------
# Supplier selection (cost optimisation)
# ---------------------------------------------------------------------------

def _choose_supplier(material, net_qty, current_day, prod_day, ddate,
                     penalty_per_day, prod_days, piece_type):
    """The 'Financial Brain' (Artur Strategy):
    Calculates expected Net Profit for both suppliers and picks the winner.
    
    Formula: Profit = SalePrice - (MaterialCost + WasteCost + PenaltyRisk)
    """
    sale_price = PRODUCT_PRICES.get(piece_type, 0.0)
    best_supplier = None
    max_profit = -999999.0

    for name, table in SUPPLIERS.items():
        info = table[material]
        # 1. Material & Waste Cost
        # We must buy in multiples of 'min'.
        batch_qty = math.ceil(net_qty / info["min"]) * info["min"]
        purchase_cost = batch_qty * info["price"]
        
        # 2. Penalty Risk
        arrival_day = max(prod_day, current_day + info["lead"])
        completion_day = arrival_day + prod_days
        days_late = max(0, completion_day - ddate)
        penalty_cost = days_late * penalty_per_day
        
        # 3. Profit Calculation (for this specific batch)
        # Note: we only count the sale_price for the pieces we are actually making now.
        # batch_qty - net_qty is 'leftover' stock for future profit.
        expected_profit = (sale_price * (net_qty / max(1, sum(BOM[piece_type].values())))) - purchase_cost - penalty_cost
        
        if expected_profit > max_profit:
            max_profit = expected_profit
            best_supplier = (name, info, batch_qty, purchase_cost, penalty_cost, days_late)

    return best_supplier


# ---------------------------------------------------------------------------
# Predictive pre-ordering
# ---------------------------------------------------------------------------

def _pending_inbound(material: str, by_day=None) -> int:
    """Total raw `material` already on order (placed=FALSE), optionally only
    counting tranches arriving on/before `by_day`. Used to avoid re-ordering
    material a previous purchase (e.g. the baseline buffer) already covers
    (v3 Bug 5)."""
    total = 0
    for p in get_purchase_plan():
        if p["material"] != material or p["placed"]:
            continue
        if by_day is None or int(p["arrival_day"]) <= by_day:
            total += int(p["quantity"])
    return total


def _place_tranched(material, net_qty, current_day, horizon_end,
                    ddate, penalty, prod_days, w1_proj, piece_type=None, prefer_fast=False):
    """Order `net_qty` of `material` in W1-sized tranches (v3 Bug 2).

    A single oversized purchase (e.g. 36 wood) never fits W1 (cap 32) and the
    old code just deferred it forever -> deadlock. Instead we split it into
    min-batch tranches across days, each fitting the projected room, modelling
    each tranche as consumed ~prod_days after arrival so production drains W1
    and frees room for the next tranche. `w1_proj` (day -> projected stock) is
    updated in place so successive tranches/materials see the committed ones.

    `prefer_fast` picks the lowest-lead supplier (so the material lands as soon
    as possible) instead of the cost-benefit choice — used for the baseline
    buffer, whose whole purpose is instant availability."""
    if prefer_fast:
        name = min(SUPPLIERS, key=lambda s: SUPPLIERS[s][material]["lead"])
        info = SUPPLIERS[name][material]
        supplier = name
    else:
        # If no piece_type provided (e.g. baseline), use a generic RWW for profit calc
        pt = piece_type if piece_type else "RWW"
        supplier, info, _b, _c, _p, _l = _choose_supplier(
            material, net_qty, current_day, current_day, ddate, penalty,
            prod_days, pt)
    remaining = net_qty
    day = current_day + info["lead"]
    drain = max(1, int(prod_days))
    guard = 0
    while remaining > 0 and day <= horizon_end and guard < 500:
        guard += 1
        room = WAREHOUSE_CAPACITY - w1_proj.get(day, 0)
        if room < info["min"]:
            day += 1
            continue
        batches = min(room // info["min"], math.ceil(remaining / info["min"]))
        tranche = batches * info["min"]
        order_day = max(current_day, day - info["lead"])
        add_purchase_entry(order_day, day, supplier, material, tranche,
                           tranche * info["price"])
        for d in range(day, horizon_end + 1):                 # arrival raises
            w1_proj[d] = w1_proj.get(d, 0) + tranche
        for d in range(min(horizon_end, day + drain), horizon_end + 1):  # drain
            w1_proj[d] = w1_proj.get(d, 0) - tranche
        print(f"[plan] purchase: {tranche} {material} from {supplier} "
              f"(order day {order_day}, arrives day {day}, "
              f"cost {tranche * info['price']:.2f}€, remaining {remaining - tranche})")
        remaining -= tranche
        day += 1
    if remaining > 0:
        print(f"[plan] WARN: {material}: {remaining} units unplaced this "
              f"replan (W1 horizon full); will retry next replan")


def ensure_baseline_stock(current_day: int):
    """Keep a baseline raw-material buffer in W1 regardless of orders, so
    production can start the instant an order arrives. Counts live inventory
    PLUS purchases already in flight so repeated calls don't stack duplicates,
    and orders the deficit in W1-sized tranches (v3 Bug 2)."""
    stock = _current_raw_inventory()
    horizon_end = current_day + PLANNING_HORIZON_DAYS
    w1_proj = _project_w1_stock(current_day, stock)
    for material, target in BASELINE_STOCK.items():
        have = stock.get(material, 0) + _pending_inbound(material)
        deficit = target - have
        if deficit <= 0:
            continue
        print(f"[plan] baseline {material}: deficit {deficit}, ordering (fast)")
        # Buffer must be available ASAP, so order from the lowest-lead supplier
        # (so day-0 orders can start on day 0 instead of waiting for a slow
        # bulk delivery). Client-demand purchases still use the cost-benefit
        # supplier choice.
        _place_tranched(material, deficit, current_day, horizon_end,
                        ddate=horizon_end, penalty=0.0, prod_days=1,
                        w1_proj=w1_proj, prefer_fast=True)


# ---------------------------------------------------------------------------
# Main planner
# ---------------------------------------------------------------------------

def _urgency(line, current_day: int) -> float:
    """Scheduling priority = penalty / slack, where slack is the days left
    AFTER accounting for production time (Item 5). A tight, high-penalty order
    gets a large urgency and front-loads ahead of looser ones. Robust to a
    missing ddate/penalty (joined rows with no order line sort last)."""
    ddate = line.get("ddate")
    if ddate is None:
        return 0.0
    prod_days = expected_production_days(line["piece_type"])
    slack = max(0.0, (int(ddate) - current_day) - prod_days)
    return float(line.get("penalty") or 0.0) / max(0.1, slack)


def replan(current_day: int):
    # Predictive buffer first, so it runs even with no client orders.
    ensure_baseline_stock(current_day)

    pending = get_pending_order_lines()
    if not pending:
        return

    # Item 5: order by DESCENDING urgency (not just ddate) so a tight,
    # high-penalty order front-loads ahead of cheaper/looser ones.
    pending.sort(key=lambda x: _urgency(x, current_day), reverse=True)
    unload_occ = _empty_unload_calendar(current_day)
    horizon_end = current_day + PLANNING_HORIZON_DAYS

    already_scheduled = _existing_production_by_line()
    raw_stock = _current_raw_inventory()

    # Track raw material we mentally "consume" against current stock
    # while planning, so two pieces planned in the same call don't both
    # claim the same wood log.
    stock_remaining = dict(raw_stock)

    # Item 5/6: finished goods already in W2 fulfil orders before any new
    # production. Available = live inventory minus units already reserved for
    # other order lines (stock_reservations), so two clients can't double-claim
    # the same physical stock.
    finished = _current_finished_inventory()
    finished_remaining = {p: max(0, q - get_reserved(p))
                          for p, q in finished.items()}

    demand: dict[tuple, int] = defaultdict(int)
    # Per-(prod_day, material) deadline/penalty/production-time context so
    # the second pass can run the cost-benefit supplier decision.
    demand_meta: dict[tuple, dict] = {}
    prod_entries: list[tuple] = []   # (day, piece, line_id, from_stock)

    # -----------------------------------------------------------------
    # First pass: fulfil from finished stock, then schedule the rest ASAP
    # and accumulate gross raw demand.
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

        # Stock-first: deliver from finished goods on hand, no production.
        # Reserve them so a concurrent client cannot claim the same units.
        from_stock_qty = min(finished_remaining.get(piece, 0), remaining)
        scheduled_from_stock = 0
        for _ in range(from_stock_qty):
            delivery_day = _pick_delivery_day(ddate, unload_occ, horizon_end)
            if delivery_day > horizon_end:
                break
            unload_occ[delivery_day] += 1
            prod_entries.append((delivery_day, piece, line_id, True))
            scheduled_from_stock += 1
        if scheduled_from_stock > 0:
            reserve_stock(line_id, piece, scheduled_from_stock)
            finished_remaining[piece] -= scheduled_from_stock
            remaining -= scheduled_from_stock
            print(f"[plan] line {line_id}: {scheduled_from_stock} {piece} "
                  f"fulfilled+reserved from finished stock (no production)")

        # Production is targeted at current_day (ASAP); material lead times are
        # absorbed by purchase arrivals. W2 occupancy is NOT tracked here.
        prod_day = current_day
        for _ in range(remaining):
            delivery_day = _pick_delivery_day(ddate, unload_occ,
                                              horizon_end)
            if delivery_day > horizon_end:
                print(f"[plan] WARN: line {line_id} cannot fit unload "
                      f"schedule; piece will be late.")
                continue

            unload_occ[delivery_day] += 1

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
                        expected_production_days(piece))

            prod_entries.append((prod_day, piece, line_id, False))

            if delivery_day > ddate:
                days_late = delivery_day - ddate
                penalty_cost = days_late * penalty
                print(f"[plan] line {line_id}: late {days_late} day(s),"
                      f" penalty = {penalty_cost:.2f} €")
                save_penalty_cost(line_id, delivery_day, penalty_cost)

    # -----------------------------------------------------------------
    # Second pass: aggregate raw demand per material, subtract material
    # already inbound (incl. the baseline buffer, v3 Bug 5), and order the
    # shortfall in W1-sized tranches (v3 Bug 2). Production days are absorbed
    # by tranche arrival timing + the dispatch-time material gate (Bug 3/8A).
    # -----------------------------------------------------------------
    mat_demand: dict[str, int] = defaultdict(int)
    mat_meta: dict[str, dict] = {}
    material_to_piece: dict[str, str] = {} # Fix: track piece type for financial calc

    for (prod_day, material), qty in demand.items():
        mat_demand[material] += qty
        dm = demand_meta.get((prod_day, material), {})
        m = mat_meta.setdefault(material, {"ddate": horizon_end,
                                           "penalty": 0.0, "prod_days": 1})
        m["ddate"] = min(m["ddate"], dm.get("ddate", horizon_end))
        m["penalty"] = max(m["penalty"], dm.get("penalty", 0.0))
        m["prod_days"] = max(m["prod_days"], dm.get("prod_days", 1))
        # Find which piece triggered this material demand
        for pt, bom in BOM.items():
            if material in bom: material_to_piece[material] = pt

    w1_proj = _project_w1_stock(current_day, raw_stock)
    for material, gross_qty in sorted(mat_demand.items()):
        meta = mat_meta[material]
        # Bug 5: material already on order that arrives by the deadline covers
        # part (or all) of the demand -> only order the shortfall.
        inbound = _pending_inbound(material, by_day=meta["ddate"])
        net_qty = max(0, gross_qty - inbound)
        if net_qty <= 0:
            print(f"[plan] skip purchase: {material} demand={gross_qty} "
                  f"covered by {inbound} already inbound (by day {meta['ddate']})")
            continue
        _place_tranched(material, net_qty, current_day, horizon_end,
                        meta["ddate"], meta["penalty"], meta["prod_days"],
                        w1_proj, piece_type=material_to_piece.get(material, "RWW"))

    # -----------------------------------------------------------------
    # Commit production entries (from_stock rows deliver without producing).
    # Each insert is its own transaction; guard per-entry so a single bad
    # row (e.g. a line removed concurrently) cannot abort the whole replan.
    # -----------------------------------------------------------------
    committed = 0
    for prod_day, piece, line_id, from_stock in prod_entries:
        try:
            add_production_entry(prod_day, piece, 1, line_id,
                                 from_stock=from_stock)
            committed += 1
        except Exception as e:
            print(f"[plan] skip scheduling line {line_id} ({piece}): {e}")

    n_stock = sum(1 for e in prod_entries if e[3])
    print(f"[plan] replan done: {committed}/{len(prod_entries)} pieces scheduled "
          f"({n_stock} from finished stock), "
          f"{len(demand)} material demands evaluated, "
          f"stock_at_start=wood:{raw_stock['Wood']} "
          f"metal:{raw_stock['Metal']}")


# ---------------------------------------------------------------------------
# Daily dispatcher
# ---------------------------------------------------------------------------

def dispatch_today(current_day: int, mqtt_bridge):
    # --- 1. Raw-material arrivals FIRST, so today's production can use them.
    arrivals = purchases_arriving_on(current_day)
    for p in arrivals:
        # Inventory is only credited when material PHYSICALLY arrives (v3
        # Bug 4), not when the purchase was planned.
        update_inventory(p["material"], int(p["quantity"]))
        print(f"[dispatch] day {current_day}: sending material_load "
              f"{p['quantity']} {p['material']} to MES (W1 inventory updated)")
        mqtt_bridge.send_material_load_command(p["material"],
                                               p["quantity"])
    if arrivals:
        mark_purchase_placed([p["id"] for p in arrivals])

    # --- 2. Dispatch production, most-urgent-first, but ONLY pieces whose BOM
    #        is covered by raw on the floor (v3 Bug 8A). This stops the MES
    #        queue bloating with pieces it cannot start. Raw is consumed at
    #        dispatch (mirrors the MES W1 drain); finished stock is drawn at
    #        delivery. Production scheduled earlier but still uncovered stays
    #        eligible (production_due_on uses sim_day <= day, Bug 3).
    rows = production_due_on(current_day)
    if not rows:
        return
    rows = sorted(rows, key=lambda r: _urgency(r, current_day), reverse=True)

    avail = _current_raw_inventory()          # {Wood, Metal}
    chosen = []
    for r in rows:
        if len(chosen) >= MAX_UNLOAD_PER_DAY:
            break
        if r.get("from_stock"):               # finished goods: no raw needed
            chosen.append(r)
            continue
        bom = BOM.get(r["piece_type"], {})
        if all(avail.get(m, 0) >= q for m, q in bom.items() if q > 0):
            for m, q in bom.items():
                if q > 0:
                    avail[m] = avail.get(m, 0) - q
            chosen.append(r)
        # else: not enough raw -> leave undispatched, retry a later day.

    if not chosen:
        waiting = len(rows)
        print(f"[dispatch] day {current_day}: nothing dispatchable yet "
              f"({waiting} pending, waiting on material)")
        return

    def _item(r):
        return {"production_plan_id": r["id"],
                "order_line_id": r["order_line_id"],
                "piece_type": r["piece_type"],
                "quantity": r["quantity"]}

    def _deliver_item(r):
        # Delivery items carry the FULL line context so the MES unloader can
        # group a whole client order onto its dock(s): the line's TOTAL
        # quantity (not just this slice), its due-date, and the client.
        d = _item(r)
        d["line_quantity"] = r.get("line_quantity")
        d["ddate"]         = r.get("ddate")
        d["client_order_id"] = r.get("client_order_id")
        d["client_name"]   = r.get("client_name")
        return d

    prod_items = [_item(r) for r in chosen if not r.get("from_stock")]
    deliver_items = [_deliver_item(r) for r in chosen]

    if prod_items:
        mqtt_bridge.send_production_order(current_day, prod_items)
    mqtt_bridge.send_delivery_order(current_day, deliver_items)

    for r in chosen:
        if not r.get("from_stock"):
            # Raw consumed by the production we just dispatched.
            for m, q in BOM.get(r["piece_type"], {}).items():
                if q > 0:
                    update_inventory(m, -q)
        # Finished good leaves W2 on delivery; produced pieces net to 0 once
        # their COMPLETED (+1) arrives, from_stock draws a real surplus.
        update_inventory(r["piece_type"], -int(r["quantity"]))
        if r.get("from_stock") and r.get("order_line_id") is not None:
            release_reservation(r["order_line_id"])

    mark_production_dispatched([r["id"] for r in chosen])
    n_stock = sum(1 for r in chosen if r.get("from_stock"))
    print(f"[dispatch] day {current_day}: dispatched {len(chosen)} pieces "
          f"to MES ({n_stock} from stock), {len(rows) - len(chosen)} held "
          f"for material")
