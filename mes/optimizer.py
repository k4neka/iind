"""Production planner.

For each queued final product the planner:

  1) Picks the cell to produce it on (must own the required assembly tool
     on M3 AND the required shaping tool on M1 or M2). Cells are balanced
     across queued pieces so we don't pile everything onto one cell.
  2) Decomposes the product into its 3 subparts (top + leg + leg) using
     the ASSEMBLY and SINGLE_TRANSFORM tables.
  3) For each subpart, picks M1 or M2 of the chosen cell (whichever
     finishes earliest accounting for tool changes).
  4) Builds an explicit 3-operation list per subpart so the CODESYS SFC
     can transport the workpiece through every machine of the cell:
        - exactly one machine does real work (Tool>0, OpTime>0)
        - the others are no-ops (Machine matches SFC slot, Tool=0,
          OpTime=0) which the Machine_Conveyor handles via its
          Load_Piece pass-through branch.
"""
from transformations import (SINGLE_TRANSFORM, ASSEMBLY, CELL_TOOLS,
                             TOOL_CHANGE_TIME, PIECE_ID,
                             find_single, find_assembly, raw_for)


class CellTimeline:
    def __init__(self):
        self.ready = {c: {1: 0.0, 2: 0.0, 3: 0.0} for c in CELL_TOOLS}
        self.tool = {c: {1: None, 2: None, 3: None} for c in CELL_TOOLS}

    def cells_with_tool(self, slot: int, tool: int):
        return [c for c in CELL_TOOLS if tool in CELL_TOOLS[c][slot]]

    def cost_to_run(self, cell: int, slot: int, tool: int, duration: float,
                   earliest_start: float = 0.0) -> tuple[float, float]:
        start = max(self.ready[cell][slot], earliest_start)
        change = TOOL_CHANGE_TIME if (self.tool[cell][slot]
                                      not in (None, tool)) else 0
        return start + change, start + change + duration

    def reserve(self, cell: int, slot: int, tool: int, start: float,
                finish: float):
        self.ready[cell][slot] = finish
        self.tool[cell][slot] = tool

    def cell_load(self, cell: int) -> float:
        return sum(self.ready[cell].values())


def _build_ops(cell: int, real_slot: int, real_tool: int, real_time: int,
               m3_tool: int, m3_time: int):
    """Build the 3-operation list (M1, M2, M3) for one subpart.

    Exactly one of M1/M2 carries the real shaping operation; the other is
    a no-op pass-through. M3 either runs the assembly op (top subpart) or
    is also a no-op (leg subparts, which the M3 will just store).
    """
    ops = []
    for slot in (1, 2):
        if slot == real_slot:
            ops.append({"cell": cell, "machine": slot,
                        "tool": real_tool, "op_time_s": real_time})
        else:
            ops.append({"cell": cell, "machine": slot,
                        "tool": 0, "op_time_s": 0})
    ops.append({"cell": cell, "machine": 3,
                "tool": m3_tool, "op_time_s": m3_time})
    return ops


def _best_shaping_slot(target_piece: str, cell: int, tl: CellTimeline,
                       earliest_start: float):
    """Return (slot, transform, start, finish) for shaping target_piece
    on M1 or M2 of `cell` -- whichever yields the earliest finish."""
    options = []
    for tr in find_single(target_piece):
        for slot in (1, 2):
            if tr["tool"] not in CELL_TOOLS[cell][slot]:
                continue
            start, finish = tl.cost_to_run(cell, slot, tr["tool"],
                                           tr["time"], earliest_start)
            options.append((finish, start, slot, tr))
    if not options:
        return None
    options.sort()
    finish, start, slot, tr = options[0]
    return slot, tr, start, finish


def _plan_one_piece(piece_type: str, tl: CellTimeline,
                    preferred_cells: list[int] | None = None):
    asm = find_assembly(piece_type)
    if asm is None:
        return None

    # Cell must own the assembly tool on M3 AND the shaping tool for
    # both top and leg on M1 or M2.
    top_tools = {t["tool"] for t in find_single(asm["top"])}
    leg_tools = {t["tool"] for t in find_single(asm["leg"])}

    def cell_capable(c):
        if asm["tool"] not in CELL_TOOLS[c][3]:
            return False
        shaping_slots = CELL_TOOLS[c][1] | CELL_TOOLS[c][2]
        return bool(top_tools & shaping_slots) and \
               bool(leg_tools & shaping_slots)

    candidate_cells = [c for c in CELL_TOOLS if cell_capable(c)]
    if not candidate_cells:
        return None

    # Honour caller-provided preference order (used for round-robin
    # across many queued pieces).
    if preferred_cells:
        ordered = [c for c in preferred_cells if c in candidate_cells]
        ordered += [c for c in candidate_cells if c not in ordered]
    else:
        ordered = sorted(candidate_cells, key=lambda c: tl.cell_load(c))
    cell = ordered[0]

    subparts_spec = [
        ("top", asm["top"]),
        ("leg", asm["leg"]),
        ("leg", asm["leg"]),
    ]
    plan = []
    # Pieces enter the cell head sequentially. The next one cannot enter
    # while the conveyor at slot 1 is still holding the previous one.
    cell_entry_free_at = tl.ready[cell][1]

    for role, sub_piece in subparts_spec:
        op = _best_shaping_slot(sub_piece, cell, tl, cell_entry_free_at)
        if op is None:
            return None
        slot, tr, start, finish = op
        tl.reserve(cell, slot, tr["tool"], start, finish)

        if role == "top":
            start3, finish3 = tl.cost_to_run(cell, 3, asm["tool"],
                                             asm["time"],
                                             earliest_start=finish)
            tl.reserve(cell, 3, asm["tool"], start3, finish3)
            ops = _build_ops(cell, slot, tr["tool"], tr["time"],
                             asm["tool"], asm["time"])
        else:
            ops = _build_ops(cell, slot, tr["tool"], tr["time"],
                             m3_tool=0, m3_time=0)

        plan.append({
            "raw": PIECE_ID[raw_for(sub_piece)],
            "ops": ops,
        })
        cell_entry_free_at = max(cell_entry_free_at, tl.ready[cell][1])

    return cell, plan


def optimise_batch(queued_pieces: list[dict]):
    """Plan a whole batch. Returns [(row, cell, [subparts...]), ...]."""
    tl = CellTimeline()

    def _seed_length(row):
        a = find_assembly(row["piece_type"])
        if a is None:
            return 0
        legs_t = max(t["time"] for t in find_single(a["leg"])) * 2
        top_t = max(t["time"] for t in find_single(a["top"]))
        return legs_t + top_t + a["time"]

    queue_sorted = sorted(queued_pieces, key=lambda r: -_seed_length(r))

    out = []
    # Round-robin starting cell hint per piece so the optimizer spreads
    # final products across all capable cells (when more than one is
    # available for a given product family).
    rr_cells = list(CELL_TOOLS.keys())
    rr_idx = 0

    for row in queue_sorted:
        hint = rr_cells[rr_idx % len(rr_cells):] + \
               rr_cells[:rr_idx % len(rr_cells)]
        rr_idx += 1
        result = _plan_one_piece(row["piece_type"], tl,
                                 preferred_cells=hint)
        if result is None:
            continue
        cell, subparts = result
        out.append((row, cell, subparts))
    return out