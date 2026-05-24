"""Writes optimised Workpiece_T routings into the PLC handshakes.
Dispatch agressivo: tenta colocar peças mal as cells estejam livres.
NÃO verifica W1 stock (porque g_W1_Count pode não estar a ser
incrementado pelo PLC). Confia no SFC da cell para esperar pela peça."""
from config import WAREHOUSE_CAPACITY, NUM_CELLS, PIECE_ID
from database import queued_pieces, mark_dispatched
from optimizer import optimise_batch
from recipes import RECIPES


class Dispatcher:
    """Cycles through the queue and pushes ready workpieces to free cells."""

    def __init__(self, plc):
        self.plc = plc
        self._tick_count = 0
        self._last_log_tick = -100  # para fazer log resumido a cada N ticks

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
            return True   # fail-open

    async def tick(self):
        """Called periodically by the main loop."""
        self._tick_count += 1

        if not await self._w2_has_room():
            return

        pending = queued_pieces()
        if not pending:
            return

        # Log resumido a cada ~20 ticks (10s) para vermos progresso
        verbose = (self._tick_count - self._last_log_tick) >= 20
        if verbose:
            self._last_log_tick = self._tick_count
            # Verificar estado de cada cell
            cell_states = {}
            for c in range(1, NUM_CELLS + 1):
                cell_states[c] = await self._cell_free(c)
            print(f"[disp] tick #{self._tick_count}: "
                  f"{len(pending)} pieces queued, "
                  f"cell_free={cell_states}")

        plans = optimise_batch(pending)
        if not plans:
            if verbose:
                print(f"[disp] optimise_batch returned empty plan list")
            return

        dispatched_cells = set()

        for row, plan in plans:
            if not plan:
                continue
            entry_cell = plan[0]["cell"]

            if entry_cell in dispatched_cells:
                continue

            if not await self._cell_free(entry_cell):
                # Tenta outras cells livres
                placed = False
                for alt in range(1, NUM_CELLS + 1):
                    if alt == entry_cell or alt in dispatched_cells:
                        continue
                    if await self._cell_free(alt):
                        # Reescreve o cell_id no plano para a nova cell
                        for op in plan:
                            op["cell"] = alt
                        entry_cell = alt
                        placed = True
                        break
                if not placed:
                    if verbose:
                        print(f"[disp] piece {row['id']} ({row['piece_type']}) "
                              f"waiting — no free cell")
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
                print(f"[disp] piece {row['id']} kept QUEUED, will retry next tick")
                return

            mark_dispatched(row["id"], entry_cell, init_piece_id)
            dispatched_cells.add(entry_cell)
            print(f"[disp] dispatched piece {row['id']} ({piece_type}) "
                  f"to Cell_{entry_cell} (init={init_piece_id})")

            if not await self._w2_has_room():
                return