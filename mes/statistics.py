"""Machine statistics derived from SCHEDULED op times (Req 4.3, TASK 1).

The MES cannot read machine occupancy/working state from the PLC — no such
signal exists and none can be added. Instead, every statistic is derived from
the ops the MES itself sent to each cell: when a chunk is dispatched the MES
knows, for every op, the tuple ``(cell, slot, tool, time_s, out)`` and whether
that op required a tool change (computed against the live ToolStateTracker).
Those per-op records are persisted on the piece's row at dispatch and only
COMMITTED into the statistics totals when the piece is tagged COMPLETED — a
dispatched piece that never finishes (PLC restart, scrap) contributes nothing.

Four tables hold the accumulators (all in db_mes):
  * machine_stats          — operating/tool-change seconds, #changes, #ops
  * machine_tool_seconds   — cumulative operating seconds per mounted tool
  * machine_piece_counts   — histogram of each op's OUTPUT piece per machine
  * machine_tool_state     — currently-mounted tool (kept by ToolStateTracker)

Occupation % is intentionally NOT stored: it is computed at read time as
operating/elapsed and (operating+tool_change)/elapsed, where elapsed is the
sim time so far (sim_base_day * SECONDS_PER_DAY). See read_machine_report().
"""
import psycopg2.extras

from database import get_conn
from transformations import TOOL_CHANGE_TIME


class StatisticsRecorder:
    """Commits a completed piece's dispatched ops into the four stats tables.

    `record_completion(ops)` is called by the MES completion_loop after a piece
    is marked COMPLETED, with the ops read back from pending_pieces.dispatched_ops
    (each op = {cell, slot, tool, time_s, out, tool_change})."""

    def record_completion(self, ops):
        """Accumulate one finished piece's ops into the stats tables in a single
        transaction. `ops` is the list persisted at dispatch; a missing/empty
        list (unknown wire, pre-rework piece) is a no-op."""
        if not ops:
            return
        with get_conn() as conn:
            cur = conn.cursor()
            for op in ops:
                try:
                    cell = int(op["cell"])
                    slot = int(op["slot"])
                    tool = int(op["tool"])
                except (KeyError, TypeError, ValueError):
                    continue
                time_s = float(op.get("time_s", 0) or 0)
                change = bool(op.get("tool_change"))
                chg_s = TOOL_CHANGE_TIME if change else 0
                out = op.get("out")

                cur.execute(
                    """INSERT INTO machine_stats
                           (cell, slot, operating_seconds, tool_change_seconds,
                            tool_changes, pieces_operated, updated_at)
                       VALUES (%s,%s,%s,%s,%s,1,NOW())
                       ON CONFLICT (cell, slot) DO UPDATE SET
                           operating_seconds  = machine_stats.operating_seconds
                                                + EXCLUDED.operating_seconds,
                           tool_change_seconds= machine_stats.tool_change_seconds
                                                + EXCLUDED.tool_change_seconds,
                           tool_changes       = machine_stats.tool_changes
                                                + EXCLUDED.tool_changes,
                           pieces_operated    = machine_stats.pieces_operated
                                                + EXCLUDED.pieces_operated,
                           updated_at         = NOW()""",
                    (cell, slot, time_s, chg_s, 1 if change else 0))

                cur.execute(
                    """INSERT INTO machine_tool_seconds (cell, slot, tool, seconds)
                       VALUES (%s,%s,%s,%s)
                       ON CONFLICT (cell, slot, tool) DO UPDATE SET
                           seconds = machine_tool_seconds.seconds
                                     + EXCLUDED.seconds""",
                    (cell, slot, tool, time_s))

                if out:
                    cur.execute(
                        """INSERT INTO machine_piece_counts
                               (cell, slot, piece_type, count)
                           VALUES (%s,%s,%s,1)
                           ON CONFLICT (cell, slot, piece_type) DO UPDATE SET
                               count = machine_piece_counts.count + 1""",
                        (cell, slot, out))
            conn.commit()

    # ---- read helpers (dashboard / report) -----------------------------

    def read_machine_stats(self):
        with get_conn() as conn:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT * FROM machine_stats ORDER BY cell, slot")
            return cur.fetchall()

    def read_tool_seconds(self):
        with get_conn() as conn:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT * FROM machine_tool_seconds "
                        "ORDER BY cell, slot, tool")
            return cur.fetchall()

    def read_piece_counts(self):
        with get_conn() as conn:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT * FROM machine_piece_counts "
                        "ORDER BY cell, slot, piece_type")
            return cur.fetchall()

    def read_machine_report(self, elapsed_seconds):
        """Build a per-machine report with both occupation variants computed
        against `elapsed_seconds` (sim time so far). Returns a list of dicts —
        used by the dashboard service. `elapsed_seconds<=0` reports occ=0."""
        elapsed = float(elapsed_seconds or 0)
        stats = {(r["cell"], r["slot"]): r for r in self.read_machine_stats()}
        report = []
        for (cell, slot), r in sorted(stats.items()):
            op_s = float(r["operating_seconds"])
            ch_s = float(r["tool_change_seconds"])
            occ_op = (op_s / elapsed) if elapsed > 0 else 0.0
            occ_busy = ((op_s + ch_s) / elapsed) if elapsed > 0 else 0.0
            report.append({
                "cell": cell, "slot": slot,
                "operating_seconds": op_s,
                "tool_change_seconds": ch_s,
                "tool_changes": int(r["tool_changes"]),
                "pieces_operated": int(r["pieces_operated"]),
                "occupation_operating": round(min(1.0, occ_op), 4),
                "occupation_busy": round(min(1.0, occ_busy), 4),
            })
        return report
