"""Production planner with tool-change minimisation and batching.

For every CHUNK of up-to-PRODUCT_BATCH_SIZE queued final products of
the same type, the planner emits a sequence of subparts intended for a
single cell:

  - 2 * batch_size leg subparts, alternating between M1 and M2 to use
    the two shaping machines in parallel.
  - batch_size top subparts, each followed by an M3 assembly op.

The optimiser only proposes a `target_cell`; the dispatcher is free to
override it if the suggested cell isn't immediately available.
"""
from config import PRODUCT_BATCH_SIZE
from transformations import (CELL_TOOLS, TOOL_CHANGE_TIME, PIECE_ID,
                             find_single, find_assembly, raw_for)


class CellTimeline:
    def __init__(self):
        self.ready = {c: {1: 0.0, 2: 0.0, 3: 0.0} for c in CELL_TOOLS}
        self.tool = {c: {1: None, 2: None, 3: None} for c in CELL_TOOLS}

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
    """Cells whose tool kit can actually produce `piece_type`."""
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
                      prefer_slot=None):
    """Pick the slot (1 or 2) with the best finish time, preferring
    one that already has the needed tool mounted (sticky tools)."""
    transforms = find_single(target_piece)

    # Prefer a slot that already has the right tool: zero swap cost.
    for tr in transforms:
        for slot in (1, 2):
            if tr["tool"] not in CELL_TOOLS[cell][slot]:
                continue
            if tl.tool[cell][slot] == tr["tool"]:
                start, finish = tl.cost_to_run(cell, slot, tr["tool"],
                                               tr["time"],
                                               earliest_start)
                return slot, tr, start, finish

    options = []
    for tr in transforms:
        slot_order = ([prefer_slot, 3 - prefer_slot]
                      if prefer_slot in (1, 2) else [1, 2])
        for idx, slot in enumerate(slot_order):
            if slot not in (1, 2):
                continue
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

    # Phase 1: 2 * count leg subparts.
    for i in range(2 * count):
        prefer = 2 if i % 2 == 0 else 1
        picked = _best_slot_sticky(asm["leg"], cell, tl,
                                   cell_entry_free_at,
                                   prefer_slot=prefer)
        if picked is None:
            return None
        slot, tr, start, finish = picked
        tl.reserve(cell, slot, tr["tool"], start, finish)

        ops = [
            {"cell": cell, "machine": slot,
             "tool": tr["tool"], "op_time_s": tr["time"]},
            {"cell": cell, "machine": 3,
             "tool": 0, "op_time_s": 0},
        ]
        subparts.append({
            "raw": PIECE_ID[raw_for(asm["leg"])],
            "ops": ops,
        })
        cell_entry_free_at = max(cell_entry_free_at, tl.ready[cell][1])

    # Phase 2: `count` top + assembly subparts.
    for _ in range(count):
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
            {"cell": cell, "machine": slot,
             "tool": tr["tool"], "op_time_s": tr["time"]},
            {"cell": cell, "machine": 3,
             "tool": asm["tool"], "op_time_s": asm["time"]},
        ]
        subparts.append({
            "raw": PIECE_ID[raw_for(asm["top"])],
            "ops": ops,
        })
        cell_entry_free_at = max(cell_entry_free_at, tl.ready[cell][1])

    return cell, subparts


def optimise_batch(queued_pieces, rr_offset_by_family=None):
    """Plan the queue.

    Returns a list of tuples:
        (list_of_rows, target_cell, capable_cells, subparts)

    `rr_offset_by_family` is an optional dict {capable_cells_tuple: int}
    that the dispatcher may pass in to seed the round-robin across
    repeated calls. If None, an internal counter is used.
    """
    tl = CellTimeline()

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
    batch = max(1, int(PRODUCT_BATCH_SIZE))

    for pt in order:
        rows = groups[pt]
        i = 0
        while i < len(rows):
            chunk_rows = rows[i:i + batch]
            i += batch
            n = len(chunk_rows)

            caps = tuple(capable_cells(pt))
            if not caps:
                continue

            idx = family_rr.get(caps, 0)
            hint = (list(caps[idx % len(caps):])
                    + list(caps[:idx % len(caps)]))
            family_rr[caps] = idx + 1

            result = _plan_chunk(pt, n, tl, preferred_cells=hint)
            if result is None:
                continue
            cell, subparts = result
            out.append((chunk_rows, cell, list(caps), subparts))

    # Return planning + new RR state so the caller can persist it.
    return out, family_rr