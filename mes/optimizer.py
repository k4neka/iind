"""Production planner with tool-change minimisation.

Exactly ONE final product per chunk (physical invariant: the SFS M3
assembly consumes every parked leg when a top arrives, so only 2 legs may
be parked before 1 top). Each chunk emits, for a single cell:

  - 2 leg subparts, alternating M1/M2 to shape on both machines in parallel
    (each parked at M3),
  - 1 top subpart followed by the M3 assembly op.

The optimiser only proposes a `target_cell`; the dispatcher is free to
override it if the suggested cell isn't immediately available.
"""
from transformations import (CELL_TOOLS, TOOL_CHANGE_TIME, PIECE_ID,
                             find_single, find_assembly, raw_for)


class CellTimeline:
    def __init__(self, tool_state=None):
        self.ready = {c: {1: 0.0, 2: 0.0, 3: 0.0} for c in CELL_TOOLS}
        # Seed the mounted-tool model from the live per-cell tool state
        # (ToolStateTracker.all_cells_snapshot) so the tool-change estimate carries
        # over between consecutive optimise_batch() calls instead of starting
        # from None (i.e. assuming a fresh, tool-less cell) every batch.
        self.tool = {c: {1: None, 2: None, 3: None} for c in CELL_TOOLS}
        if tool_state:
            for c, slots in tool_state.items():
                if c in self.tool:
                    for slot, tool in slots.items():
                        if slot in self.tool[c]:
                            self.tool[c][slot] = tool

    def cost_to_run(self, cell, slot, tool, duration,
                    earliest_start=0.0):
        start = max(self.ready[cell][slot], earliest_start)
        change = (TOOL_CHANGE_TIME
                  if self.tool[cell][slot] not in (None, tool) else 0)
        return start + change, start + change + duration

    def reserve(self, cell, slot, tool, start, finish):
        self.ready[cell][slot] = finish
        self.tool[cell][slot] = tool

    def cell_load(self, cell):
        return sum(self.ready[cell].values())


def capable_cells(piece_type: str) -> list[int]:
    """Cells that can build `piece_type` *within a single cell*.

    Both the top and the legs must be shapeable on the cell's M1/M2
    machines (slots 1,2). Mixed-material pieces (RWM/SWM = wood top +
    metal legs) fail this test in every cell: no single cell has both
    wood (T1-T3) and metal (T4-T6) shaping tools on M1/M2. Those pieces
    are handled out-of-band by the ComplexOrchestrator, which produces
    the sub-parts in separate cells and stages them through the Transfer
    Cell — so they are intentionally excluded here.
    """
    asm = find_assembly(piece_type)
    if asm is None:
        return []
    top_tools = {t["tool"] for t in find_single(asm["top"])}
    leg_tools = {t["tool"] for t in find_single(asm["leg"])}
    out = []
    for c in CELL_TOOLS:
        if asm["tool"] not in CELL_TOOLS[c][3]:
            continue
        shaping = CELL_TOOLS[c][1] | CELL_TOOLS[c][2]
        if (top_tools & shaping) and (leg_tools & shaping):
            out.append(c)
    return out


def _best_slot_sticky(target_piece, cell, tl, earliest_start,
                      prefer_slot=None, allowed_slots=(1, 2)):
    """Pick the slot in `allowed_slots` with the best finish time,
    preferring one that already has the needed tool mounted (sticky
    tools). `allowed_slots` defaults to the two shaping machines; pass
    (3,) to shape an off-material part on the M3 assembly machine using
    its alt tools."""
    transforms = find_single(target_piece)

    # Prefer a slot that already has the right tool: zero swap cost.
    for tr in transforms:
        for slot in allowed_slots:
            if tr["tool"] not in CELL_TOOLS[cell][slot]:
                continue
            if tl.tool[cell][slot] == tr["tool"]:
                start, finish = tl.cost_to_run(cell, slot, tr["tool"],
                                               tr["time"],
                                               earliest_start)
                return slot, tr, start, finish

    options = []
    for tr in transforms:
        if prefer_slot in allowed_slots:
            slot_order = ([prefer_slot]
                          + [s for s in allowed_slots if s != prefer_slot])
        else:
            slot_order = list(allowed_slots)
        for idx, slot in enumerate(slot_order):
            if tr["tool"] not in CELL_TOOLS[cell][slot]:
                continue
            start, finish = tl.cost_to_run(cell, slot, tr["tool"],
                                           tr["time"],
                                           earliest_start)
            options.append((idx, finish, start, slot, tr))
    if not options:
        return None
    options.sort()
    _, finish, start, slot, tr = options[0]
    return slot, tr, start, finish


