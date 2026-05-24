"""Production optimisation algorithm.

Strategy:
  * Build the full operation list for each queued piece from `recipes.RECIPES`.
  * For each operation, pick the (cell, machine) pair able to execute it
    (tool present in CELL_TOOLS) AND with the lowest projected end time
    (load-balancing across cells).
  * Track virtual machine timelines to estimate each machine's "ready time".
  * Add a 30 s penalty when the chosen machine is currently mounted with a
    different tool (PDF: tool change = 30 s).
  * Within a single piece, all operations are pinned to the SAME cell to
    avoid unnecessary inter-cell transfers (line layout favors this).
  * Across pieces, cells are balanced by their accumulated workload, and
    longer recipes are scheduled first (LPT-style) for better balancing.
"""
from config import CELL_TOOLS, NUM_CELLS
from recipes import RECIPES


class CellLoadEstimator:
    """Tracks virtual finish-time and currently-mounted tool per machine."""

    def __init__(self):
        # ready_time[cell][machine] = projected seconds-from-now until free
        self.ready_time = {c: {m: 0.0 for m in (1, 2, 3)}
                           for c in range(1, NUM_CELLS + 1)}
        self.mounted_tool = {c: {m: None for m in (1, 2, 3)}
                             for c in range(1, NUM_CELLS + 1)}

    def best_machine(self, tool: int, cell_hint: int | None = None):
        """Return (cell, machine) for the given tool, or None if impossible."""
        candidates = []
        cells = [cell_hint] if cell_hint else range(1, NUM_CELLS + 1)
        for c in cells:
            for m, tools in CELL_TOOLS[c].items():
                if tool not in tools:
                    continue
                start = self.ready_time[c][m]
                change = 30 if (self.mounted_tool[c][m] not in (None, tool)) else 0
                candidates.append((start + change, c, m))
        if not candidates:
            return None
        candidates.sort()
        _, c, m = candidates[0]
        return c, m

    def reserve(self, cell: int, machine: int, tool: int, duration_s: int):
        change = 30 if (self.mounted_tool[cell][machine]
                        not in (None, tool)) else 0
        self.ready_time[cell][machine] += change + duration_s
        self.mounted_tool[cell][machine] = tool

    def total_load(self, cell: int) -> float:
        return sum(self.ready_time[cell].values())


def plan_piece(piece_type: str, estimator: CellLoadEstimator):
    """Return a list of Operation_T-shaped dicts for one piece.

    All ops of a piece are pinned to the same cell, chosen as the cell whose
    assembly machine (M3) for this piece is the earliest available. If a
    given operation's tool isn't available on M1/M2 of that cell, we fall
    back to the globally-best machine (rare, but safe).
    """
    recipe = RECIPES.get(piece_type)
    if not recipe:
        print(f"[opt] no recipe for piece '{piece_type}'")
        return None

    assembly_op = recipe[-1]
    assembly_tool = assembly_op["tool"]

    # 1) Pick the cell whose M3 (assembly) is the least loaded for this tool.
    best = None
    for c in range(1, NUM_CELLS + 1):
        if assembly_tool not in CELL_TOOLS[c][3]:
            continue
        ready = estimator.ready_time[c][3]
        if estimator.mounted_tool[c][3] not in (None, assembly_tool):
            ready += 30
        # Tie-breaker: prefer the cell with the smaller overall load.
        score = (ready, estimator.total_load(c))
        if best is None or score < best[0]:
            best = (score, c)

    if best is None:
        print(f"[opt] no cell can assemble '{piece_type}' (tool {assembly_tool})")
        return None
    cell = best[1]

    # 2) Plan each operation, preferring the chosen cell.
    plan = []
    for op in recipe:
        chosen = estimator.best_machine(op["tool"], cell_hint=cell)
        if chosen is None:
            chosen = estimator.best_machine(op["tool"])
            if chosen is None:
                print(f"[opt] no machine has tool {op['tool']} for "
                      f"{piece_type} → {op['out']}")
                return None
        c2, m2 = chosen
        estimator.reserve(c2, m2, op["tool"], op["time_s"])
        plan.append({
            "cell":      c2,
            "machine":   m2,
            "tool":      op["tool"],
            "op_time_s": op["time_s"],
            "produces":  op["out"],
        })
    return plan


def optimise_batch(queued_pieces: list[dict]):
    """Plan a whole batch of pieces.

    Returns: list of (piece_db_row, routing_plan).
    Pieces with no valid plan are skipped (caller may retry later).
    """
    estimator = CellLoadEstimator()
    # Longer recipes first (LPT) — improves load balance & reduces makespan.
    sorted_pieces = sorted(
        queued_pieces,
        key=lambda r: -len(RECIPES.get(r["piece_type"], []))
    )
    out = []
    for row in sorted_pieces:
        plan = plan_piece(row["piece_type"], estimator)
        if plan is None:
            continue
        out.append((row, plan))
    return out