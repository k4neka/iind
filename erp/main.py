"""ERP entry point: starts the database, clock, TCP server, MQTT bridge,
planner and daily dispatcher."""
import threading

from database import init_db
from sim_clock import SimClock
from tcp_server import OrderServer
from mqtt_client import MQTTBridge
from planner import replan, dispatch_today


def main():
    # 1) Persistence (crash recovery happens automatically: schema + data
    #    survive reboots, planner reads pending orders on startup).
    init_db()

    # 2) Simulation clock (1 sim day = 60 s, persistent base day)
    clock = SimClock()

    # 3) MQTT bridge to the MES
    mqtt = MQTTBridge()
    mqtt.start()

    # 4) On startup, recompute the plan from whatever is still pending in DB
    replan(clock.current_day())

    # 5) Hook the planner + dispatcher to each new simulation day
    def on_new_day(day: int):
        print(f"\n=== [sim] New day: {day} ===")
        replan(day)
        dispatch_today(day, mqtt)

    clock.add_day_listener(on_new_day)
    clock.start()

    # 6) TCP server on port 6666 (re-plans on every accepted order)
    def on_new_order():
        replan(clock.current_day())

    server = OrderServer(clock, on_new_order)
    server.start()

    # 7) Also dispatch immediately for the current day (in case the ERP was
    #    restarted mid-day after a crash)
    dispatch_today(clock.current_day(), mqtt)


if __name__ == "__main__":
    main()