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

    # Single serialised plan step. replan and dispatch are INDEPENDENT: if
    # replan fails (e.g. a transient DB/FK error), dispatch_today STILL runs so
    # already-scheduled production is sent to the MES once its material arrives
    # — the machines never stall just because one replan threw.
    plan_lock = threading.Lock()

    def do_plan(day: int):
        with plan_lock:
            try:
                replan(day)
            except Exception as e:
                print(f"[plan] replan error (continuing to dispatch): {e}")
            try:
                dispatch_today(day, mqtt)
            except Exception as e:
                print(f"[dispatch] error: {e}")

    # Compute the initial plan but do not dispatch on startup beyond what
    # do_plan does (no material has arrived yet).
    do_plan(clock.current_day())

    def on_new_day(day: int):
        print(f"\n=== [sim] New day: {day} ===")
        # The day that just ENDED is `day - 1`: tell the MES to discharge every
        # unloading dock that was loaded during it (pieces placed during the
        # day are unloaded automatically at end-of-day, PDF §2.4/§4.1). Done
        # BEFORE today's plan so the docks are clear for today's deliveries.
        if day > 0:
            try:
                mqtt.send_end_of_day(day - 1)
            except Exception as e:
                print(f"[erp] send_end_of_day({day - 1}) failed: {e}")
        do_plan(day)

    clock.add_day_listener(on_new_day)
    clock.start()

    # Client orders are handled by ONE coalescing worker thread, not a thread
    # per order: the TCP handler just signals (instant response, v3 Bug 1) and
    # the worker runs a single replan+dispatch, draining a burst of orders in
    # one pass. This removes the concurrent-replan race entirely.
    order_event = threading.Event()

    def order_worker():
        while True:
            order_event.wait()
            order_event.clear()
            do_plan(clock.current_day())

    threading.Thread(target=order_worker, daemon=True).start()

    def on_new_order(_day: int):
        order_event.set()

    server = OrderServer(clock, on_new_order)
    server.start()

    _stop = threading.Event()
    try:
        _stop.wait()
    except KeyboardInterrupt:
        print("\n[erp] shutting down.")


if __name__ == "__main__":
    main()