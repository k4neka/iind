"""Minimal persistence for the MES."""
import json
import time
from contextlib import contextmanager
import psycopg2, psycopg2.extras
from psycopg2 import pool

from config import DB_CONFIG

_pool = None
_STALE_ERRORS = (psycopg2.OperationalError, psycopg2.InterfaceError)


def _fresh_conn(retries: int = 3, delay: float = 1.0):
    last_exc = None
    for attempt in range(retries):
        conn = _pool.getconn()
        if conn.closed:
            _pool.putconn(conn, close=True)
            time.sleep(delay)
            continue
        try:
            conn.cursor().execute("SELECT 1")
            conn.rollback()
            return conn
        except _STALE_ERRORS as exc:
            last_exc = exc
            print(f"[db] stale connection (attempt {attempt+1}/{retries})")
            try:
                _pool.putconn(conn, close=True)
            except Exception:
                pass
            time.sleep(delay)
    raise psycopg2.OperationalError(
        f"Could not obtain a live DB connection after {retries} attempts"
    ) from last_exc


def init_db():
    global _pool
    _pool = psycopg2.pool.ThreadedConnectionPool(1, 8, **DB_CONFIG)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS pending_pieces (
            id             SERIAL PRIMARY KEY,
            order_id       BIGINT,
            order_line_id  BIGINT,
            piece_type     TEXT NOT NULL,
            status         TEXT DEFAULT 'QUEUED',
            assigned_cell  INT,
            init_piece_id  INT,
            wire_piece_id  INT,          -- INT16-safe PLC PieceID (g_Done key)
            real_cost      NUMERIC DEFAULT 0,
            started_at     TIMESTAMPTZ,   -- enqueued (mark_dispatched) time
            cell_started_at TIMESTAMPTZ,  -- first subpart accepted by the cell
            finished_at    TIMESTAMPTZ,
            created_at     TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS production_timing (
            id              SERIAL PRIMARY KEY,
            piece_type      TEXT NOT NULL,
            cell            INT  NOT NULL,
            preceding_type  TEXT,
            actual_seconds  NUMERIC NOT NULL,
            recorded_at     TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS consumed_messages (
            message_id  TEXT PRIMARY KEY,
            consumed_at TIMESTAMPTZ DEFAULT NOW()
        );

        -- Backward-compatible migrations for older DBs.
        ALTER TABLE pending_pieces
            ADD COLUMN IF NOT EXISTS wire_piece_id INT;
        ALTER TABLE pending_pieces
            ADD COLUMN IF NOT EXISTS cell_started_at TIMESTAMPTZ;

        -- machine_occupancy was removed with CostTracker (v2 Item 1). Drop it
        -- so the schema stays clean; the original definition is preserved in
        -- the comment below in case it is ever wanted back:
        --   CREATE TABLE IF NOT EXISTS machine_occupancy (
        --       id          SERIAL PRIMARY KEY,
        --       cell        INT NOT NULL,
        --       piece_id    INT NOT NULL,
        --       entered_at  TIMESTAMPTZ NOT NULL,
        --       left_at     TIMESTAMPTZ,
        --       duration_s  NUMERIC
        --   );
        DROP TABLE IF EXISTS machine_occupancy;
        """)
        conn.commit()


@contextmanager
def get_conn():
    conn = _fresh_conn()
    try:
        yield conn
    except _STALE_ERRORS:
        if not conn.closed:
            try: conn.close()
            except Exception: pass
        try: _pool.putconn(conn, close=True)
        except Exception: pass
        raise
    except Exception:
        if not conn.closed:
            try: conn.rollback()
            except Exception: pass
        raise
    finally:
        try:
            if conn.closed:
                _pool.putconn(conn, close=True)
            else:
                _pool.putconn(conn)
        except Exception:
            pass


def enqueue_piece(order_id, order_line_id, piece_type):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO pending_pieces(order_id, order_line_id, piece_type)
               VALUES(%s,%s,%s) RETURNING id""",
            (order_id, order_line_id, piece_type))
        pid = cur.fetchone()[0]; conn.commit(); return pid


def queued_pieces():
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM pending_pieces WHERE status='QUEUED' "
                    "ORDER BY id")
        return cur.fetchall()


def mark_dispatched(piece_pk, cell, init_piece_id, wire_piece_id=None):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""UPDATE pending_pieces
                          SET status='IN_PROGRESS', assigned_cell=%s,
                              init_piece_id=%s, wire_piece_id=%s,
                              started_at=NOW()
                        WHERE id=%s""",
                    (cell, init_piece_id, wire_piece_id, piece_pk))
        conn.commit()


def mark_cell_started(piece_pk, ts):
    """Stamp when the piece's first subpart was accepted by its cell (it
    physically started production), as opposed to when it was enqueued.
    Timing samples use this so queue-wait time is excluded (v3 Bug 7).
    Only sets it once (the first subpart)."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE pending_pieces SET cell_started_at=%s "
                    "WHERE id=%s AND cell_started_at IS NULL",
                    (ts, piece_pk))
        conn.commit()