def _plan_chunk(piece_type: str, count: int, tl: CellTimeline,
                preferred_cells):
    """Plan a chunk of `count` identical final products on one cell."""
    asm = find_assembly(piece_type)
    if asm is None:
        return None

    candidates = capable_cells(piece_type)
    if not candidates:
        return None

    ordered = [c for c in preferred_cells if c in candidates]
    ordered += [c for c in candidates if c not in ordered]
    cell = ordered[0]

    subparts = []
    cell_entry_free_at = tl.ready[cell][1]

    # Phase 1: 2 * count leg subparts (shaped on M1/M2, parked at M3).
    for i in range(2 * count):
        prefer = 2 if i % 2 == 0 else 1
        picked = _best_slot_sticky(asm["leg"], cell, tl,
                                   cell_entry_free_at,
                                   prefer_slot=prefer,
                                   allowed_slots=(1, 2))
        if picked is None:
            return None
        slot, tr, start, finish = picked
        tl.reserve(cell, slot, tr["tool"], start, finish)

        ops = [
            # `out` = this op's OUTPUT piece, so the statistics recorder can
            # attribute "shaped 1 LegW" to the machine that ran it (TASK 1).
            {"cell": cell, "machine": slot,
             "tool": tr["tool"], "op_time_s": tr["time"], "out": asm["leg"]},
            {"cell": cell, "machine": 3,
             "tool": 0, "op_time_s": 0},          # park: no tool, no output
        ]
        subparts.append({
            "raw": PIECE_ID[raw_for(asm["leg"])],
            "ops": ops,
            "final": False,          # a parked leg; never fires g_Done
        })
        cell_entry_free_at = max(cell_entry_free_at, tl.ready[cell][1])

    # Phase 2: `count` top + assembly subparts.
    for top_idx in range(count):
        picked = _best_slot_sticky(asm["top"], cell, tl,
                                   cell_entry_free_at,
                                   prefer_slot=1)
        if picked is None:
            return None
        slot, tr, start, finish = picked
        tl.reserve(cell, slot, tr["tool"], start, finish)

        start3, finish3 = tl.cost_to_run(cell, 3, asm["tool"],
                                         asm["time"],
                                         earliest_start=finish)
        tl.reserve(cell, 3, asm["tool"], start3, finish3)

        ops = [
            # Shaping op outputs the top (e.g. RtopW); the M3 op assembles the
            # FINAL product (e.g. RWW) — attributed to M3, not the shapers.
            {"cell": cell, "machine": slot,
             "tool": tr["tool"], "op_time_s": tr["time"], "out": asm["top"]},
            {"cell": cell, "machine": 3,
             "tool": asm["tool"], "op_time_s": asm["time"], "out": piece_type},
        ]
        subparts.append({
            "raw": PIECE_ID[raw_for(asm["top"])],
            "ops": ops,
            "final": True,           # the assembled final product
            "final_index": top_idx,  # -> chunk_rows[top_idx]
        })
        cell_entry_free_at = max(cell_entry_free_at, tl.ready[cell][1])

    return cell, subparts


def optimise_batch(queued_pieces, rr_offset_by_family=None, tool_state=None):
    """Plan the queue.

    Returns a list of tuples:
        (list_of_rows, target_cell, capable_cells, subparts)

    `rr_offset_by_family` is an optional dict {capable_cells_tuple: int}
    that the dispatcher may pass in to seed the round-robin across
    repeated calls. If None, an internal counter is used.

    `tool_state` is an optional {cell: {slot: tool}} snapshot
    (ToolStateTracker.all_cells_snapshot) used to seed the tool-change model so
    estimates carry the currently-mounted tools forward.
    """
    tl = CellTimeline(tool_state=tool_state)

    groups: dict[str, list] = {}
    order: list[str] = []
    for r in queued_pieces:
        pt = r["piece_type"]
        if pt not in groups:
            groups[pt] = []
            order.append(pt)
        groups[pt].append(r)

    def _cycle_cost(pt):
        a = find_assembly(pt)
        if a is None:
            return 0
        legs_t = max(t["time"] for t in find_single(a["leg"])) * 2
        top_t = max(t["time"] for t in find_single(a["top"]))
        return legs_t + top_t + a["time"]

    # Heaviest cycles first so the timeline accounts for them early.
    order.sort(key=lambda pt: -_cycle_cost(pt))

    family_rr: dict[tuple, int] = dict(rr_offset_by_family or {})
    out = []

    # One product per chunk (no batching). Round-robin spreads consecutive
    # same-type products across capable cells.
    for pt in order:
        caps = tuple(capable_cells(pt))
        if not caps:
            continue
        for row in groups[pt]:
            idx = family_rr.get(caps, 0)
            hint = (list(caps[idx % len(caps):])
                    + list(caps[:idx % len(caps)]))
            family_rr[caps] = idx + 1

            result = _plan_chunk(pt, 1, tl, preferred_cells=hint)
            if result is None:
                continue
            cell, subparts = result
            out.append(([row], cell, list(caps), subparts))

    # Return planning + new RR state so the caller can persist it.
    return out, family_rr