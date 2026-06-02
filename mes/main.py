"""MES entry point. Wires PLC + MQTT + DB + dispatcher + poller."""
import asyncio
import time

from config import DISPATCH_INTERVAL
from database import init_db
from opcua_client import PLCClient
from mqtt_client import MESMqtt
from poller import Poller
from cost_tracker import CostTracker
from dispatcher import Dispatcher, ComplexOrchestrator
from transfer_manager import TransferManager


async def main_async():
    init_db()

    plc = PLCClient()
    if not await plc.connect():
        print("[mes] CRITICAL: cannot connect to PLC. Exiting.")
        return

    loop = asyncio.get_running_loop()

    mqtt_bridge = MESMqtt(loop=loop, plc=plc)

    # Single worker that drains W2 -> W1 through the Transfer_Cell, used
    # both for sub-part staging and for returning finished pieces.
    transfers = TransferManager(plc)
    cost = CostTracker(
        mqtt_publish_status=mqtt_bridge.publish_status,
        on_completed=lambda row: transfers.enqueue(row["piece_type"]),
    )

    def on_warehouse_update(w1, w2):
        # PLC counter (unreliable for W1 but kept for W2 visibility).
        print(f"[wh] PLC W1={w1}/32  W2={w2}/32")

    poller = Poller(plc, cost, on_warehouse_update=on_warehouse_update)

    # Shared cell-lease registry between simple and complex production.
    busy_cells: set[int] = set()
    busy_lock = asyncio.Lock()
    dispatcher = Dispatcher(plc, mqtt_bridge,
                            busy_cells=busy_cells, busy_lock=busy_lock)
    orchestrator = ComplexOrchestrator(
        plc, mqtt_bridge,
        publish_status=mqtt_bridge.publish_status,
        transfer_manager=transfers,
        busy_cells=busy_cells, busy_lock=busy_lock,
    )

    mqtt_bridge.start()

    async def dispatcher_loop():
        # Aggressive 0.5 s tick: starts production as soon as one raw
        # piece is available in the locally-tracked W1.
        while True:
            try:
                await dispatcher.tick()
            except Exception as e:
                print(f"[disp] tick error: {e}")
            await asyncio.sleep(DISPATCH_INTERVAL)

    async def cost_sweep_loop():
        while True:
            cost.sweep_stuck(time.time())
            await asyncio.sleep(30)

    async def loader_ack_loop():
        while True:
            try:
                status = await plc.read_loader_status()
                if status == 3:
                    print("[mes] WARN: loader reported ERROR (status=3)")
                    await plc.clear_loader_exec()
            except Exception as e:
                print(f"[mes] loader_ack_loop error: {e}")
            await asyncio.sleep(0.5)

    async def loader_flush_loop():
        while True:
            try:
                await mqtt_bridge.flush_loader()
            except Exception as e:
                print(f"[mes] loader_flush_loop error: {e}")
            await asyncio.sleep(1.0)

    async def completion_loop():
        # Form A: consume the PLC completion buffer (g_Done_*). The PLC writes
        # one event per finished product into an empty slot; we mark it
        # COMPLETED, report to the ERP, then clear the slot (read-and-clear).
        #
        # IMPORTANT: g_Done carries the ParentID (= pending_pieces.id) and the
        # InitPiece type. We must report the FINAL PRODUCT type to the ERP
        # (e.g. 'RWM'), NOT the InitPiece (which is the shaped top 'RtopW' and
        # would make the ERP log '+1 RtopW' with an empty BOM). So we look the
        # product type up by ParentID in the DB.
        from database import mark_completed, piece_type_by_id
        from transformations import ID_TO_PIECE
        while True:
            try:
                for ev in await plc.read_done_buffer(size=16):
                    parent = ev["parent_id"]
                    # Resolve the final product type + order context by parent.
                    row = None
                    try:
                        row = piece_type_by_id(parent)
                    except Exception as e:
                        print(f"[done] piece_type_by_id({parent}) failed: {e}")
                    if row and row.get("piece_type"):
                        ptype = row["piece_type"]            # e.g. 'RWM'
                        order_id = row.get("order_id")
                        order_line_id = row.get("order_line_id")
                    else:
                        # Fallback: unknown parent -> best-effort InitPiece map.
                        ptype = ID_TO_PIECE.get(ev["type"], ev["type"])
                        order_id = order_line_id = None
                    # Real cost for complex products is not tracked per-parent
                    # reliably (machine costs are keyed by changing piece_ids,
                    # and the M3 step clears the PieceID). Report 0.0 for now;
                    # the FINAL-TYPE fix below is the actual bug being fixed.
                    # TODO: accumulate complex cost by ParentID if needed.
                    real_cost = 0.0
                    try:
                        mark_completed(parent, real_cost)
                    except Exception as e:
                        print(f"[done] mark_completed({parent}) failed: {e}")
                    mqtt_bridge.publish_status({
                        "order_id":      order_id,
                        "order_line_id": order_line_id,
                        "piece_db_id":   parent,
                        "piece_type":    ptype,
                        "status":        "COMPLETED",
                        "real_cost":     real_cost,
                    })
                    print(f"[done] parent={parent} ({ptype}) COMPLETED "
                          f"cost={real_cost:.2f}€")
                    # Release complex serialisation + held assembly cell.
                    try:
                        orchestrator.on_complete(parent)
                    except Exception as e:
                        print(f"[done] on_complete({parent}) failed: {e}")
                    # Clear only after handling -> no completion is ever lost.
                    await plc.clear_done_slot(ev["slot"])
            except Exception as e:
                print(f"[done] completion_loop error: {e}")
            await asyncio.sleep(0.5)

    try:
        await asyncio.gather(
            poller.reg_loop(),
            poller.warehouse_loop(),
            dispatcher_loop(),
            cost_sweep_loop(),
            loader_ack_loop(),
            loader_flush_loop(),
            transfers.run_loop(),
            orchestrator.run_loop(),
            completion_loop(),
        )
    finally:
        await plc.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\n[mes] shutting down.")