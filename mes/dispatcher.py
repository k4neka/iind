"""Writes optimised Workpiece_T routings into the PLC handshakes."""
import asyncio

from config import WAREHOUSE_CAPACITY, NUM_CELLS, PIECE_ID
from database import queued_pieces, mark_dispatched
from optimizer import optimise_batch
from recipes import RECIPES


class Dispatcher:
    """Cycles through the queue and pushes ready workpieces to free cells."""

    def __init__(self, plc):
        self.plc = plc

    async def _cell_free(self, cell: int) -> bool:
        try:
            return await self.plc.cell_free(cell)
        except Exception as e:
            print(f"[disp] cell_free({cell}) error: {e}")
            return False

    async def _w2_has_room(self) -> bool:
        try:
            return (await self.plc.read_w2_count()) < WAREHOUSE_CAPACITY
        except Exception as e:
            print(f"[disp] read_w2_count error: {e}")
            return True   # fail-open: don't block dispatch on a read error

    async def tick(self):
        """Called periodically by the main loop."""
        # Hard constraint: never dispatch if W2 is full.
        if not await self._w2_has_room():
            return

        pending = queued_pieces()
        if not pending:
            return

        plans = optimise_batch(pending)
        if not plans:
            return

        # Try to place each plan on its planned entry cell, if free.
        for row, plan in plans:
            if not plan:
                continue
            entry_cell = plan[0]["cell"]

            if not await self._cell_free(entry_cell):
                # Try other cells whose entry machine has the right tool.
                placed = False
                for alt in range(1, NUM_CELLS + 1):
                    if alt == entry_cell:
                        continue
                    if await self._cell_free(alt):
                        # Rewrite the cell field of every operation to `alt`
                        # (the recipe is the same, only the physical cell
                        # changes; the assembly cell field is updated too).
                        for op in plan:
                            op["cell"] = alt
                        entry_cell = alt
                        placed = True
                        break
                if not placed:
                    continue

            piece_type = row["piece_type"]
            recipe = RECIPES[piece_type]
            # InitPiece = raw material of the first operation.
            init_piece_id = PIECE_ID[recipe[0]["raw"]]

            try:
                await self.plc.write_workpiece(
                    cell=entry_cell,
                    init_piece=init_piece_id,
                    operations=[{
                        "cell":      op["cell"],
                        "machine":   op["machine"],
                        "tool":      op["tool"],
                        "op_time_s": op["op_time_s"],
                    } for op in plan],
                )
                mark_dispatched(row["id"], entry_cell, init_piece_id)
                print(f"[disp] dispatched piece {row['id']} ({piece_type}) "
                      f"to Cell_{entry_cell} (init={init_piece_id})")
            except Exception as e:
                print(f"[disp] write_workpiece error: {e}")
                # Stop this tick; retry next interval
                return

            # Re-check W2 room before sending the next piece.
            if not await self._w2_has_room():
                return