"""Persistent per-cell tool state.

The MES knows exactly which ops it sends to each cell, so it can track the
tool mounted in every (cell, machine_slot) and carry that state forward
across dispatches. This drives more accurate tool-change estimates in the
optimiser (CellTimeline.tool is seeded from this snapshot instead of
starting from None each batch) and cheaper back-to-back same-type dispatch.

Persistence (TASK 6): the currently-mounted tool per machine is written
through to db_mes.machine_tool_state on every change and loaded back on
startup, so it survives an MES restart. A PLC reconnect restores the known
startup tools (the PLC restarted, so its machines really are at the defaults)
and writes that back to the DB too.

`apply_ops` doubles as the dispatch-time statistics hook (TASK 1): it returns
one record per real op — {cell, slot, tool, time_s, out, tool_change} — where
`tool_change` is computed against the tool mounted just before the op. The
dispatcher persists these on the piece's row so they can be committed to the
statistics tables when the piece completes.
"""

# Known InitTool per (cell, slot) from PLC_PRG FB_Init args:
#   Cell_1: M1=1, M2=1, M3=8     Cell_2: M1=1, M2=1, M3=8
#   Cell_3: M1=4, M2=4, M3=8     Cell_4: M1=4, M2=4, M3=8
_INIT_TOOLS: dict[tuple[int, int], int] = {
    (1, 1): 1, (1, 2): 1, (1, 3): 8,
    (2, 1): 1, (2, 2): 1, (2, 3): 8,
    (3, 1): 4, (3, 2): 4, (3, 3): 8,
    (4, 1): 4, (4, 2): 4, (4, 3): 8,
}


class ToolStateTracker:
    def __init__(self, load_fn=None, save_fn=None, save_all_fn=None):
        """`load_fn`/`save_fn`/`save_all_fn` are optional DB hooks (TASK 6):
          * load_fn() -> {(cell, slot): tool}  (called once on startup)
          * save_fn(cell, slot, tool)          (write-through on every change)
          * save_all_fn({(cell, slot): tool})  (bulk write on reset)
        When omitted (e.g. in unit tests) the tracker is pure in-memory and
        behaves exactly as before."""
        self._save_fn = save_fn
        self._save_all_fn = save_all_fn
        self._state: dict[tuple[int, int], int | None] = dict(_INIT_TOOLS)
        if load_fn is not None:
            try:
                stored = load_fn() or {}
            except Exception as e:
                print(f"[tools] load failed, using startup defaults: {e}")
                stored = {}
            # Seed from the DB where present; fall back to _INIT_TOOLS per slot.
            for key, tool in stored.items():
                if key in self._state and tool is not None:
                    self._state[key] = int(tool)
            # Persist the merged startup state so a fresh DB gets seeded.
            self._save_all(self._state)

    # ---- persistence helpers -------------------------------------------

    def _save(self, cell, slot, tool):
        if self._save_fn is None:
            return
        try:
            self._save_fn(cell, slot, tool)
        except Exception as e:
            print(f"[tools] save_tool_state({cell},{slot},{tool}) failed: {e}")

    def _save_all(self, mapping):
        if self._save_all_fn is None:
            return
        try:
            self._save_all_fn(mapping)
        except Exception as e:
            print(f"[tools] save_all_tool_state failed: {e}")

    # ---- state transitions ---------------------------------------------

    def reset(self):
        """Call on OPC-UA reconnect — the PLC may have restarted, so the
        live tool state is unknown; reinit to the known startup defaults and
        write that back to the DB."""
        self._state = dict(_INIT_TOOLS)
        self._save_all(self._state)

    def apply_ops(self, cell: int, ops: list[dict]):
        """Update after a subpart is accepted by the cell, returning per-op
        statistics records. Only real shaping/assembly ops (tool != 0) mount a
        tool; M3 no-op park ops (tool 0) leave the slot untouched and emit no
        record. Each returned record is
        {cell, slot, tool, time_s, out, tool_change}, with `tool_change` True
        iff the op's tool differs from the one mounted just before it."""
        records = []
        for op in ops:
            if op.get("cell") != cell:
                continue
            tool = int(op.get("tool", 0))
            slot = op.get("machine")
            if tool == 0 or not slot:
                continue
            before = self._state.get((cell, slot))
            change = before is not None and before != tool
            self._state[(cell, slot)] = tool
            if change or before is None:
                self._save(cell, slot, tool)
            records.append({
                "cell": cell, "slot": int(slot), "tool": tool,
                "time_s": float(op.get("op_time_s", 0) or 0),
                "out": op.get("out"),
                "tool_change": bool(change),
            })
        return records

    def snapshot(self, cell: int) -> dict[int, int | None]:
        return {s: self._state.get((cell, s)) for s in (1, 2, 3)}

    def all_cells_snapshot(self) -> dict[int, dict[int, int | None]]:
        return {c: self.snapshot(c) for c in (1, 2, 3, 4)}
