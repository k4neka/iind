"""Process knowledge: piece transformations and per-machine tool
capabilities. Mirrors Tables 1, 2 and 3 of the project PDF (v1.1).

Tool families (Table 1 of the PDF):
  - M1a, M1b, M2a, M2b -> T1, T2, T3  (wood shaping)
  - M3a, M3b, M4a, M4b -> T4, T5, T6  (metal shaping)
  - M1c, M3c           -> T8, T9, T11 (assembly + alt LegM)
  - M2c, M4c           -> T8, T9, T10 (assembly + alt LegW)

Note that T10 (alt LegW) and T11 (alt LegM) appear ONLY in the M*c
machines (i.e. the M3 slot inside each cell). They are shaping tools
that happen to live on the assembly machine. Whether to use them is a
scheduling choice the optimiser can later exploit.
"""

RAW_WOOD = 1
RAW_METAL = 2

# Table 2 of the PDF (with Piece IDs added in v1.1).
PIECE_ID = {
    "Wood": 1, "Metal": 2,
    "RtopW": 3, "StopW": 4, "LegW": 5,
    "RtopM": 6, "StopM": 7, "LegM": 8,
    "RWW": 9, "SWW": 10, "RWM": 11, "SWM": 12,
    "RMM": 13, "SMM": 14,
}
ID_TO_PIECE = {v: k for k, v in PIECE_ID.items()}

FINAL_PRODUCTS = {"RWW", "SWW", "RWM", "SWM", "RMM", "SMM"}

# Shaping transformations (Table 3 of the PDF, single-input rows).
# Every entry is a viable way to produce `out` from `in` using `tool`
# for `time` seconds. Multiple rows for the same `out` mean the
# scheduler has alternatives.
SINGLE_TRANSFORM = [
    {"in": "Wood",  "out": "RtopW", "tool": 1,  "time": 30},
    {"in": "Wood",  "out": "StopW", "tool": 2,  "time": 20},
    {"in": "Wood",  "out": "LegW",  "tool": 3,  "time": 10},
    {"in": "Wood",  "out": "LegW",  "tool": 10, "time": 30},  # alt
    {"in": "Metal", "out": "RtopM", "tool": 4,  "time": 35},
    {"in": "Metal", "out": "StopM", "tool": 6,  "time": 25},
    {"in": "Metal", "out": "LegM",  "tool": 5,  "time": 30},
    {"in": "Metal", "out": "LegM",  "tool": 11, "time": 40},  # alt
]

# Assembly transformations (Table 3, three-input rows).
# All final products use T8 except RWM and SWM (which use T9).
ASSEMBLY = [
    {"out": "RWW", "top": "RtopW", "leg": "LegW", "tool": 8, "time": 10},
    {"out": "SWW", "top": "StopW", "leg": "LegW", "tool": 8, "time": 10},
    {"out": "RWM", "top": "RtopW", "leg": "LegM", "tool": 9, "time": 10},
    {"out": "SWM", "top": "StopW", "leg": "LegM", "tool": 9, "time": 10},
    {"out": "RMM", "top": "RtopM", "leg": "LegM", "tool": 8, "time": 10},
    {"out": "SMM", "top": "StopM", "leg": "LegM", "tool": 8, "time": 10},
]

# Tool availability per (cell, machine_slot). Mirrors PLC_PRG.
# Slot 1 = M1a/M1b/etc (first shaping), slot 2 = same family
# (parallel shaping), slot 3 = Mxc (assembly + alt shaping).
CELL_TOOLS = {
    1: {1: {1, 2, 3}, 2: {1, 2, 3}, 3: {8, 9, 11}},
    2: {1: {1, 2, 3}, 2: {1, 2, 3}, 3: {8, 9, 10}},
    3: {1: {4, 5, 6}, 2: {4, 5, 6}, 3: {8, 9, 11}},
    4: {1: {4, 5, 6}, 2: {4, 5, 6}, 3: {8, 9, 10}},
}

# Section 2 of the PDF: "Changing between any two tools takes 30 seconds."
TOOL_CHANGE_TIME = 30


def find_single(out_piece: str):
    """All viable shaping recipes for `out_piece`."""
    return [t for t in SINGLE_TRANSFORM if t["out"] == out_piece]


def find_assembly(out_piece: str):
    for a in ASSEMBLY:
        if a["out"] == out_piece:
            return a
    return None


def raw_for(piece: str) -> str | None:
    if piece in ("Wood", "Metal"):
        return piece
    s = find_single(piece)
    if s:
        return s[0]["in"]
    return None