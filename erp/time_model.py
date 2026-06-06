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
import json
import math

from config import (TOOL_CHANGE_TIME_S, TRANSFER_TIME_S,
                    CELL_HOPS, QUEUE_FACTOR, SECONDS_PER_DAY)
from database import get_state


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


def _estimate_mixed(r: dict, busy: bool, warm: bool = False) -> float:
    """RWM / SWM: wood top in a wood cell (1/2) and two metal legs in a
    metal+assembly cell (3/4) run *concurrently*; the top is then carried
    W2->W1 by the Transfer_Cell and assembled at the metal cell's M3.

    Critical path = max(top reaching W1, both legs parked) + the M3
    assembly. Legs shape in parallel on M1+M2, so they cost one leg path.
    `warm=True` (same-type predecessor: tools already mounted) zeroes the
    tool-change terms — used to compute the tool-change credit."""
    hop = CELL_HOPS * TRANSFER_TIME_S * (QUEUE_FACTOR if busy else 1.0)

    # Wood cell: M1 starts on T1; shape the top, exit to W2, cross to W1.
    top_tool, top_time = r["top"]
    top_change = 0.0 if (warm or top_tool == DEFAULT_SHAPE_WOOD) \
        else TOOL_CHANGE_TIME_S
    top_to_w1 = top_change + top_time + hop + hop      # cell exit + corridor

    # Metal cell: M1/M2 start on T4; two legs in parallel = one leg path.
    leg_tool, leg_time = r["leg"]
    leg_change = 0.0 if (warm or leg_tool == DEFAULT_SHAPE_METAL) \
        else TOOL_CHANGE_TIME_S
    legs_parked = leg_change + leg_time

    # Assembly on the metal cell M3 (starts on T8).
    asm_tool, asm_time = r["asm"]
    asm_change = 0.0 if (warm or asm_tool == DEFAULT_ASM) \
        else TOOL_CHANGE_TIME_S

    return max(top_to_w1, legs_parked) + asm_change + asm_time


_SLOT_TO_M = {1: "M1", 2: "M2", 3: "M3"}


def _flow_seconds(r: dict, st: "MachineToolState", busy: bool) -> float:
    """Single-cell flow seconds for a non-mixed piece using tool-state `st`."""
    top_tool, top_time = r["top"]
    t = st.use("M1", top_tool) + top_time

    leg_tool, leg_time = r["leg"]
    if r["leg_on_m3"]:
        t += st.use("M3", leg_tool) + leg_time          # leg 1
        t += st.use("M3", leg_tool) + leg_time          # leg 2 (no change)
    else:
        t += max(st.use("M1", leg_tool) + leg_time,
                 st.use("M2", leg_tool) + leg_time)

    asm_tool, asm_time = r["asm"]
    t += st.use("M3", asm_tool) + asm_time

    transfer = CELL_HOPS * TRANSFER_TIME_S
    if busy:
        transfer *= QUEUE_FACTOR
    return t + transfer


def estimate_production_seconds(piece_type: str, busy: bool = False) -> float:
    """Estimate seconds to flow one `piece_type` through a clear cell from
    the cell's STARTUP tools. `busy=True` inflates transfer time for
    queueing behind other pieces."""
    r = RECIPES.get(piece_type)
    if r is None:
        return 0.0
    if r.get("mixed"):
        return _estimate_mixed(r, busy)
    st = MachineToolState(r["init_shape"], r["init_asm"])
    return _flow_seconds(r, st, busy)


def _static_with_tools(piece_type: str, init_map) -> float:
    """Static seconds with a given initial mounted-tool map ({slot: tool}, or
    None = cold cell with all tools unknown so every first op pays a change).
    Only used to compute the tool-change credit (cold vs warm)."""
    r = RECIPES.get(piece_type)
    if r is None:
        return 0.0
    if r.get("mixed"):
        return _estimate_mixed(r, False, warm=(init_map is not None))
    st = MachineToolState(r["init_shape"], r["init_asm"])
    if init_map is None:
        st.tool = {"M1": None, "M2": None, "M3": None}
    else:
        st.tool = {_SLOT_TO_M[s]: init_map.get(s) for s in (1, 2, 3)}
    return _flow_seconds(r, st, False)


