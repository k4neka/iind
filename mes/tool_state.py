"""Persistent per-cell tool state.

The MES knows exactly which ops it sends to each cell, so it can track the
tool mounted in every (cell, machine_slot) and carry that state forward
across dispatches. This drives more accurate tool-change estimates in the
optimiser (CellTimeline.tool is seeded from this snapshot instead of
starting from None each batch) and cheaper back-to-back same-type dispatch.
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
    def __init__(self):
        self._state: dict[tuple[int, int], int | None] = dict(_INIT_TOOLS)

    def reset(self):
        """Call on OPC-UA reconnect — the PLC may have restarted, so the
        live tool state is unknown; reinit to the known startup defaults."""
        self._state = dict(_INIT_TOOLS)

    def apply_ops(self, cell: int, ops: list[dict]):
        """Update after a subpart is accepted by the cell. Only real
        shaping/assembly ops (tool != 0) mount a tool; M3 no-op park ops
        (tool 0) leave the slot untouched."""
        for op in ops:
            if op.get("cell") == cell and int(op.get("tool", 0)) != 0:
                slot = op.get("machine")
                if slot:
                    self._state[(cell, slot)] = int(op["tool"])

    def snapshot(self, cell: int) -> dict[int, int | None]:
        return {s: self._state.get((cell, s)) for s in (1, 2, 3)}

    def all_cells_snapshot(self) -> dict[int, dict[int, int | None]]:
        return {c: self.snapshot(c) for c in (1, 2, 3, 4)}
