"""PostgreSQL persistence layer with crash recovery."""
import time
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from psycopg2 import pool

from config import DB_CONFIG

_pool = None

# Errors that indicate a server-side connection drop (need retry, not rollback).
_STALE_ERRORS = (psycopg2.OperationalError, psycopg2.InterfaceError)


def init_db():
    """Create the connection pool and schema if missing."""
    global _pool
    _pool = psycopg2.pool.ThreadedConnectionPool(1, 10, **DB_CONFIG)

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS client_orders (
            id              SERIAL PRIMARY KEY,
            client_name     TEXT      NOT NULL,
            nif             BIGINT    NOT NULL,
            order_id        BIGINT    NOT NULL,
            received_day    INTEGER   NOT NULL,
            status          TEXT      DEFAULT 'PENDING'
        );

        CREATE TABLE IF NOT EXISTS order_lines (
            id              SERIAL PRIMARY KEY,
            client_order_id INTEGER   NOT NULL REFERENCES client_orders(id) ON DELETE CASCADE,
            piece_type      TEXT      NOT NULL,
            quantity        INTEGER   NOT NULL,
            ddate           INTEGER   NOT NULL,       -- absolute sim day
            penalty         NUMERIC   NOT NULL,
            produced        INTEGER   DEFAULT 0,
            delivered       INTEGER   DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS production_plan (
            id              SERIAL PRIMARY KEY,
            sim_day         INTEGER   NOT NULL,
            piece_type      TEXT      NOT NULL,
            quantity        INTEGER   NOT NULL,
            order_line_id   INTEGER REFERENCES order_lines(id) ON DELETE CASCADE,
            dispatched      BOOLEAN   DEFAULT FALSE
        );

        CREATE TABLE IF NOT EXISTS purchase_plan (
            id              SERIAL PRIMARY KEY,
            order_day       INTEGER   NOT NULL,
            arrival_day     INTEGER   NOT NULL,
            supplier        TEXT      NOT NULL,
            material        TEXT      NOT NULL,
            quantity        INTEGER   NOT NULL,
            cost            NUMERIC   NOT NULL,
            placed          BOOLEAN   DEFAULT FALSE
        );

        CREATE TABLE IF NOT EXISTS inventory (
            piece_type TEXT PRIMARY KEY,
            quantity   INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS mes_status_log (
            id      SERIAL PRIMARY KEY,
            ts      DOUBLE PRECISION NOT NULL,
            topic   TEXT NOT NULL,
            payload TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS production_costs (
            id              SERIAL PRIMARY KEY,
            order_line_id   INTEGER REFERENCES order_lines(id) ON DELETE CASCADE,
            sim_day         INTEGER NOT NULL,
            raw_cost        NUMERIC DEFAULT 0,
            penalty_cost    NUMERIC DEFAULT 0,
            other_cost      NUMERIC DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS sim_state (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        """)

        for p in ("Wood", "Metal", "RWW", "SWW", "RWM", "SWM", "RMM", "SMM"):
            cur.execute(
                "INSERT INTO inventory(piece_type, quantity) VALUES(%s, 0) "
                "ON CONFLICT (piece_type) DO NOTHING",
                (p,),
            )
        conn.commit()


@contextmanager
def get_conn():
    """Yield a live, pool-managed psycopg2 connection.

    The remote PostgreSQL server may silently drop idle connections (firewall /
    idle-timeout). conn.closed only reflects the *local* state, so we probe
    with SELECT 1 before handing the connection to the caller.  If the probe
    fails, the broken connection is discarded (close=True) and we try again,
    up to 3 attempts.

    IMPORTANT: rollback() is called after the probe so the connection is
    returned in a clean (no open transaction) state.
    """
    conn = None
    for attempt in range(3):
        conn = _pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            conn.rollback()   # ← leave the connection clean; no open transaction
            break             # probe succeeded
        except _STALE_ERRORS:
            print(f"[db] stale connection (attempt {attempt + 1}/3), discarding.")
            try:
                _pool.putconn(conn, close=True)
            except Exception:
                pass
            conn = None
            time.sleep(0.5)

    if conn is None:
        raise psycopg2.OperationalError(
            "Could not obtain a live DB connection after 3 attempts."
        )

    try:
        yield conn
    except _STALE_ERRORS:
        # Network failure mid-query — kill the connection and propagate.
        if not conn.closed:
            try:
                conn.close()
            except Exception:
                pass
        try:
            _pool.putconn(conn, close=True)
        except Exception:
            pass
        conn = None   # prevent double-putconn in finally
        raise
    except Exception:
        # Application-level error — rollback and return to pool normally.
        if not conn.closed:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        if conn is not None:
            try:
                if conn.closed:
                    _pool.putconn(conn, close=True)
                else:
                    _pool.putconn(conn)
            except Exception:
                pass


def _dict_cursor(conn):
    """Return a RealDictCursor for the given connection."""
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


# ---------- helpers ----------

def save_client_order(client_name, nif, order_id, received_day, lines):
    """lines = [(type, qty, ddate_abs, penalty), ...]"""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO client_orders(client_name, nif, order_id, received_day)
               VALUES(%s,%s,%s,%s) RETURNING id""",
            (client_name, nif, order_id, received_day),
        )
        coid = cur.fetchone()[0]
        psycopg2.extras.execute_batch(
            cur,
            """INSERT INTO order_lines(client_order_id, piece_type, quantity, ddate, penalty)
               VALUES(%s,%s,%s,%s,%s)""",
            [(coid, t, q, d, p) for (t, q, d, p) in lines],
        )
        conn.commit()
        return coid


def get_pending_order_lines():
    with get_conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute(
            "SELECT * FROM order_lines WHERE produced < quantity ORDER BY ddate ASC"
        )
        return cur.fetchall()


def get_all_orders():
    with get_conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("""
            SELECT co.id            AS client_order_pk,
                   co.client_name,
                   co.nif,
                   co.order_id,
                   co.received_day,
                   co.status,
                   ol.id            AS line_id,
                   ol.piece_type,
                   ol.quantity,
                   ol.ddate,
                   ol.penalty,
                   ol.produced,
                   ol.delivered
              FROM client_orders co
              JOIN order_lines  ol ON ol.client_order_id = co.id
             ORDER BY ol.ddate ASC
        """)
        return cur.fetchall()


def get_production_plan():
    with get_conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("SELECT * FROM production_plan ORDER BY sim_day ASC")
        return cur.fetchall()


def get_purchase_plan():
    with get_conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("SELECT * FROM purchase_plan ORDER BY order_day ASC")
        return cur.fetchall()


def clear_undispatched_plans():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM production_plan WHERE dispatched = FALSE")
        cur.execute("DELETE FROM purchase_plan   WHERE placed     = FALSE")
        conn.commit()


def add_production_entry(sim_day, piece_type, qty, order_line_id):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO production_plan(sim_day, piece_type, quantity, order_line_id)
               VALUES(%s,%s,%s,%s)""",
            (sim_day, piece_type, qty, order_line_id),
        )
        conn.commit()


def add_purchase_entry(order_day, arrival_day, supplier, material, qty, cost):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO purchase_plan(order_day, arrival_day, supplier,
                                         material, quantity, cost)
               VALUES(%s,%s,%s,%s,%s,%s)""",
            (order_day, arrival_day, supplier, material, qty, cost),
        )
        conn.commit()


def mark_production_dispatched(ids):
    if not ids:
        return
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE production_plan SET dispatched=TRUE WHERE id = ANY(%s)", (list(ids),))
        conn.commit()


def mark_purchase_placed(ids):
    if not ids:
        return
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE purchase_plan SET placed=TRUE WHERE id = ANY(%s)", (list(ids),))
        conn.commit()


def production_due_on(day):
    with get_conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute(
            "SELECT * FROM production_plan WHERE sim_day=%s AND dispatched=FALSE",
            (day,),
        )
        return cur.fetchall()


def purchases_due_on(day):
    with get_conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute(
            "SELECT * FROM purchase_plan WHERE order_day=%s AND placed=FALSE",
            (day,),
        )
        return cur.fetchall()


def log_mes_status(ts, topic, payload):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO mes_status_log(ts, topic, payload) VALUES(%s,%s,%s)",
            (ts, topic, payload),
        )
        conn.commit()


def set_state(key, value):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO sim_state(key, value) VALUES(%s,%s)
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
            (key, str(value)),
        )
        conn.commit()


def get_state(key, default=None):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT value FROM sim_state WHERE key=%s", (key,))
        row = cur.fetchone()
        return row[0] if row else default


def update_inventory(piece_type, delta):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO inventory(piece_type, quantity) VALUES(%s,%s)
               ON CONFLICT (piece_type) DO UPDATE
               SET quantity = inventory.quantity + EXCLUDED.quantity""",
            (piece_type, delta),
        )
        conn.commit()


def get_inventory():
    with get_conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("SELECT * FROM inventory ORDER BY piece_type")
        return cur.fetchall()
    

def save_penalty_cost(order_line_id, sim_day, penalty_cost):
    """Persist a delay penalty for a given order line."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO production_costs(order_line_id, sim_day, penalty_cost)
               VALUES(%s,%s,%s)""",
            (order_line_id, sim_day, penalty_cost),
        )
        conn.commit()


def save_raw_cost(order_line_id, sim_day, raw_cost):
    """Persist raw-material cost associated with an order line."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO production_costs(order_line_id, sim_day, raw_cost)
               VALUES(%s,%s,%s)""",
            (order_line_id, sim_day, raw_cost),
        )
        conn.commit()