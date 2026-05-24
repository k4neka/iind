"""Dispatcher: sends final products to free cells as sequential subparts."""
import asyncio

from config import NUM_CELLS
from database import queued_pieces, mark_dispatched
from optimizer import optimise_batch
from transformations import ID_TO_PIECE


class Dispatcher:
    def __init__(self, plc, mqtt_bridge):
        self.plc = plc
        self.mqtt = mqtt_bridge
        self._tick_count = 0
        self._last_log_tick = -100
        self._busy_cells: set[int] = set()
        self._busy_lock = asyncio.Lock()

    async def _cell_free_plc(self, cell: int) -> bool:
        try:
            return await self.plc.cell_free(cell)
        except Exception as e:
            print(f"[disp] cell_free({cell}) error: {e}")
            return False

    def _w1_local(self) -> dict:
        return self.mqtt.w1_estimate()

    def _w1_total(self) -> int:
        est = self._w1_local()
        return est.get("Wood", 0) + est.get("Metal", 0)

    def _need_for(self, subparts):
        need = {"Wood": 0, "Metal": 0}
        for sp in subparts:
            raw_name = ID_TO_PIECE.get(sp["raw"])
            if raw_name in need:
                need[raw_name] += 1
        return need

    def _can_afford(self, subparts) -> bool:
        need = self._need_for(subparts)
        est = self._w1_local()
        return all(est.get(k, 0) >= v for k, v in need.items())

    def _consume_w1(self, subparts):
        for sp in subparts:
            raw_name = ID_TO_PIECE.get(sp["raw"])
            if raw_name in ("Wood", "Metal"):
                self.mqtt.w1_consume(raw_name, 1)

    async def tick(self):
        self._tick_count += 1
        pending = queued_pieces()
        if not pending:
            return

        verbose = (self._tick_count - self._last_log_tick) >= 20
        if verbose:
            self._last_log_tick = self._tick_count
            states = {}
            for c in range(1, NUM_CELLS + 1):
                free_plc = await self._cell_free_plc(c)
                states[c] = free_plc and (c not in self._busy_cells)
            est = self._w1_local()
            print(f"[disp] tick #{self._tick_count}: "
                  f"{len(pending)} queued, "
                  f"w1=wood:{est.get('Wood',0)}/metal:{est.get('Metal',0)},"
                  f" free={states}")

        if self._w1_total() == 0:
            if verbose:
                print("[disp] W1 empty - waiting for material")
            return

        plans = optimise_batch(pending)
        if not plans:
            return

        for row, target_cell, subparts in plans:
            if not self._can_afford(subparts):
                if verbose:
                    est = self._w1_local()
                    need = self._need_for(subparts)
                    print(f"[disp] piece {row['id']} ({row['piece_type']}) "
                          f"waiting - need wood:{need['Wood']} "
                          f"metal:{need['Metal']} "
                          f"(have wood:{est.get('Wood',0)} "
                          f"metal:{est.get('Metal',0)})")
                continue

            # Pick a free cell. Prefer the optimizer's target; fall back
            # to any other capable cell that is free.
            cell = None
            async with self._busy_lock:
                if (target_cell not in self._busy_cells
                        and await self._cell_free_plc(target_cell)):
                    cell = target_cell
                else:
                    for alt in range(1, NUM_CELLS + 1):
                        if alt == target_cell:
                            continue
                        if alt in self._busy_cells:
                            continue
                        if await self._cell_free_plc(alt):
                            cell = alt
                            break
                if cell is None:
                    continue
                self._busy_cells.add(cell)

            # If we ended up on a different cell, rewrite cell on every op.
            if cell != target_cell:
                for sp in subparts:
                    for op in sp["ops"]:
                        op["cell"] = cell

            # Reserve material now so the next tick sees up-to-date stock.
            self._consume_w1(subparts)

            mark_dispatched(row["id"], cell, subparts[0]["raw"])
            print(f"[disp] piece {row['id']} ({row['piece_type']}) -> "
                  f"Cell_{cell} as {len(subparts)} subparts")

            asyncio.create_task(
                self._run_piece(row["id"], row["piece_type"],
                                cell, subparts)
            )

    async def _run_piece(self, piece_id: int, piece_type: str,
                         cell: int, subparts: list[dict]):
        try:
            for idx, sp in enumerate(subparts, start=1):
                ok = await self._send_subpart(cell, sp, piece_id, idx,
                                              len(subparts))
                if not ok:
                    print(f"[disp] piece {piece_id} aborted at subpart {idx}")
                    return
            print(f"[disp] piece {piece_id} ({piece_type}) "
                  f"all subparts sent to Cell_{cell}")
        finally:
            async with self._busy_lock:
                self._busy_cells.discard(cell)

    async def _send_subpart(self, cell: int, sp: dict, piece_id: int,
                            idx: int, total: int) -> bool:
        # Wait for the cell head to be free (PLC side).
        for _ in range(600):
            if await self._cell_free_plc(cell):
                break
            await asyncio.sleep(0.1)
        else:
            print(f"[disp] piece {piece_id} sub{idx}: cell never free")
            return False

        try:
            await self.plc.write_workpiece(
                cell=cell,
                init_piece=sp["raw"],
                operations=sp["ops"],
            )
        except Exception as e:
            print(f"[disp] write_workpiece sub{idx}/{total} "
                  f"piece {piece_id} failed: {e}")
            return False

        # Wait for the cell to accept the workpiece (free_cmd -> FALSE).
        for _ in range(600):
            if not await self._cell_free_plc(cell):
                break
            await asyncio.sleep(0.1)
        else:
            print(f"[disp] piece {piece_id} sub{idx}: free_cmd never dropped")
            return False

        # Mirror the Order_generator pattern: lower recv_cmd once accepted.
        try:
            await self.plc.clear_recv_cmd(cell)
        except Exception as e:
            print(f"[disp] clear_recv_cmd error: {e}")

        # Wait until the cell head is ready for the next subpart.
        for _ in range(6000):
            if await self._cell_free_plc(cell):
                return True
            await asyncio.sleep(0.1)

        print(f"[disp] piece {piece_id} sub{idx}: free_cmd never rose")
        return False