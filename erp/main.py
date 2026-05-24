"""ERP entry point: starts the database, clock, TCP server, MQTT bridge,
planner and daily dispatcher.

No day-0 dispatch. The first material_load is sent on the day the new
purchase_plan rows have arrival_day == current_day, which the planner
now anchors to the production day, not the order day. Result: no raw
material is pre-loaded into W1 before production really needs it.
"""
import threading
import time

from database import init_db
from sim_clock import SimClock
from tcp_server import OrderServer
from mqtt_client import MQTTBridge
from planner import replan, dispatch_today


def _wait_mqtt(mqtt: MQTTBridge, timeout: float = 5.0):
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

    # Compute the plan but do NOT dispatch on startup. The first
    # dispatch happens via on_new_day (or on_new_order if a client
    # places an order whose prod_day is today).
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

    _stop = threading.Event()
    try:
        _stop.wait()
    except KeyboardInterrupt:
        print("\n[erp] shutting down.")


if __name__ == "__main__":
    main()