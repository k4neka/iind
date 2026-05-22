"""MPS, production and purchasing planner + daily dispatcher.

Strategy:
  * Each order line is planned piece by piece, in increasing DDate order
    (most urgent first).
  * Virtual occupancy calendars are kept for the planning horizon:
        - W1 (raw materials, capacity 32)
        - W2 (finished products, capacity 32)
        - Unloading docks (max 30 dispatches per day)
  * For each piece:
        1. Pick a delivery day. If the unload dock is already full on the
           original DDate, push delivery forward (and accept the penalty
           defined in the order).
        2. Pick a production day. Ideally equal to delivery day (no W2
           occupation). If W2 is full, try earlier days down to today.
        3. Plan raw-material purchase. If W1 has room for a big batch,
           use the cheaper SupplierB (min 12 wood / 8 metal). Otherwise
           fall back to SupplierA (min 2 / 4).
  * Purchases are aggregated per (arrival_day, material) so we don't
    place one minimum-order per piece.
"""
import math
from collections import defaultdict

from config import (BOM, SUPPLIERS, MAX_UNLOAD_PER_DAY,
                    WAREHOUSE_CAPACITY, PLANNING_HORIZON_DAYS)
from database import (
    get_pending_order_lines, clear_undispatched_plans,
    add_production_entry, add_purchase_entry,
    production_due_on, purchases_due_on,
    mark_production_dispatched, mark_purchase_placed,
    save_penalty_cost, save_raw_cost,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _occupancy(current_day: int):
    """Return three empty occupancy calendars for the planning horizon."""
    horizon = range(current_day, current_day + PLANNING_HORIZON_DAYS + 1)
    return (
        defaultdict(int, {d: 0 for d in horizon}),  # W1
        defaultdict(int, {d: 0 for d in horizon}),  # W2
        defaultdict(int, {d: 0 for d in horizon}),  # unload
    )


def _pick_delivery_day(ddate: int, unload_occ: dict, horizon_end: int) -> int:
    """Find the earliest day >= ddate with an available unload slot."""
    day = ddate
    while unload_occ[day] >= MAX_UNLOAD_PER_DAY:
        day += 1
        if day > horizon_end:
            return day  # planner will warn; piece will be late
    return day


def _pick_production_day(delivery_day: int, w2_occ: dict,
                         current_day: int) -> int | None:
    """Find a production day that lets the piece sit in W2 until delivery.

    The piece occupies one W2 slot from prod_day .. delivery_day (inclusive).
    Returns None if no feasible day exists (W2 fully booked).
    """
    prod_day = delivery_day
    while prod_day >= current_day:
        ok = all(w2_occ[d] < WAREHOUSE_CAPACITY
                 for d in range(prod_day, delivery_day + 1))
        if ok:
            return prod_day
        prod_day -= 1
    return None


def _choose_supplier(material: str, prod_day: int, current_day: int,
                     w1_occ: dict) -> tuple[str, dict, int]:
    """Pick supplier + arrival day, considering W1 free space.

    Returns (supplier_name, supplier_info_dict, arrival_day).
    Prefers cheaper SupplierB if both lead time AND warehouse fit; otherwise
    falls back to SupplierA.
    """
    info_a = SUPPLIERS["SupplierA"][material]
    info_b = SUPPLIERS["SupplierB"][material]

    arrival_b = prod_day - info_b["lead"]
    arrival_a = prod_day - info_a["lead"]

    # Check W1 capacity for SupplierB (large batch sits in W1 from arrival
    # day until prod_day).
    if arrival_b >= current_day:
        b_fits = all(
            w1_occ[d] + info_b["min"] <= WAREHOUSE_CAPACITY
            for d in range(arrival_b, prod_day + 1)
        )
        if b_fits:
            return "SupplierB", info_b, arrival_b

    # Fall back to SupplierA (smaller batch, possibly immediate delivery).
    arrival_a = max(arrival_a, current_day)
    return "SupplierA", info_a, arrival_a


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def replan(current_day: int):
    """Rebuild the undispatched production + purchase plans from scratch."""
    clear_undispatched_plans()

    pending = get_pending_order_lines()
    if not pending:
        return

    # Sort by urgency (earliest DDate first).
    pending.sort(key=lambda x: x["ddate"])

    w1_occ, w2_occ, unload_occ = _occupancy(current_day)
    horizon_end = current_day + PLANNING_HORIZON_DAYS

    # Aggregate raw-material purchases per (arrival_day, material, supplier).
    purchase_aggregate: dict[tuple[int, str, str], int] = defaultdict(int)
    # Track raw cost per order line so we can persist it later.
    raw_cost_per_line: dict[int, float] = defaultdict(float)

    for line in pending:
        line_id   = line["id"]
        piece     = line["piece_type"]
        remaining = line["quantity"] - line["produced"]
        ddate     = max(line["ddate"], current_day)  # clamp past-due dates to today
        penalty   = float(line["penalty"])

        if remaining <= 0:
            continue
        if piece not in BOM:
            print(f"[plan] WARN: unknown piece type '{piece}' on line {line_id}")
            continue

        bom = BOM[piece]  # {"Wood": x, "Metal": y}

        # Plan each piece individually so occupancy is enforced exactly.
        for _ in range(remaining):

            # --- (A) Delivery day ---
            delivery_day = _pick_delivery_day(ddate, unload_occ, horizon_end)
            if delivery_day > horizon_end:
                print(f"[plan] WARN: line {line_id} cannot fit unload schedule "
                      f"within horizon; piece will be late.")
                continue

            # --- (B) Production day ---
            prod_day = _pick_production_day(delivery_day, w2_occ, current_day)
            if prod_day is None:
                print(f"[plan] WARN: line {line_id} cannot be produced — "
                      f"no feasible production day found (delivery_day={delivery_day}, current_day={current_day}). Order is past-due; will retry next day.")
                continue

            # --- (C) Raw materials for this piece ---
            piece_feasible = True
            piece_raw_cost = 0.0
            chosen_purchases = []  # buffered until we commit the piece

            for material, qty_needed in bom.items():
                if qty_needed <= 0:
                    continue

                supplier, info, arrival_day = _choose_supplier(
                    material, prod_day, current_day, w1_occ
                )

                # Reserve W1 capacity for the units actually consumed by THIS
                # piece (qty_needed), from arrival until prod_day.
                if arrival_day < current_day:
                    arrival_day = current_day
                if any(w1_occ[d] + qty_needed > WAREHOUSE_CAPACITY
                       for d in range(arrival_day, prod_day + 1)):
                    print(f"[plan] WARN: line {line_id} — no W1 room for "
                          f"{material}; piece skipped.")
                    piece_feasible = False
                    break

                chosen_purchases.append(
                    (material, qty_needed, supplier, info, arrival_day)
                )
                piece_raw_cost += qty_needed * info["price"]

            if not piece_feasible:
                continue

            # --- Commit reservations for this piece ---
            unload_occ[delivery_day] += 1
            for d in range(prod_day, delivery_day + 1):
                w2_occ[d] += 1

            for material, qty_needed, supplier, info, arrival_day in chosen_purchases:
                for d in range(arrival_day, prod_day + 1):
                    w1_occ[d] += qty_needed
                # Aggregate purchases so we don't fire a min-batch per piece.
                purchase_aggregate[(arrival_day, material, supplier)] += qty_needed

            add_production_entry(prod_day, piece, 1, line_id)
            raw_cost_per_line[line_id] += piece_raw_cost

            # --- Penalty bookkeeping ---
            if delivery_day > ddate:
                days_late = delivery_day - ddate
                penalty_cost = days_late * penalty
                print(f"[plan] line {line_id}: delivered {days_late} day(s) "
                      f"late, penalty = {penalty_cost:.2f} €")
                save_penalty_cost(line_id, delivery_day, penalty_cost)

    # ---------------------------------------------------------------------
    # Materialise aggregated purchases, rounded up to supplier minimums.
    # ---------------------------------------------------------------------
    for (arrival_day, material, supplier), qty in purchase_aggregate.items():
        info     = SUPPLIERS[supplier][material]
        min_q    = info["min"]
        # Round up to the nearest multiple of the minimum order.
        batch    = math.ceil(qty / min_q) * min_q
        order_day = arrival_day - info["lead"]
        if order_day < current_day:
            order_day = current_day
            arrival_day = order_day + info["lead"]
        cost = batch * info["price"]
        add_purchase_entry(order_day, arrival_day, supplier,
                           material, batch, cost)

    # Persist raw-material costs per order line (informational).
    for line_id, raw_cost in raw_cost_per_line.items():
        if raw_cost > 0:
            save_raw_cost(line_id, current_day, raw_cost)


def dispatch_today(current_day: int, mqtt_bridge):
    """Once per simulation day, push the day's orders to the MES."""

    # ---- Production / delivery orders ----
    rows = production_due_on(current_day)
    if rows:
        total = sum(r["quantity"] for r in rows)
        if total > MAX_UNLOAD_PER_DAY:
            print(f"[dispatch] WARN: production on day {current_day} totals "
                  f"{total} pieces (>30). Truncating to {MAX_UNLOAD_PER_DAY}.")
            kept, accum = [], 0
            for r in rows:
                if accum + r["quantity"] <= MAX_UNLOAD_PER_DAY:
                    kept.append(r)
                    accum += r["quantity"]
                else:
                    remaining = MAX_UNLOAD_PER_DAY - accum
                    if remaining > 0:
                        r_clone = dict(r)
                        r_clone["quantity"] = remaining
                        kept.append(r_clone)
                    break
            rows = kept

        items = [
            {
                "production_plan_id": r["id"],
                "order_line_id":      r["order_line_id"],
                "piece_type":         r["piece_type"],
                "quantity":           r["quantity"],
            }
            for r in rows
        ]
        mqtt_bridge.send_production_order(current_day, items)
        mqtt_bridge.send_delivery_order(current_day, items)
        mark_production_dispatched([r["id"] for r in rows])

    # ---- Material-load commands (only on the actual arrival day) ----
    purchases = purchases_due_on(current_day)
    for p in purchases:
        if p["arrival_day"] == current_day:
            mqtt_bridge.send_material_load_command(
                p["material"], p["quantity"]
            )
    mark_purchase_placed([p["id"] for p in purchases])