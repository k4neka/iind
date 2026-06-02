"""Orchestrator for complex (mixed-material) pieces: RWM / SWM.

A complex piece needs a wood top + two metal legs. No single cell can
shape both wood and metal on its M1/M2 machines, so the build is staged.
This orchestrator mirrors the working Order_generator(PRG) scheduler
exactly (see prompt.md / codesys_transfercell_config.txt):

    Phase A  In parallel:
               * shape both metal legs INSIDE the assembly cell (a
                 metal+assembly cell, 3 or 4). Leg 1 goes to M2, leg 2 to
                 M1, so they shape at the same time; each carries a
                 trailing no-op at M3 so it parks in the assembly buffer
                 and never leaves the cell.
               * shape the wood top in a wood cell (1 or 2); it exits to
                 W2.
    Phase B  Use the Transfer_Cell to bring ONLY the top from W2 to W1.
    Phase C  Re-inject the top into the assembly cell at M3 with tool 9.
             The two parked legs are consumed and the finished product
             exits to W2.
    Phase D  Mark the order complete, report it, and return the finished
             product to W1 (via the shared TransferManager).

Unlike the previous design, the legs are NOT transferred: they stay in
the assembly cell the whole time. Only the top crosses the corridor.

Complex pieces are processed one at a time so the single Transfer_Cell
and the assembly-cell buffer never get crossed between two products.
"""
import asyncio

from config import COMPLEX_PIECES, COMPLEX_RECIPE
from database import queued_pieces, mark_dispatched, mark_completed

# Machine slot used as the in-cell assembly buffer / assembly station.
M3 = 3


