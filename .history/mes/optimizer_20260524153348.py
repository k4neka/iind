"""Production planner.

For each queued final product, the planner:

  1. Decomposes the product into its 3 input subparts (top + leg + leg)
     using the ASSEMBLY table, then resolves each subpart back to a raw
     material via the SINGLE_TRANSFORM table.
  2. Computes which CODESYS cell can produce the product (the cell's M3
     must own the required assembly tool).
  3. For each subpart, picks M1 or M2 of that cell — whichever can hold
     the required shaping tool AND finishes earliest given (a) current
     virtual ready time and (b) a 30-s tool-change penalty if needed.
  4. Adds a third operation slot on M3 (assembly for the top subpart, or a
     no-op "pass-through" for the two legs) so the workpiece traverses the
     full Wout -> M1 -> T1 -> M2 -> T2 -> M3 -> Win path the SFC expects.
  5. Updates the virtual ready-time of every machine it uses, accounting
     for the strict in-cell flow: a subpart entering M2 cannot pass through
     M1 until M1 has finished its current job (because conveyors are
     pull-based inside the cell).
"""
from transformations import (SINGLE_TRANSFORM, ASSEMBLY, CELL_TOOLS,
                             TOOL_CHANGE_TIME, PIECE_ID,
                             find_single, find_assembly, raw_for)


class CellTimeline:
    """Tracks ready-time and mounted tool for each machine of each cell."""

    def __init__(self):
        self.ready = {c: {1: 0.0, 2: 0.0, 3: 0.0} for c in CELL_TOOLS}
        self.tool = {c: {1: None, 2: None, 3: None} for c in CELL_TOOLS}

    def cells_with_tool(self, slot: int, tool: int):
        return [c for c in CELL_TOOLS if tool in CELL_TOOLS[c][slot]]

    def slots_with_tool(self, cell: int, tool: int):
        return [m for m, tools in CELL_TOOLS[cell].items() if tool in tools]

    def cost_to_run(self, cell: int, slot: int, tool: int, duration: float,
                   earliest_start: float = 0.0) -> tuple[float, float]:
        """Return (start_time, finish_time) if we ran this op now."""
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


def _best_single_op(target_piece: str, cell: int, tl: CellTimeline,
                    earliest_start: float):
    """Pick (slot, transform, start, finish) for producing target_piece
    on `cell` using M1 or M2 (the shaping machines).

    Returns None if no machine in this cell can host any viable tool.
    """
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
    options.sort()  # earliest finish wins
    finish, start, slot, tr = options[0]
    return slot, tr, start, finish


def _plan_one_piece(piece_type: str, tl: CellTimeline):
    """Build the 3-subpart plan for one final product instance.

    Returns (cell, [subpart_dict, subpart_dict, subpart_dict]) or None.

    Each subpart_dict contains:
      raw:  int piece ID for the Loader
      ops:  list of dicts {cell, machine, tool, op_time_s}
    """
    asm = find_assembly(piece_type)
    if asm is None:
        return None

    # 1) Pick cell whose M3 can do the assembly tool AND has the smallest
    #    total load (load balancing).
    candidate_cells = tl.cells_with_tool(3, asm["tool"])
    if not candidate_cells:
        return None
    candidate_cells.sort(key=lambda c: tl.cell_load(c))
    cell = candidate_cells[0]

    # 2) Plan the three subparts: top first (gets the assembly op on M3),
    #    then the two legs (which only pass through M3).
    subparts_spec = [
        ("top", asm["top"]),
        ("leg", asm["leg"]),
        ("leg", asm["leg"]),
    ]
    plan = []
    # The shared "previous machine in the cell" finish time -- the next
    # subpart cannot enter M1 of the same cell until the previous subpart
    # has cleared M1 (in-cell flow constraint).
    cell_entry_free_at = tl.ready[cell][1]

    for role, sub_piece in subparts_spec:
        op = _best_single_op(sub_piece, cell, tl,
                             earliest_start=cell_entry_free_at)
        if op is None:
            return None
        slot, tr, start, finish = op

        # Reserve the shaping machine
        tl.reserve(cell, slot, tr["tool"], start, finish)

        # Build the workpiece's operation list (max 2 ops per subpart).
        # We always have an M3 step: either the assembly itself (for the
        # top) or a no-op pass-through (Tool=0, OpTime=0) for the legs so
        # the SFC routes the piece through Win at the end.
        ops = []

        # If shaping happened on M2, the piece transparently passes M1
        # (Machine=0 / Tool=0 / OpTime=0).  The SFC will just move it
        # along the conveyor.  We do NOT add an explicit slot-1 entry --
        # the Machine_Conveyor checks Operations[Next_Operation].Machine
        # and skips when it's not equal to its own MachineID.
        ops.append({
            "cell":      cell,
            "machine":   slot,
            "tool":      tr["tool"],
            "op_time_s": tr["time"],
        })

        if role == "top":
            # M3 must mount the assembly tool and run the assembly op.
            start3, finish3 = tl.cost_to_run(cell, 3, asm["tool"],
                                             asm["time"],
                                             earliest_start=finish)
            tl.reserve(cell, 3, asm["tool"], start3, finish3)
            ops.append({
                "cell":      cell,
                "machine":   3,
                "tool":      asm["tool"],
                "op_time_s": asm["time"],
            })
            # Legs entering AFTER the top must wait for the cell entry
            # to clear; refresh the constraint accordingly.
            cell_entry_free_at = max(cell_entry_free_at,
                                     tl.ready[cell][1])
        else:
            # Leg: no work on M3, just pass through.  Use Tool=0/Time=0;
            # the SFC's Machine_Conveyor will treat this as a no-op.
            # We still bump M3 ready-time by a small epsilon to account
            # for transport, but keep it negligible.
            ops.append({
                "cell":      cell,
                "machine":   3,
                "tool":      0,
                "op_time_s": 0,
            })
            cell_entry_free_at = max(cell_entry_free_at,
                                     tl.ready[cell][slot])

        plan.append({
            "raw": PIECE_ID[raw_for(sub_piece)],
            "ops": ops,
        })

    return cell, plan


def optimise_batch(queued_pieces: list[dict]):
    """Plan a whole batch of queued final products.

    Sort policy: longest projected cycle first (LPT) for balancing.
    Returns: list of tuples (row, target_cell, [subparts])
    Pieces that cannot be planned (no machine has the required tool) are
    skipped so the caller can retry them later.
    """
    tl = CellTimeline()
    # Estimate naive cycle length to seed the LPT sort.
    def _seed_length(row):
        a = find_assembly(row["piece_type"])
        if a is None:
            return 0
        legs_t = max(t["time"] for t in find_single(a["leg"])) * 2
        top_t = max(t["time"] for t in find_single(a["top"]))
        return legs_t + top_t + a["time"]

    queue_sorted = sorted(queued_pieces, key=lambda r: -_seed_length(r))

    out = []
    for row in queue_sorted:
        result = _plan_one_piece(row["piece_type"], tl)
        if result is None:
            continue
        cell, subparts = result
        out.append((row, cell, subparts))
    return out