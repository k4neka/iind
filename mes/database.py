"""Minimal persistence for the MES."""
import threading
from contextlib import contextmanager
import psycopg2, psycopg2.extras
from psycopg2 import pool

from config import DB_CONFIG

_pool = None


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
    conn = _pool.getconn()
    try:
        yield conn
    except Exception:
        conn.rollback(); raise
    finally:
        _pool.putconn(conn)


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