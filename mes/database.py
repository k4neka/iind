"""Minimal persistence for the MES."""
import threading
import time
from contextlib import contextmanager
import psycopg2, psycopg2.extras
from psycopg2 import pool

from config import DB_CONFIG

_pool = None

# Errors that mean the connection was dropped server-side and we should retry.
_STALE_ERRORS = (psycopg2.OperationalError, psycopg2.InterfaceError)


def _fresh_conn(retries: int = 3, delay: float = 1.0):
    """Pull a connection from the pool and verify it is alive.

    psycopg2's ThreadedConnectionPool recycles connections, but the remote
    PostgreSQL server (especially one behind a firewall or with a short
    idle-timeout) may have already closed them.  conn.closed only reflects
    the *local* state, so we probe with a cheap round-trip query instead.
    If the probe fails we close the broken connection and ask the pool for a
    fresh one, retrying up to `retries` times.
    """
    last_exc = None
    for attempt in range(retries):
        conn = _pool.getconn()
        if conn.closed:
            _pool.putconn(conn, close=True)
            time.sleep(delay)
            continue
        try:
            conn.cursor().execute("SELECT 1")
            conn.rollback()          # leave the connection clean
            return conn
        except _STALE_ERRORS as exc:
            last_exc = exc
            print(f"[db] stale connection detected (attempt {attempt+1}/{retries}), discarding.")
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
            id            SERIAL PRIMARY KEY,
            order_id      BIGINT,
            order_line_id BIGINT,
            piece_type    TEXT NOT NULL,
            status        TEXT DEFAULT 'QUEUED',
            assigned_cell INT,
            init_piece_id INT,
            real_cost     NUMERIC DEFAULT 0,
            started_at    TIMESTAMPTZ,
            finished_at   TIMESTAMPTZ,
            created_at    TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS machine_occupancy (
            id          SERIAL PRIMARY KEY,
            cell        INT NOT NULL,
            piece_id    INT NOT NULL,
            entered_at  TIMESTAMPTZ NOT NULL,
            left_at     TIMESTAMPTZ,
            duration_s  NUMERIC
        );
        """)
        conn.commit()


@contextmanager
def get_conn():
    """Yield a live, pool-managed psycopg2 connection.

    Strategy:
      1. _fresh_conn() probes with SELECT 1, retrying on stale connections.
      2. If the query inside the `with` block itself raises a network error
         (server dropped us mid-query) we close the connection and re-raise
         so the caller can decide whether to retry.
      3. Non-network exceptions get a rollback; the connection is returned
         to the pool.
      4. Dead connections are always returned with close=True so the pool
         drops them instead of re-using them.
    """
    conn = _fresh_conn()
    try:
        yield conn
    except _STALE_ERRORS:
        # Mid-query network failure — kill the connection and propagate.
        if not conn.closed:
            try:
                conn.close()
            except Exception:
                pass
        try:
            _pool.putconn(conn, close=True)
        except Exception:
            pass
        raise
    except Exception:
        # Application-level error — rollback and return to pool.
        if not conn.closed:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        # Return connection to pool; broken ones are discarded.
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
        cur.execute("SELECT * FROM pending_pieces WHERE status='QUEUED' ORDER BY id")
        return cur.fetchall()


def mark_dispatched(piece_pk, cell, init_piece_id):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""UPDATE pending_pieces
                          SET status='IN_PROGRESS', assigned_cell=%s,
                              init_piece_id=%s, started_at=NOW()
                        WHERE id=%s""",
                    (cell, init_piece_id, piece_pk))
        conn.commit()


def mark_completed(piece_pk, real_cost):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""UPDATE pending_pieces
                          SET status='COMPLETED', real_cost=%s, finished_at=NOW()
                        WHERE id=%s""", (real_cost, piece_pk))
        conn.commit()


def in_progress_by_initpiece(init_piece_id, cell):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""SELECT * FROM pending_pieces
                        WHERE status='IN_PROGRESS' AND init_piece_id=%s
                          AND assigned_cell=%s
                        ORDER BY started_at ASC LIMIT 1""",
                    (init_piece_id, cell))
        return cur.fetchone()


def log_machine_event(cell, piece_id, entered_at, left_at, duration_s):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""INSERT INTO machine_occupancy
                       (cell, piece_id, entered_at, left_at, duration_s)
                       VALUES(%s,%s,%s,%s,%s)""",
                    (cell, piece_id, entered_at, left_at, duration_s))
        conn.commit()