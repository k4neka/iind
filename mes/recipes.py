"""Piece recipes — transformation sequences (from Table 3 of the PDF).

Each final piece is produced by chaining a top + 2 legs + an assembly op.
An operation is (raw_type, produced_type, tool, op_time_sec).
"""

# Single-step transformations: (raw, product, tool, seconds)
TRANSFORMATIONS = {
    ("Wood",  "RtopW"): (1, 30),
    ("Wood",  "StopW"): (2, 20),
    ("Wood",  "LegW"):  (3, 10),
    ("Wood",  "LegW_alt"): (10, 30),
    ("Metal", "RtopM"): (4, 35),
    ("Metal", "StopM"): (6, 25),
    ("Metal", "LegM"):  (5, 30),
    ("Metal", "LegM_alt"): (11, 40),

    ("RtopW", "RWW"): (8, 10),   # + 2 LegW
    ("StopW", "SWW"): (8, 10),   # + 2 LegW
    ("RtopW", "RWM"): (9, 10),   # + 2 LegM
    ("StopW", "SWM"): (9, 10),   # + 2 LegM
    ("RtopM", "RMM"): (8, 10),   # + 2 LegM
    ("StopM", "SMM"): (8, 10),   # + 2 LegM
}


# Build the full recipe (list of operations) for each final product.
# Each op is dict: {"raw": str, "out": str, "tool": int, "time_s": int,
#                   "machine_kind": "single"|"assembly"}
def _op(raw, out, kind="single"):
    tool, t = TRANSFORMATIONS[(raw, out)]
    return {"raw": raw, "out": out, "tool": tool, "time_s": t,
            "machine_kind": kind}


RECIPES = {
    "RWW": [
        _op("Wood", "RtopW"),
        _op("Wood", "LegW"),
        _op("Wood", "LegW"),
        _op("RtopW", "RWW", kind="assembly"),
    ],
    "SWW": [
        _op("Wood", "StopW"),
        _op("Wood", "LegW"),
        _op("Wood", "LegW"),
        _op("StopW", "SWW", kind="assembly"),
    ],
    "RWM": [
        _op("Wood",  "RtopW"),
        _op("Metal", "LegM"),
        _op("Metal", "LegM"),
        _op("RtopW", "RWM", kind="assembly"),
    ],
    "SWM": [
        _op("Wood",  "StopW"),
        _op("Metal", "LegM"),
        _op("Metal", "LegM"),
        _op("StopW", "SWM", kind="assembly"),
    ],
    "RMM": [
        _op("Metal", "RtopM"),
        _op("Metal", "LegM"),
        _op("Metal", "LegM"),
        _op("RtopM", "RMM", kind="assembly"),
    ],
    "SMM": [
        _op("Metal", "StopM"),
        _op("Metal", "LegM"),
        _op("Metal", "LegM"),
        _op("StopM", "SMM", kind="assembly"),
    ],
}