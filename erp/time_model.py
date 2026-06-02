"""Production-time estimator for cost-benefit supplier decisions.

The ERP must weigh two purchasing strategies for each material:

  * Supplier A: expensive, but lead time 0 (material available now).
  * Supplier B: cheap, but delayed (lead 2-4 days) which can push a piece
    past its deadline and incur a late penalty.

To choose correctly we need to know *when a piece would actually finish*,
which depends on machine processing time. This module estimates that.

Model (mirrors Tables 1-3 of the project PDF):
  * A cell has 3 machine slots: M1, M2 (parallel shaping) and M3
    (assembly, also carries the alt shaping tools T10/T11).
  * We STORE AND TRACK the tool currently mounted in each machine. An op
    only pays TOOL_CHANGE_TIME_S when the next op on that machine needs a
    different tool than the one already mounted.
  * The two legs are shaped in parallel on M1 and M2 (so their cost is
    the longer single-leg path, not the sum). Mixed-material legs are
    shaped on M3 with the alt tool instead.
  * Moving a piece one hop in a clear cell costs TRANSFER_TIME_S; when the
    line is busy (other pieces in flight) transfer time is inflated by
    QUEUE_FACTOR to approximate queueing/waiting behind them.
"""
import math

from config import (TOOL_CHANGE_TIME_S, TRANSFER_TIME_S,
                    CELL_HOPS, QUEUE_FACTOR, SECONDS_PER_DAY)


# Compact recipe table (top, leg, assembly) per final product.
# Each tuple is (tool, op_time_s).
#   * `init_shape` / `init_asm` are the tools mounted at cell startup
#     (cells 1/2 M1,M2 = T1; cells 3/4 M1,M2 = T4; every M3 = T8).
#   * `mixed` marks RWM/SWM, which are built across two cells: a wood top
#     in a wood cell (1/2) + two metal legs in a metal+assembly cell (3/4),
#     with the top transferred W2->W1 before the M3 assembly. These are
#     estimated by _estimate_mixed (the single-cell path does not apply).
DEFAULT_SHAPE_WOOD = 1     # cells 1/2 M1,M2 startup tool
DEFAULT_SHAPE_METAL = 4    # cells 3/4 M1,M2 startup tool
DEFAULT_ASM = 8            # every cell's M3 startup tool

RECIPES = {
    "RWW": {"top": (1, 30), "leg": (3, 10),  "asm": (8, 10),
            "init_shape": 1, "init_asm": 8, "leg_on_m3": False},
    "SWW": {"top": (2, 20), "leg": (3, 10),  "asm": (8, 10),
            "init_shape": 1, "init_asm": 8, "leg_on_m3": False},
    "RWM": {"top": (1, 30), "leg": (5, 30),  "asm": (9, 10), "mixed": True},
    "SWM": {"top": (2, 20), "leg": (5, 30),  "asm": (9, 10), "mixed": True},
    "RMM": {"top": (4, 35), "leg": (5, 30),  "asm": (8, 10),
            "init_shape": 4, "init_asm": 8, "leg_on_m3": False},
    "SMM": {"top": (6, 25), "leg": (5, 30),  "asm": (8, 10),
            "init_shape": 4, "init_asm": 8, "leg_on_m3": False},
}


class MachineToolState:
    """Tracks the tool mounted on each machine slot of one cell so the
    estimator can charge a tool change only when one is actually needed."""

    def __init__(self, init_shape: int, init_asm: int):
        self.tool = {"M1": init_shape, "M2": init_shape, "M3": init_asm}

    def use(self, machine: str, tool: int) -> float:
        """Return the time cost (s) of running `tool` on `machine`,
        i.e. a tool change if the mounted tool differs, then mount it."""
        if self.tool[machine] == tool:
            return 0.0
        self.tool[machine] = tool
        return float(TOOL_CHANGE_TIME_S)


def _estimate_mixed(r: dict, busy: bool) -> float:
    """RWM / SWM: wood top in a wood cell (1/2) and two metal legs in a
    metal+assembly cell (3/4) run *concurrently*; the top is then carried
    W2->W1 by the Transfer_Cell and assembled at the metal cell's M3.

    Critical path = max(top reaching W1, both legs parked) + the M3
    assembly. Legs shape in parallel on M1+M2, so they cost one leg path.
    """
    hop = CELL_HOPS * TRANSFER_TIME_S * (QUEUE_FACTOR if busy else 1.0)

    # Wood cell: M1 starts on T1; shape the top, exit to W2, cross to W1.
    top_tool, top_time = r["top"]
    top_change = 0.0 if top_tool == DEFAULT_SHAPE_WOOD else TOOL_CHANGE_TIME_S
    top_to_w1 = top_change + top_time + hop + hop      # cell exit + corridor

    # Metal cell: M1/M2 start on T4; two legs in parallel = one leg path.
    leg_tool, leg_time = r["leg"]
    leg_change = 0.0 if leg_tool == DEFAULT_SHAPE_METAL else TOOL_CHANGE_TIME_S
    legs_parked = leg_change + leg_time

    # Assembly on the metal cell M3 (starts on T8).
    asm_tool, asm_time = r["asm"]
    asm_change = 0.0 if asm_tool == DEFAULT_ASM else TOOL_CHANGE_TIME_S

    return max(top_to_w1, legs_parked) + asm_change + asm_time


def estimate_production_seconds(piece_type: str, busy: bool = False) -> float:
    """Estimate seconds to flow one `piece_type` through a clear cell.

    `busy=True` inflates transfer time to account for queueing behind
    other pieces already on the line.
    """
    r = RECIPES.get(piece_type)
    if r is None:
        return 0.0
    if r.get("mixed"):
        return _estimate_mixed(r, busy)

    st = MachineToolState(r["init_shape"], r["init_asm"])

    # Top: shaped on M1.
    top_tool, top_time = r["top"]
    t = st.use("M1", top_tool) + top_time

    # Legs: two of them. Either parallel on M1/M2, or both on M3.
    leg_tool, leg_time = r["leg"]
    if r["leg_on_m3"]:
        # Two sequential leg ops on M3 (single machine).
        t += st.use("M3", leg_tool) + leg_time          # leg 1
        t += st.use("M3", leg_tool) + leg_time          # leg 2 (no change)
    else:
        # Parallel on M1 and M2: cost is the longer single-leg path.
        leg_cost = max(st.use("M1", leg_tool) + leg_time,
                       st.use("M2", leg_tool) + leg_time)
        t += leg_cost

    # Assembly on M3.
    asm_tool, asm_time = r["asm"]
    t += st.use("M3", asm_tool) + asm_time

    # Transfers across the line (with queueing inflation when busy).
    transfer = CELL_HOPS * TRANSFER_TIME_S
    if busy:
        transfer *= QUEUE_FACTOR
    t += transfer

    return t


def estimate_production_days(piece_type: str, busy: bool = False) -> int:
    """Production time rounded up to whole sim days (>= 1 for any piece)."""
    secs = estimate_production_seconds(piece_type, busy)
    if secs <= 0:
        return 0
    return max(1, math.ceil(secs / SECONDS_PER_DAY))
