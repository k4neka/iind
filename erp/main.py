"""ERP entry point: starts the database, clock, TCP server, MQTT bridge,
planner and daily dispatcher."""
import threading
import time

from database import init_db, add_purchase_entry, get_state, set_state
from sim_clock import SimClock
from tcp_server import OrderServer
from mqtt_client import MQTTBridge
from planner import replan, dispatch_today


def _seed_debug_materials(current_day: int):
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


def _wait_mqtt(mqtt: MQTTBridge, timeout: float = 5.0):
    # Wait until the broker accepts the connection so the first day-0
    # dispatch doesn't fly into the void.
    deadline = time.time() + timeout
    while not mqtt.connected and time.time() < deadline:
        time.sleep(0.1)
    if not mqtt.connected:
        print("[erp] WARN: MQTT not connected yet, "
              "first dispatch may be lost.")


def main():
    init_db()
    clock = SimClock()
    mqtt = MQTTBridge()
    mqtt.start()
    _wait_mqtt(mqtt)

    _seed_debug_materials(clock.current_day())
    replan(clock.current_day())

    def on_new_day(day: int):
        print(f"\n=== [sim] New day: {day} ===")
        replan(day)
        dispatch_today(day, mqtt)

    clock.add_day_listener(on_new_day)
    clock.start()

    def on_new_order(day: int):
        replan(day)
        dispatch_today(day, mqtt)

    server = OrderServer(clock, on_new_order)
    server.start()

    # Small grace period so the MES has time to subscribe to the topics
    # before we fire the first dispatch (otherwise QoS-1 deliveries with
    # no subscribers are simply dropped by the broker).
    time.sleep(2.0)
    dispatch_today(clock.current_day(), mqtt)

    _stop = threading.Event()
    try:
        _stop.wait()
    except KeyboardInterrupt:
        print("\n[erp] shutting down.")


if __name__ == "__main__":
    main()