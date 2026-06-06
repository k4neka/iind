"""MES entry point. Wires PLC + MQTT + DB + dispatcher.

Loops run concurrently via asyncio.gather: warehouse visibility, dispatcher,
loader ack/flush, timing push, transfer worker, complex orchestrator, and the
g_Done completion consumer.
"""
import asyncio
from datetime import datetime, timezone

from config import DISPATCH_INTERVAL, POLL_WAREHOUSE_INTERVAL
from database import init_db
from opcua_client import PLCClient
from mqtt_client import MESMqtt
from tool_state import ToolStateTracker
from dispatcher import Dispatcher, ComplexOrchestrator
from unloader import UnloaderManager


async def main_async():
    init_db()

    # One shared tool-state tracker: the optimiser seeds its tool-change
    # model from it; both production paths update it; reset on PLC reconnect.
    tools = ToolStateTracker()

    plc = PLCClient(on_reconnect=tools.reset)
    if not await plc.connect():
        print("[mes] CRITICAL: cannot connect to PLC. Exiting.")
        return

    loop = asyncio.get_running_loop()

    # The unloader needs the MQTT bridge's publish_status, but the bridge
    # needs the unloader to route delivery orders — so build the unloader
    # first with a late-bound publisher, then wire it into the bridge.
    unloader = UnloaderManager(plc, publish_status=None)
    mqtt_bridge = MESMqtt(loop=loop, plc=plc, unloader=unloader)
    unloader.publish_status = mqtt_bridge.publish_status

    # NOTE: finished products STAY in W2 (output warehouse) until the unloader
    # picks them up — they are never transferred back to W1. The complex-piece
    # top transfer is handled entirely PLC-side by the Router corridor, so the
    # MES no longer drives the corridor at all (v3 Bug 6).

    # Shared cell-lease registry between simple and complex production.
    busy_cells: set[int] = set()
    busy_lock = asyncio.Lock()
    dispatcher = Dispatcher(plc, mqtt_bridge,
                            busy_cells=busy_cells, busy_lock=busy_lock,
                            tool_tracker=tools)
    orchestrator = ComplexOrchestrator(
        plc, mqtt_bridge,
        publish_status=mqtt_bridge.publish_status,
        busy_cells=busy_cells, busy_lock=busy_lock,
        tool_tracker=tools,
    )

    mqtt_bridge.start()

    async def warehouse_loop():
        # Read the PLC's authoritative W1Count[1..2] (Wood/Metal) and snap the
        # MES's local W1 estimate to it. This keeps the dispatcher honest:
        # when an order is queued but its raw material has NOT physically left
        # W1 yet (e.g. the cell's WarehouseOut conveyors are full), W1Count
        # still shows that material as present, so the MES won't wrongly treat
        # it as consumed.
        #
        # W2 occupancy is NOT read from the PLC: there is no g_W2_Count node in
        # the current GVL (it was removed). The unloader tracks finished-goods
        # stock in W2 from completion events instead (see UnloaderManager),
        # so we report that figure for visibility.
        last = (None, None)
        while True:
            try:
                w1 = await plc.read_w1_counts()      # {'Wood','Metal'} or None
                if w1 is not None:
                    mqtt_bridge.w1_sync_from_plc(w1)
                w1_total = None if w1 is None else (w1["Wood"] + w1["Metal"])
                w2_total = unloader.w2_stock_total()
                snap = (w1_total, w2_total)
                if snap != last:
                    last = snap
                    print(f"[wh] PLC W1={w1_total}/32  W2(MES)={w2_total}/32")
            except Exception as e:
                print(f"[poll] warehouse error: {e}")
            await asyncio.sleep(POLL_WAREHOUSE_INTERVAL)

    async def dispatcher_loop():
        # Aggressive 0.5 s tick: starts production as soon as one raw
        # piece is available in the locally-tracked W1.
        while True:
            try:
                await dispatcher.tick()
            except Exception as e:
                print(f"[disp] tick error: {e}")
            await asyncio.sleep(DISPATCH_INTERVAL)

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

    async def timing_push_loop():
        # Every 30 s, publish measured production-time aggregates into the
        # ERP sim_state table so the ERP planner can use real durations.
        from database import (distinct_timing_keys, get_timing_samples,
                              push_timing_to_erp)
        while True:
            try:
                for ptype, cell in distinct_timing_keys():
                    vals = [float(s["actual_seconds"])
                            for s in get_timing_samples(ptype, cell, limit=30)]
                    if not vals:
                        continue
                    n = len(vals)
                    push_timing_to_erp(ptype, cell, sum(vals) / n, n,
                                       min(vals), max(vals))
            except Exception as e:
                print(f"[timing] push error: {e}")
            await asyncio.sleep(30)

    async def completion_loop():
        # Form A: consume the PLC completion buffer (g_Done_*). The PLC writes
        # one event per finished product into an empty slot; we map its wire
        # ParentID back to the pending_pieces row, mark it COMPLETED, report to
        # the ERP, record its production time, then clear the slot. BOTH simple
        # and complex final products flow through here (simple pieces now carry
        # a non-zero wire ParentID).
        #
        # We report the FINAL PRODUCT type (e.g. 'RWM'), resolved by wire id,
        # NOT the InitPiece (the shaped top 'RtopW', which would log a wrong
        # '+1 RtopW' with an empty BOM at the ERP).
        from database import (mark_completed, piece_by_wire_id,
                              add_timing_sample, last_completed_on_cell)
        from transformations import ID_TO_PIECE
        while True:
            try:
                for ev in await plc.read_done_buffer(size=16):
                    parent = ev["parent_id"]          # wire id we wrote
                    row = None
                    try:
                        row = piece_by_wire_id(parent)
                    except Exception as e:
                        print(f"[done] piece_by_wire_id({parent}) failed: {e}")

                    # DEDUPE (§7.1): the CODESYS corridor defect can re-write a
                    # completion for the SAME finished product on later sim-days.
                    # If this piece is already COMPLETED in the DB, do NOT
                    # re-report it (that double-counts ERP inventory and drives
                    # raw stock negative) -- but DO clear the slot so the buffer
                    # can't keep refilling.
                    if row and row.get("finished_at"):
                        print(f"[done] wire={parent} already COMPLETED; "
                              f"ignoring duplicate, clearing slot")
                        await plc.clear_done_slot(ev["slot"])
                        continue

                    if row and row.get("piece_type"):
                        db_id = row["id"]
                        ptype = row["piece_type"]            # e.g. 'RWM'
                        order_id = row.get("order_id")
                        order_line_id = row.get("order_line_id")
                        cell = row.get("assigned_cell")
                        # Prefer the cell-entry time (excludes queue wait,
                        # v3 Bug 7); fall back to the enqueue time.
                        started = (row.get("cell_started_at")
                                   or row.get("started_at"))
                    else:
                        # Fallback: unknown wire -> best-effort InitPiece map.
                        db_id = None
                        ptype = ID_TO_PIECE.get(ev["type"], ev["type"])
                        order_id = order_line_id = cell = started = None

                    # CostTracker is gone: per-piece cost is no longer tracked.
                    real_cost = 0.0
                    if db_id is not None:
                        try:
                            mark_completed(db_id, real_cost)
                        except Exception as e:
                            print(f"[done] mark_completed({db_id}) failed: {e}")
                    mqtt_bridge.publish_status({
                        "order_id":      order_id,
                        "order_line_id": order_line_id,
                        "piece_db_id":   db_id,
                        "piece_type":    ptype,
                        "status":        "COMPLETED",
                        "real_cost":     real_cost,
                    })
                    print(f"[done] wire={parent} ({ptype}) COMPLETED")

                    # The finished product is now physically in W2 and may be
                    # pulled onto an unloading dock to satisfy a delivery
                    # order. Credit the unloader's W2 ledger (pending delivery
                    # demand drains against this).
                    unloader.credit_w2(ptype)

                    # Production-timing sample (Item 2): now - started_at, with
                    # the piece that ran on this cell just before it.
                    if cell and started:
                        try:
                            prev = last_completed_on_cell(cell, before=started)
                            preceding = prev["piece_type"] if prev else None
                            actual = ((datetime.now(timezone.utc) - started)
                                      .total_seconds())
                            add_timing_sample(ptype, cell, preceding, actual)
                            print(f"[timing] {ptype} on Cell_{cell}: "
                                  f"{actual:.1f}s (prev={preceding})")
                        except Exception as e:
                            print(f"[timing] sample failed: {e}")

                    # Release complex serialisation + held assembly cell, and
                    # drive the stale re-injection recv_cmd low (band-aid). A
                    # no-op for simple pieces (not held by the orchestrator).
                    try:
                        await orchestrator.on_complete(parent)
                    except Exception as e:
                        print(f"[done] on_complete({parent}) failed: {e}")

                    # Finished goods stay in W2 (v3 Bug 6): no W2->W1 transfer.

                    # Clear only after handling -> no completion is ever lost.
                    await plc.clear_done_slot(ev["slot"])
            except Exception as e:
                print(f"[done] completion_loop error: {e}")
            await asyncio.sleep(0.5)

    try:
        await asyncio.gather(
            warehouse_loop(),
            dispatcher_loop(),
            loader_ack_loop(),
            loader_flush_loop(),
            timing_push_loop(),
            orchestrator.run_loop(),
            completion_loop(),
            unloader.run_loop(),
        )
    finally:
        await plc.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\n[mes] shutting down.")