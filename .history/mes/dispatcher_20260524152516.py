"""Writes optimised Workpiece_T routings into the PLC handshakes.
Aggressive dispatch: tries to place pieces as soon as cells become free
and there is at least one matching raw material in W1."""
from config import WAREHOUSE_CAPACITY, NUM_CELLS, PIECE_ID
from database import queued_pieces, mark_dispatched
from optimizer import optimise_batch
from recipes import RECIPES


class Dispatcher:
    def __init__(self, plc):
        self.plc = plc
        self._tick_count = 0
        self._last_log_tick = -100

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
            return True

    async def _w1_has_stock(self) -> bool:
        """At least one raw piece available in W1."""
        try:
            return (await self.plc.read_w1_count()) > 0
        except Exception:
            return True

    async def tick(self):
        self._tick_count += 1

        if not await self._w2_has_room():
            return

        # Start production as soon as ANY raw piece is in W1
        if not await self._w1_has_stock():
            return

        pending = queued_pieces()
        if not pending:
            return

        verbose = (self._tick_count - self._last_log_tick) >= 20
        if verbose:
            self._last_log_tick = self._tick_count
            cell_states = {}
            for c in range(1, NUM_CELLS + 1):
                cell_states[c] = await self._cell_free(c)
            w1 = await self.plc.read_w1_count()
            w2 = await self.plc.read_w2_count()
            print(f"[disp] tick #{self._tick_count}: "
                  f"{len(pending)} pieces queued, "
                  f"W1={w1} W2={w2} cell_free={cell_states}")

        # Build availability map of free cells
        free_cells = []
        for c in range(1, NUM_CELLS + 1):
            if await self._cell_free(c):
                free_cells.append(c)

        if not free_cells:
            if verbose:
                print("[disp] no free cells this tick")
            return

        plans = optimise_batch(pending, free_cells_hint=free_cells)
        if not plans:
            return

        dispatched_cells = set()

        for row, plan in plans:
            if not plan:
                continue
            entry_cell = plan[0]["cell"]

            if entry_cell in dispatched_cells:
                # Try any other free cell not yet used this tick
                alt = next((c for c in free_cells
                            if c not in dispatched_cells), None)
                if alt is None:
                    continue
                for op in plan:
                    op["cell"] = alt
                entry_cell = alt

            if entry_cell not in free_cells:
                continue

            piece_type = row["piece_type"]
            recipe = RECIPES[piece_type]
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
            except Exception as e:
                print(f"[disp] write_workpiece FAILED for piece {row['id']} "
                      f"({piece_type}): {e}")
                return

            mark_dispatched(row["id"], entry_cell, init_piece_id)
            dispatched_cells.add(entry_cell)
            print(f"[disp] dispatched piece {row['id']} ({piece_type}) "
                  f"to Cell_{entry_cell} (init={init_piece_id})")

            if not await self._w2_has_room():
                return
            if not await self._w1_has_stock():
                return