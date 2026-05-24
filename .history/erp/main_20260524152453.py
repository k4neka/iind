"""ERP entry point: starts the database, clock, TCP server, MQTT bridge,
planner and daily dispatcher."""
import threading

from database import (init_db, add_purchase_entry, get_state, set_state,
                      get_pending_order_lines)
from sim_clock import SimClock
from tcp_server import OrderServer
from mqtt_client import MQTTBridge
from planner import replan, dispatch_today


def _seed_debug_materials(current_day: int):
    """One-shot seed: 10 Wood + 10 Metal via SupplierA on day 0.
    Only runs if there are no pending orders, otherwise it would double
    with quantities computed by replan()."""
    if get_state("debug_seed_done") == "1":
        print("[debug-seed] already done, skipping.")
        return
    if get_pending_order_lines():
        print("[debug-seed] pending orders exist, skipping seed.")
        set_state("debug_seed_done", "1")
        return

    from config import SUPPLIERS
    supplier = "SupplierA"
    seeds = [("Wood", 10), ("Metal", 10)]

    for material, qty in seeds:
        info = SUPPLIERS[supplier][material]
        arrival_day = current_day + info["lead"]
        cost = qty * info["price"]
        add_purchase_entry(
            order_day=current_day,
            arrival_day=arrival_day,
            supplier=supplier,
            material=material,
            qty=qty,
            cost=cost,
        )
        print(f"[debug-seed] +{qty}x {material} via {supplier} "
              f"(arrives day {arrival_day}, cost={cost}€)")

    set_state("debug_seed_done", "1")
    print("[debug-seed] done.")


def main():
    init_db()

    clock = SimClock()

    mqtt = MQTTBridge()
    mqtt.start()

    # Seed BEFORE replan so the 10+10 are guaranteed to flush on day 0
    # without being aggregated with any planner-generated purchases.
    _seed_debug_materials(clock.current_day())

    # Dispatch the seed immediately (day 0 purchases with arrival_day=0)
    dispatch_today(clock.current_day(), mqtt)

    # Now plan production based on any pending orders in DB
    replan(clock.current_day())

    def on_new_day(day: int):
        print(f"\n=== [sim] New day: {day} ===")
        replan(day)
        dispatch_today(day, mqtt)

    clock.add_day_listener(on_new_day)
    clock.start()

    def on_new_order():
        replan(clock.current_day())

    server = OrderServer(clock, on_new_order)
    server.start()

    _stop = threading.Event()
    try:
        _stop.wait()
    except KeyboardInterrupt:
        print("\n[erp] shutting down.")


if __name__ == "__main__":
    main()