def mark_completed(piece_pk, real_cost):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""UPDATE pending_pieces
                          SET status='COMPLETED', real_cost=%s,
                              finished_at=NOW()
                        WHERE id=%s""", (real_cost, piece_pk))
        conn.commit()


def piece_by_wire_id(wire_id):
    """Resolve a g_Done ParentID (a wire PieceID from the shared generator)
    back to its pending_pieces row.

    The PLC completion buffer carries the wire ParentID we wrote at dispatch,
    NOT the DB serial id (which would overflow INT16). We must report the
    FINAL product type to the ERP, not the shaped-top InitPiece (which would
    wrongly log a 'RtopW' completion instead of 'RWM'). `status`/`finished_at`
    let the consumer dedupe a re-reported completion (§7.1);
    `assigned_cell`/`started_at` feed the production-timing sample. Picks the
    most recent matching row in case the generator has wrapped past 30000."""
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id, piece_type, order_id, order_line_id, "
                    "status, finished_at, assigned_cell, started_at, "
                    "cell_started_at "
                    "FROM pending_pieces WHERE wire_piece_id=%s "
                    "ORDER BY id DESC LIMIT 1", (wire_id,))
        return cur.fetchone()


# ---------- production timing (replaces machine-occupancy costing) ----------

def add_timing_sample(piece_type, cell, preceding_type, actual_seconds):
    """Record one measured production time for `piece_type` on `cell`.

    `preceding_type` is the piece type produced just before this one on the
    same cell (NULL if unknown), so a later analysis can see tool-change
    savings for back-to-back same-type runs."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO production_timing
                   (piece_type, cell, preceding_type, actual_seconds)
               VALUES(%s,%s,%s,%s)""",
            (piece_type, int(cell), preceding_type, float(actual_seconds)))
        conn.commit()


def get_timing_samples(piece_type, cell, limit=30):
    """The most recent `limit` `actual_seconds` samples for (piece_type,
    cell), newest first. Returns a list of dicts."""
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT piece_type, cell, preceding_type, actual_seconds,
                      recorded_at
               FROM production_timing
               WHERE piece_type=%s AND cell=%s
               ORDER BY recorded_at DESC LIMIT %s""",
            (piece_type, int(cell), int(limit)))
        return cur.fetchall()


def distinct_timing_keys():
    """Every (piece_type, cell) pair that has at least one timing sample."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT piece_type, cell FROM production_timing")
        return [(r[0], int(r[1])) for r in cur.fetchall()]


def last_completed_on_cell(cell, before):
    """The previous COMPLETED piece on `cell`, finished before `before`
    (a TIMESTAMPTZ — pass this piece's `started_at`). Returns the row dict
    or None when there is no earlier piece."""
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT piece_type FROM pending_pieces
               WHERE assigned_cell=%s AND status='COMPLETED'
                 AND finished_at IS NOT NULL AND finished_at < %s
               ORDER BY finished_at DESC LIMIT 1""",
            (int(cell), before))
        return cur.fetchone()


def push_timing_to_erp(piece_type, cell, mean_s, n, min_s, max_s):
    """Publish one timing aggregate into the ERP's sim_state table so the
    ERP planner can read measured production times at replan() time.

    Cross-schema write: the MES pool's search_path is db_mes, so the target
    is fully qualified as db_erp.sim_state (same PostgreSQL server/user).
    Key namespace `timing:{type}:{cell}` is distinct from the clock's
    `sim_base_day` key. Guarded by the caller; a missing ERP schema/table
    just means the ERP hasn't booted yet and we retry next cycle."""
    key = f"timing:{piece_type}:{cell}"
    value = json.dumps({"mean_s": round(float(mean_s), 2), "n": int(n),
                        "min_s": round(float(min_s), 2),
                        "max_s": round(float(max_s), 2)})
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO db_erp.sim_state(key, value) VALUES(%s,%s)
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
            (key, value))
        conn.commit()


def is_message_consumed(message_id: str) -> bool:
    # True if we've already processed a material_load with this id.
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM consumed_messages WHERE message_id=%s",
                    (message_id,))
        return cur.fetchone() is not None


def mark_message_consumed(message_id: str):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO consumed_messages(message_id) VALUES(%s) "
            "ON CONFLICT (message_id) DO NOTHING", (message_id,)
        )
        conn.commit()