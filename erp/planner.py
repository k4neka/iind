"""MPS, production and purchasing planner + daily dispatcher.

Two-day cadence: material is ordered to arrive on the EXACT day
production needs it (never earlier), so W1 doesn't fill up days before
demand. Supplier preference defaults to SupplierA (lead=0, smaller min
order). SupplierB is only chosen when the aggregate batch already
crosses its minimum order on its own, so bulk orders aren't placed for
small demand.
"""
import math
from collections import defaultdict

from config import (BOM, SUPPLIERS, MAX_UNLOAD_PER_DAY,
                    WAREHOUSE_CAPACITY, PLANNING_HORIZON_DAYS)
from database import (
    get_pending_order_lines, get_production_plan, get_purchase_plan,
    add_production_entry, add_purchase_entry,
    production_due_on, purchases_arriving_on,
    mark_production_dispatched, mark_purchase_placed,
    save_penalty_cost, save_raw_cost,
)


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


def _pick_delivery_day(ddate, unload_occ, horizon_end):
    day = ddate
    while unload_occ[day] >= MAX_UNLOAD_PER_DAY:
        day += 1
        if day > horizon_end:
            return day
    return day


def _pick_production_day(delivery_day, w2_occ, current_day):
    prod_day = delivery_day
    while prod_day >= current_day:
        ok = all(w2_occ[d] < WAREHOUSE_CAPACITY
                 for d in range(prod_day, delivery_day + 1))
        if ok:
            return prod_day
        prod_day -= 1
    return None


def replan(current_day: int):
    pending = get_pending_order_lines()
    if not pending:
        return

    pending.sort(key=lambda x: x["ddate"])
    w2_occ, unload_occ = _empty_calendars(current_day)
    horizon_end = current_day + PLANNING_HORIZON_DAYS

    already_scheduled = _existing_production_by_line()

    # First pass: schedule production day for every piece and aggregate
    # how much raw material each (prod_day, material) needs in total.
    demand = defaultdict(int)            # (prod_day, material) -> qty
    raw_cost_per_line = defaultdict(float)
    prod_entries = []                    # rows to insert later

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

            # Earliest valid prod day is the day after current (so the
            # material has time to arrive) unless ddate forces today.
            prod_day = _pick_production_day(delivery_day, w2_occ,
                                            max(current_day,
                                                current_day + 1))
            # Fall back to today only if the ddate is today already.
            if prod_day is None:
                prod_day = _pick_production_day(delivery_day, w2_occ,
                                                current_day)
            if prod_day is None:
                print(f"[plan] WARN: line {line_id} cannot be produced "
                      f"in horizon; retry next day.")
                continue

            unload_occ[delivery_day] += 1
            for d in range(prod_day, delivery_day + 1):
                w2_occ[d] += 1

            for material, qty_needed in bom.items():
                if qty_needed > 0:
                    demand[(prod_day, material)] += qty_needed

            prod_entries.append((prod_day, piece, line_id))

            if delivery_day > ddate:
                days_late = delivery_day - ddate
                penalty_cost = days_late * penalty
                print(f"[plan] line {line_id}: late {days_late} day(s), "
                      f"penalty = {penalty_cost:.2f} €")
                save_penalty_cost(line_id, delivery_day, penalty_cost)

    # Second pass: choose suppliers per (prod_day, material) aggregate.
    # SupplierA is the default. SupplierB is only used when the demand
    # already meets its minimum order on its own AND the lead time fits.
    for (prod_day, material), qty in demand.items():
        info_a = SUPPLIERS["SupplierA"][material]
        info_b = SUPPLIERS["SupplierB"][material]
        days_slack = prod_day - current_day

        use_b = (qty >= info_b["min"]) and (days_slack >= info_b["lead"])
        if use_b:
            supplier, info = "SupplierB", info_b
        else:
            supplier, info = "SupplierA", info_a

        min_q = info["min"]
        batch = math.ceil(qty / min_q) * min_q
        # Material MUST arrive on prod_day (no earlier) so W1 isn't
        # pre-filled days in advance.
        arrival_day = prod_day
        order_day = max(current_day, arrival_day - info["lead"])
        cost = batch * info["price"]
        add_purchase_entry(order_day, arrival_day, supplier,
                           material, batch, cost)

    # Commit production entries.
    for prod_day, piece, line_id in prod_entries:
        add_production_entry(prod_day, piece, 1, line_id)


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
                    kept.append(r); accum += r["quantity"]
                else:
                    remaining = MAX_UNLOAD_PER_DAY - accum
                    if remaining > 0:
                        rc = dict(r); rc["quantity"] = remaining
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

    # Material loads only when they actually arrive at W1.
    arrivals = purchases_arriving_on(current_day)
    for p in arrivals:
        mqtt_bridge.send_material_load_command(
            p["material"], p["quantity"]
        )
    mark_purchase_placed([p["id"] for p in arrivals])