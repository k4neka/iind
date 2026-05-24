"""ERP entry point: starts the database, clock, TCP server, MQTT bridge,
planner and daily dispatcher."""
import threading

from database import init_db, add_purchase_entry, get_state, set_state
from sim_clock import SimClock
from tcp_server import OrderServer
from mqtt_client import MQTTBridge
from planner import replan, dispatch_today


# ============================================================================
# DEBUG: seed inicial de matéria-prima
# ----------------------------------------------------------------------------
# Adiciona 10 Wood + 10 Metal ao SupplierA (lead=0) com arrival_day = hoje.
# Será despachada via MQTT pelo dispatch_today() do dia atual.
#
# Para DESLIGAR: comenta a chamada a _seed_debug_materials() em main().
# Para forçar de novo (já foi feito numa run anterior):
#   DELETE FROM db_erp.sim_state WHERE key = 'debug_seed_done';
# ============================================================================
def _seed_debug_materials(current_day: int):
    """Mete 10 Wood + 10 Metal na fila de compras, uma única vez por reset."""
    if get_state("debug_seed_done") == "1":
        print("[debug-seed] já feito anteriormente, a saltar.")
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
# ============================================================================


def main():
    # 1) Persistence
    init_db()

    # 2) Simulation clock
    clock = SimClock()

    # 3) MQTT bridge to the MES
    mqtt = MQTTBridge()
    mqtt.start()

    # ----------------------------------------------------------------------
    # DEBUG: comentar para DESLIGAR o seed de 10 Wood + 10 Metal
    # ----------------------------------------------------------------------
    _seed_debug_materials(clock.current_day())
    # ----------------------------------------------------------------------

    # 4) Recompute plan from pending DB state
    replan(clock.current_day())

    # 5) Hook planner + dispatcher to each new sim day
    def on_new_day(day: int):
        print(f"\n=== [sim] New day: {day} ===")
        replan(day)
        dispatch_today(day, mqtt)

    clock.add_day_listener(on_new_day)
    clock.start()

    # 6) TCP server (re-plans on every accepted order)
    def on_new_order():
        replan(clock.current_day())

    server = OrderServer(clock, on_new_order)
    server.start()

    # 7) Dispatch for current day (also flushes the debug seed)
    dispatch_today(clock.current_day(), mqtt)

    # 8) Keep main thread alive
    _stop = threading.Event()
    try:
        _stop.wait()
    except KeyboardInterrupt:
        print("\n[erp] shutting down.")


if __name__ == "__main__":
    main()