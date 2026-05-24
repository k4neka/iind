"""Dispatcher: sends final products to free cells as sequential subparts.

Mirrors the CODESYS Order_generator handshake:
  1) Write Workpiece_T into Cell_X_Top_Order and raise recv_cmd
  2) Wait until Cell_X_Top_Status.free_cmd drops (cell accepted it)
  3) Lower recv_cmd
  4) Wait until free_cmd rises again (cell ready for the next subpart)
  5) Repeat for the next subpart of the same final product

Subparts are computed dynamically by the optimizer based on the PDF
transformation tables -- there are no hardcoded recipes.
"""
import asyncio

from config import NUM_CELLS
from database import queued_pieces, mark_dispatched
from optimizer import optimise_batch


class Dispatcher:
    def __init__(self, plc):
        self.plc = plc
        self._tick_count = 0
        self._last_log_tick = -100
        # Cells currently running a subpart sequence (MES-side reservation)
        self._busy_cells: set[int] = set()
        self._busy_lock = asyncio.Lock()

    async def _cell_free_plc(self, cell: int) -> bool:
        try:
            return await self.plc.cell_free(cell)
        except Exception as e:
            print(f"[disp] cell_free({cell}) error: {e}")
            return False

    async def _w1_count(self) -> int:
        try:
            return await self.plc.read_w1_count()
        except Exception:
            return 0

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
            w1 = await self._w1_count()
            print(f"[disp] tick #{self._tick_count}: "
                  f"{len(pending)} queued, w1={w1}, free={states}")

        # Need at least one raw piece in W1 before we attempt anything.
        if await self._w1_count() == 0:
            if verbose:
                print("[disp] W1 empty - waiting for material")
            return

        plans = optimise_batch(pending)
        if not plans:
            return

        for row, target_cell, subparts in plans:
            # Re-pick a free cell if the optimizer's target is taken.
            cell = None
            async with self._busy_lock:
                if (target_cell not in self._busy_cells
                        and await self._cell_free_plc(target_cell)):
                    cell = target_cell
                else:
                    for alt in range(1, NUM_CELLS + 1):
                        if alt in self._busy_cells:
                            continue
                        if await self._cell_free_plc(alt):
                            cell = alt
                            break
                if cell is None:
                    continue
                self._busy_cells.add(cell)

            # Rewrite the cell field on every op if we fell back to alt cell.
            if cell != target_cell:
                for sp in subparts:
                    for op in sp["ops"]:
                        op["cell"] = cell

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
            print(f"[disp] piece {piece_id} ({piece_type}) all subparts "
                  f"sent to Cell_{cell}")
        finally:
            async with self._busy_lock:
                self._busy_cells.discard(cell)

    async def _send_subpart(self, cell: int, sp: dict, piece_id: int,
                            idx: int, total: int) -> bool:
        # Wait for the cell to be PLC-level free.
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

        # Wait for cell to take the workpiece (free_cmd -> FALSE).
        for _ in range(600):
            if not await self._cell_free_plc(cell):
                break
            await asyncio.sleep(0.1)
        else:
            print(f"[disp] piece {piece_id} sub{idx}: free_cmd never dropped")
            return False

        # Lower recv_cmd, then wait for the cell to be ready again.
        try:
            await self.plc.clear_recv_cmd(cell)
        except Exception as e:
            print(f"[disp] clear_recv_cmd error: {e}")

        for _ in range(6000):
            if await self._cell_free_plc(cell):
                return True
            await asyncio.sleep(0.1)

        print(f"[disp] piece {piece_id} sub{idx}: free_cmd never rose")
        return False