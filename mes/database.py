"""Minimal persistence for the MES."""
import threading
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
        CREATE TABLE IF NOT EXISTS consumed_messages (
            message_id  TEXT PRIMARY KEY,
            consumed_at TIMESTAMPTZ DEFAULT NOW()
        );
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
                          SET status='COMPLETED', real_cost=%s,
                              finished_at=NOW()
                        WHERE id=%s""", (real_cost, piece_pk))
        conn.commit()


def piece_type_by_id(piece_pk):
    """Return the piece_type (e.g. 'RWM') for a pending_pieces row id.

    Used by the completion buffer consumer: the PLC g_Done buffer carries
    the ParentID (= this row id), and we must report the FINAL product type
    to the ERP, not the shaped-top InitPiece (which would wrongly log a
    'RtopW' completion instead of 'RWM')."""
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT piece_type, order_id, order_line_id "
                    "FROM pending_pieces WHERE id=%s", (piece_pk,))
        return cur.fetchone()


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