class ComplexOrchestrator:
    def __init__(self, plc, mqtt_bridge, publish_status, transfer_manager,
                 busy_cells: set, busy_lock: asyncio.Lock):
        self.plc = plc
        self.mqtt = mqtt_bridge
        self.publish_status = publish_status
        self.transfers = transfer_manager
        # Shared with the Dispatcher so simple and complex production
        # never claim the same physical cell at once.
        self._busy_cells = busy_cells
        self._busy_lock = busy_lock
        self._active = False     # one complex product in flight at a time

    # ---- helpers --------------------------------------------------------

    @staticmethod
    def _raw_need(rec) -> dict:
        need = {}
        need[rec["top"]["raw"]] = need.get(rec["top"]["raw"], 0) + 1
        need[rec["leg"]["raw"]] = (need.get(rec["leg"]["raw"], 0)
                                   + rec["leg"]["count"])
        return need

    def _can_afford(self, rec) -> bool:
        est = self.mqtt.w1_estimate()
        return all(est.get(k, 0) >= v for k, v in self._raw_need(rec).items())

    async def _claim_cell(self, cells, timeout=60.0):
        """Reserve the first free cell from `cells` (PLC-free and not held
        by the dispatcher or us). Returns the cell number or None."""
        step = 0.2
        for _ in range(max(1, int(timeout / step))):
            async with self._busy_lock:
                for c in cells:
                    if c in self._busy_cells:
                        continue
                    try:
                        if await self.plc.cell_free(c):
                            self._busy_cells.add(c)
                            return c
                    except Exception:
                        continue
            await asyncio.sleep(step)
        return None

    def _release_cell(self, cell):
        if cell is not None:
            self._busy_cells.discard(cell)

    # ---- main loop ------------------------------------------------------

    async def run_loop(self):
        while True:
            try:
                await self._tick()
            except Exception as e:
                print(f"[complex] tick error: {e}")
            await asyncio.sleep(1.0)

    async def _tick(self):
        if self._active:
            return
        pending = queued_pieces()
        todo = [r for r in pending if r["piece_type"] in COMPLEX_PIECES]
        if not todo:
            return

        row = todo[0]
        rec = COMPLEX_RECIPE[row["piece_type"]]
        if not self._can_afford(rec):
            return  # wait for raw material to arrive in W1

        # Claim the order so neither the dispatcher nor the next tick
        # picks it up again.
        mark_dispatched(row["id"], cell=0, init_piece_id=rec["top"]["id"])
        self._active = True
        asyncio.create_task(self._produce(row, rec))

    # ---- production pipeline -------------------------------------------

    async def _produce(self, row, rec):
        pt = row["piece_type"]
        pid = row["id"]
        top, leg, asm = rec["top"], rec["leg"], rec["asm"]
        print(f"[complex] start {pt} (db id {pid})")

        asm_cell = None   # metal+assembly cell: holds the legs AND assembles
        top_cell = None   # wood cell: shapes the top only
        try:
            # Reserve raw material in the W1 model up front.
            for material, qty in self._raw_need(rec).items():
                self.mqtt.w1_consume(material, qty)

            # Claim the assembly cell (held through assembly so the
            # dispatcher cannot inject a simple piece between the legs and
            # the top) and a wood cell for the top.
            asm_cell = await self._claim_cell(asm["cells"])
            if asm_cell is None:
                print(f"[complex] {pt}: no free assembly cell in "
                      f"{asm['cells']}; aborting")
                return
            top_cell = await self._claim_cell(top["cells"])
            if top_cell is None:
                print(f"[complex] {pt}: no free wood cell in "
                      f"{top['cells']}; aborting")
                return

            # -- Phase A: shape legs (in the assembly cell) and the top (in
            # the wood cell) in parallel, mirroring step 0 of the PRG. --
            async def shape_legs():
                # Leg N: shape on its machine (M2 then M1 -> parallel),
                # then a no-op at M3 so it parks in the assembly buffer.
                for i, m in enumerate(leg["machines"][:leg["count"]], 1):
                    ops = [
                        {"cell": asm_cell, "machine": m,
                         "tool": leg["tool"], "op_time_s": leg["time_s"]},
                        {"cell": asm_cell, "machine": M3,
                         "tool": 0, "op_time_s": 0},
                    ]
                    print(f"[complex] {pt}: leg {i} -> Cell_{asm_cell} "
                          f"M{m} (tool {leg['tool']}, {leg['time_s']}s) "
                          f"then park at M3")
                    if not await self.plc.send_workpiece_handshake(
                            asm_cell, leg["raw_id"], ops):
                        return False
                return True

            async def shape_top():
                ops = [{"cell": top_cell, "machine": top["machine"],
                        "tool": top["tool"], "op_time_s": top["time_s"]}]
                print(f"[complex] {pt}: {top['piece']} -> Cell_{top_cell} "
                      f"M{top['machine']} (tool {top['tool']}, "
                      f"{top['time_s']}s) then W2")
                return await self.plc.send_workpiece_handshake(
                    top_cell, top["raw_id"], ops)

            legs_ok, top_ok = await asyncio.gather(shape_legs(), shape_top())

            # The wood cell is no longer needed once the top is accepted.
            self._release_cell(top_cell)
            top_cell = None

            if not (legs_ok and top_ok):
                print(f"[complex] {pt}: sub-part shaping failed; aborting")
                return

            # -- Phase B: bring ONLY the top from W2 to W1 via the
            # Transfer_Cell. Routed through the shared TransferManager so it
            # serialises against finished-goods returns on the single
            # corridor. --
            done = asyncio.Event()
            res = {"ok": False}

            def _cb(ok):
                res["ok"] = ok
                if ok:
                    self.mqtt.w1_add_piece(top["piece"], 1)
                done.set()

            self.transfers.enqueue(top["id"], on_done=_cb,
                                   label=f"{pt}:{top['piece']}")
            await done.wait()
            if not res["ok"]:
                print(f"[complex] {pt}: top transfer failed; aborting")
                return

            # -- Phase C: assemble. Re-inject the top (by its shaped id)
            # into the assembly cell at M3 with tool 9; the two parked legs
            # are consumed. --
            asm_ops = [{"cell": asm_cell, "machine": asm["machine"],
                        "tool": asm["tool"], "op_time_s": asm["time_s"]}]
            print(f"[complex] {pt}: assembling in Cell_{asm_cell} "
                  f"M{asm['machine']} (tool {asm['tool']}, "
                  f"{asm['time_s']}s)")
            if not await self.plc.send_workpiece_handshake(
                    asm_cell, top["id"], asm_ops):
                print(f"[complex] {pt}: assembly failed; aborting")
                return
            # Sub-parts consumed from the W1 model (top came back via the
            # corridor; legs were reserved up front as raw).
            self.mqtt.w1_consume(top["piece"], 1)

            # -- Phase D: complete + return finished product to W1 --
            mark_completed(pid, 0.0)
            self.publish_status({
                "order_id":      row["order_id"],
                "order_line_id": row["order_line_id"],
                "piece_db_id":   pid,
                "piece_type":    pt,
                "status":        "COMPLETED",
                "real_cost":     0.0,
            })
            self.transfers.enqueue(pt)
            print(f"[complex] {pt} (db id {pid}) assembled and returned")
        except Exception as e:
            print(f"[complex] {pt}: pipeline error: {e}")
        finally:
            self._release_cell(top_cell)
            self._release_cell(asm_cell)
            self._active = False
