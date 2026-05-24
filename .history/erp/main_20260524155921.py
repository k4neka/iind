"""ERP entry point: starts the database, clock, TCP server, MQTT bridge,
planner and daily dispatcher."""
import threading

from database import init_db, add_purchase_entry, get_state, set_state
from sim_clock import SimClock
from tcp_server import OrderServer
from mqtt_client import MQTTBridge
from planner import replan, dispatch_today


def _seed_debug_materials(current_day: int):
    # One-shot seed of raw material into the purchase plan so production
    # can start immediately on day 0. Skipped if a previous run already
    # did it (idempotent via sim_state.debug_seed_done).
    if get_state("debug_seed_done") == "1":
        print("[debug-seed] already done, skipping.")
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

    _seed_debug_materials(clock.current_day())
    replan(clock.current_day())

    def on_new_day(day: int):
        print(f"\n=== [sim] New day: {day} ===")
        replan(day)
        dispatch_today(day, mqtt)

    clock.add_day_listener(on_new_day)
    clock.start()

    def on_new_order(day: int):
        # New client orders may have created production entries scheduled
        # for TODAY (e.g. a same-day urgent order). Replan and immediately
        # dispatch anything due today so we don't wait for the next day.
        replan(day)
        dispatch_today(day, mqtt)

    server = OrderServer(clock, on_new_order)
    server.start()

    dispatch_today(clock.current_day(), mqtt)

    _stop = threading.Event()
    try:
        _stop.wait()
    except KeyboardInterrupt:
        print("\n[erp] shutting down.")


if __name__ == "__main__":
    main()