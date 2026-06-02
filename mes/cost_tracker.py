"""Tracks machine occupancy → real production cost, and notifies the ERP."""
from datetime import datetime, timezone

from config import MACHINE_RATE_PER_SEC
from database import (in_progress_by_initpiece, mark_completed,
                      log_machine_event)


class CostTracker:
    """When a piece enters a machine we record entry time; when it leaves we
    compute occupancy cost, accumulate it into the piece, and (for the
    assembly/last machine) publish a COMPLETED event to the ERP."""

    def __init__(self, mqtt_publish_status, on_completed=None):
        self.mqtt_publish_status = mqtt_publish_status
        # Optional hook fired with the completed pending_pieces row, used
        # to auto-route the finished product back to WH1.
        self.on_completed = on_completed
        # _open[(cell, piece_id)] = entered_at (epoch seconds)
        self._open: dict[tuple[int, int], float] = {}
        # accumulated cost per (cell, piece_id) so far
        self._cost_acc: dict[tuple[int, int], float] = {}

        # Safety: if Reg[i] never returns to 0 (PLC may not clear it yet),
        # auto-close machine occupancy after this many seconds.
        self._max_machine_seconds = 600  # 10 minutes hard cap

    def machine_entered(self, cell: int, piece_id: int, ts: float):
        self._open[(cell, piece_id)] = ts
        self._cost_acc.setdefault((cell, piece_id), 0.0)
        print(f"[cost] piece_id={piece_id} entered Cell_{cell}")

    def machine_left(self, cell: int, piece_id: int, ts: float):
        key = (cell, piece_id)
        entered_at = self._open.pop(key, None)
        if entered_at is None:
            return
        duration = max(0.0, ts - entered_at)
        added_cost = duration * MACHINE_RATE_PER_SEC
        self._cost_acc[key] = self._cost_acc.get(key, 0.0) + added_cost

        log_machine_event(
            cell=cell,
            piece_id=piece_id,
            entered_at=datetime.fromtimestamp(entered_at, tz=timezone.utc),
            left_at=datetime.fromtimestamp(ts, tz=timezone.utc),
            duration_s=duration,
        )
        print(f"[cost] piece_id={piece_id} left Cell_{cell} "
              f"after {duration:.1f}s (+{added_cost:.2f}€)")

        # Look up the matching pending piece; if it's the assembly cell (the
        # piece was IN_PROGRESS there), consider it COMPLETED.
        row = in_progress_by_initpiece(piece_id, cell)
        if row:
            total_cost = self._cost_acc.pop(key, 0.0)
            mark_completed(row["id"], total_cost)
            self.mqtt_publish_status({
                "order_id":      row["order_id"],
                "order_line_id": row["order_line_id"],
                "piece_db_id":   row["id"],
                "piece_type":    row["piece_type"],
                "status":        "COMPLETED",
                "real_cost":     round(total_cost, 2),
            })

            # Auto-route the finished product back to WH1 via Transfer_Cell.
            if self.on_completed:
                try:
                    self.on_completed(row)
                except Exception as e:
                    print(f"[cost] on_completed hook error: {e}")

    def sweep_stuck(self, now_ts: float):
        """Force-close machine occupancies that have been open too long.

        Needed because the current Codesys project may not reset GVL.Reg[i]
        to 0 when a piece leaves the machine. Without this sweep, the MES
        would think the machine is busy forever and never bill the cost.
        """
        stuck = [
            (key, entered)
            for key, entered in list(self._open.items())
            if now_ts - entered > self._max_machine_seconds
        ]
        for (cell, piece_id), entered in stuck:
            print(f"[cost] WARN: forcing close on Cell_{cell} "
                  f"piece_id={piece_id} (stuck for "
                  f"{now_ts - entered:.0f}s)")
            self.machine_left(cell, piece_id, now_ts)