def estimate_production_days(piece_type: str, busy: bool = False) -> int:
    """Production time rounded up to whole sim days (>= 1 for any piece)."""
    secs = estimate_production_seconds(piece_type, busy)
    if secs <= 0:
        return 0
    return max(1, math.ceil(secs / SECONDS_PER_DAY))


# ---------------------------------------------------------------------------
# Measured production time (Item 2): real averages recorded by the MES and
# pushed into the ERP sim_state table under key `timing:{type}:{cell}`.
# ---------------------------------------------------------------------------

# Below this many samples the measured mean is too noisy; fall back to the
# static estimator.
_MIN_SAMPLES = 5


def read_timing_stats(piece_type: str, cell: int | None = None):
    """Read the MES-published timing aggregate(s) from ERP sim_state.

    With `cell` given, returns that cell's `{mean_s, n}`; otherwise pools the
    four cells (sample-weighted mean, summed n). Returns {"mean_s","n"}.
    The MES writes these keys every 30 s (see database.push_timing_to_erp)."""
    cells = [cell] if cell else [1, 2, 3, 4]
    weighted, total_n = 0.0, 0
    for c in cells:
        try:
            raw = get_state(f"timing:{piece_type}:{c}")
        except Exception:
            raw = None
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except Exception:
            continue
        n = int(d.get("n", 0))
        if n > 0:
            weighted += float(d.get("mean_s", 0.0)) * n
            total_n += n
    return {"mean_s": (weighted / total_n if total_n else 0.0), "n": total_n}


def expected_seconds(piece_type: str, cell: int | None = None,
                     current_tool_state: dict | None = None,
                     preceding_type: str | None = None,
                     timing_stats: dict | None = None) -> float:
    """Best estimate of seconds to produce one `piece_type` (Item 2/3.3).

      1. If `timing_stats` (or the sim_state aggregate) has >= _MIN_SAMPLES
         samples for (piece_type, cell): use its mean.
      2. Otherwise the static estimator from the cell's startup tools.
      3. Apply a tool-change credit by running the estimator twice — cold
         (all tools unknown) vs warm (`current_tool_state`); credit =
         cold - warm. Computed from the model, never a hard-coded 30/60.

    `preceding_type == piece_type` with no explicit `current_tool_state`
    implies the same-type predecessor left its tools mounted, so the warm
    state is the piece's own tool set."""
    if timing_stats is None:
        timing_stats = read_timing_stats(piece_type, cell)
    if timing_stats and int(timing_stats.get("n", 0)) >= _MIN_SAMPLES:
        base = float(timing_stats["mean_s"])
    else:
        base = estimate_production_seconds(piece_type)

    warm_map = current_tool_state
    if warm_map is None and preceding_type is not None \
            and preceding_type == piece_type:
        warm_map = _own_tool_map(piece_type)
    if warm_map:
        cold = _static_with_tools(piece_type, None)
        warm = _static_with_tools(piece_type, warm_map)
        base = max(0.0, base - max(0.0, cold - warm))
    return base


def _own_tool_map(piece_type: str) -> dict | None:
    """The {slot: tool} a same-type predecessor would leave mounted: M1=top
    tool, M2/M3 the leg/assembly tools the piece uses."""
    r = RECIPES.get(piece_type)
    if r is None or r.get("mixed"):
        return None
    return {1: r["top"][0],
            2: (r["leg"][0] if not r["leg_on_m3"] else r["init_shape"]),
            3: r["asm"][0]}


def expected_production_days(piece_type: str, cell: int | None = None,
                             current_tool_state: dict | None = None,
                             preceding_type: str | None = None,
                             timing_stats: dict | None = None) -> int:
    """Whole sim days for one piece using the measured/estimated time."""
    secs = expected_seconds(piece_type, cell, current_tool_state,
                            preceding_type, timing_stats)
    if secs <= 0:
        return estimate_production_days(piece_type)
    return max(1, math.ceil(secs / SECONDS_PER_DAY))
