"""Process knowledge: piece transformations and per-machine tool capabilities.

This module mirrors Tables 1 and 3 of the project PDF.  Recipes for final
products are NOT hardcoded; instead, the planner derives them dynamically
from these primitives.
"""

# Raw piece IDs the loader pushes into W1
RAW_WOOD = 1
RAW_METAL = 2

# Piece name <-> numeric ID (Table 2 of the PDF)
PIECE_ID = {
    "Wood": 1, "Metal": 2,
    "RtopW": 3, "StopW": 4, "LegW": 5,
    "RtopM": 6, "StopM": 7, "LegM": 8,
    "RWW": 9, "SWW": 10, "RWM": 11, "SWM": 12,
    "RMM": 13, "SMM": 14,
}
ID_TO_PIECE = {v: k for k, v in PIECE_ID.items()}

# Final products (the only ones the ERP/clients can order)
FINAL_PRODUCTS = {"RWW", "SWW", "RWM", "SWM", "RMM", "SMM"}

# Single-input transformations from Table 3 (raw + tool -> product).
# A list of dicts so multiple ways can exist for the same product (e.g.
# LegW from T3 OR T10, LegM from T5 OR T11).
SINGLE_TRANSFORM = [
    {"in": "Wood",  "out": "RtopW", "tool": 1,  "time": 30},
    {"in": "Wood",  "out": "StopW", "tool": 2,  "time": 20},
    {"in": "Wood",  "out": "LegW",  "tool": 3,  "time": 10},
    {"in": "Wood",  "out": "LegW",  "tool": 10, "time": 30},
    {"in": "Metal", "out": "RtopM", "tool": 4,  "time": 35},
    {"in": "Metal", "out": "StopM", "tool": 6,  "time": 25},
    {"in": "Metal", "out": "LegM",  "tool": 5,  "time": 30},
    {"in": "Metal", "out": "LegM",  "tool": 11, "time": 40},
]

# Assembly transformations: a top + 2 legs -> final product.
# Each entry lists the required inputs (with counts) and the assembly op.
ASSEMBLY = [
    {"out": "RWW", "top": "RtopW", "leg": "LegW", "tool": 8, "time": 10},
    {"out": "SWW", "top": "StopW", "leg": "LegW", "tool": 8, "time": 10},
    {"out": "RWM", "top": "RtopW", "leg": "LegM", "tool": 9, "time": 10},
    {"out": "SWM", "top": "StopW", "leg": "LegM", "tool": 9, "time": 10},
    {"out": "RMM", "top": "RtopM", "leg": "LegM", "tool": 8, "time": 10},
    {"out": "SMM", "top": "StopM", "leg": "LegM", "tool": 8, "time": 10},
]

# Tool availability per (cell, machine_slot).  Machine slot is 1, 2 or 3
# (M1, M2, M3 within the cell).  Mirrors PLC_PRG instantiation in CODESYS.
# Cells 1 and 2 carry the "wood/metal shaping" tool families on M1/M2 and
# the assembly tools on M3; cells 3 and 4 are the metal-leg variants.
CELL_TOOLS = {
    1: {1: {1, 2, 3}, 2: {1, 2, 3}, 3: {8, 9, 11}},
    2: {1: {1, 2, 3}, 2: {1, 2, 3}, 3: {8, 9, 10}},
    3: {1: {4, 5, 6}, 2: {4, 5, 6}, 3: {8, 9, 11}},
    4: {1: {4, 5, 6}, 2: {4, 5, 6}, 3: {8, 9, 10}},
}

# Time penalty for swapping any two tools on a machine (Table 1 footnote).
TOOL_CHANGE_TIME = 30


def find_single(out_piece: str):
    """Return all viable (raw, tool, time) ways to produce `out_piece`."""
    return [t for t in SINGLE_TRANSFORM if t["out"] == out_piece]


def find_assembly(out_piece: str):
    """Return the assembly entry that yields `out_piece`, or None."""
    for a in ASSEMBLY:
        if a["out"] == out_piece:
            return a
    return None


def raw_for(piece: str) -> str | None:
    """Walk back the transformation graph until a raw material is found."""
    if piece in ("Wood", "Metal"):
        return piece
    s = find_single(piece)
    if s:
        return s[0]["in"]
    return None