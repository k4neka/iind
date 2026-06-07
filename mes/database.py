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
        CREATE TABLE IF NOT EXISTS consumed_messages (
            message_id  TEXT PRIMARY KEY,
            consumed_at TIMESTAMPTZ DEFAULT NOW()
        );

        -- ===== Machine statistics from SCHEDULED op times (TASK 1) =====
        -- Per (cell, slot): cumulative operating time, tool-change time/count
        -- and number of ops performed. Occupation % is derived at read time
        -- from these and the elapsed sim time (sim_base_day * 60).
        CREATE TABLE IF NOT EXISTS machine_stats (
            cell                INT NOT NULL,
            slot                INT NOT NULL,
            operating_seconds   NUMERIC NOT NULL DEFAULT 0,
            tool_change_seconds NUMERIC NOT NULL DEFAULT 0,
            tool_changes        INT     NOT NULL DEFAULT 0,
            pieces_operated     INT     NOT NULL DEFAULT 0,
            updated_at          TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (cell, slot)
        );
        -- Cumulative operating seconds per tool on each machine (cloud
        -- dashboard "tool usage", Req 3).
        CREATE TABLE IF NOT EXISTS machine_tool_seconds (
            cell    INT NOT NULL,
            slot    INT NOT NULL,
            tool    INT NOT NULL,
            seconds NUMERIC NOT NULL DEFAULT 0,
            PRIMARY KEY (cell, slot, tool)
        );
        -- Histogram of each op's OUTPUT piece, attributed to the machine that
        -- ran it (RtopW/LegW on shaping machines, the final product on M3).
        CREATE TABLE IF NOT EXISTS machine_piece_counts (
            cell       INT  NOT NULL,
            slot       INT  NOT NULL,
            piece_type TEXT NOT NULL,
            count      INT  NOT NULL DEFAULT 0,
            PRIMARY KEY (cell, slot, piece_type)
        );
        -- Currently-mounted tool per machine, so it survives an MES restart
        -- (TASK 6). Maintained by ToolStateTracker, not record_completion.
        CREATE TABLE IF NOT EXISTS machine_tool_state (
            cell       INT NOT NULL,
            slot       INT NOT NULL,
            tool       INT NOT NULL,
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (cell, slot)
        );

        -- ===== Unloader books, persisted so they survive restart (TASK 4) =====
        -- Per-line whole-order delivery book (mirrors UnloaderManager._lines).
        CREATE TABLE IF NOT EXISTS unloader_lines (
            order_line_id BIGINT PRIMARY KEY,
            client        TEXT,
            order_id      BIGINT,
            piece_type    TEXT,
            ddate         BIGINT,
            target        INT,
            need          INT,
            on_docks      INT,
            docks         JSONB         -- list of dock ids this line owns
        );
        -- Finished-goods available in W2 by type (the unloader's W2 ledger).
        CREATE TABLE IF NOT EXISTS unloader_w2_stock (
            piece_type TEXT PRIMARY KEY,
            qty        INT NOT NULL DEFAULT 0
        );
        -- Per-dock ownership + occupancy mirror (dashboard reads occupancy).
        CREATE TABLE IF NOT EXISTS dock_state (
            dock          INT PRIMARY KEY,
            owner_line_id BIGINT,
            count         INT NOT NULL DEFAULT 0
        );
        -- Lifetime count of pieces discharged (delivered) per dock, BY TYPE.
        CREATE TABLE IF NOT EXISTS unloaded_pieces (
            dock       INT  NOT NULL,
            piece_type TEXT NOT NULL,
            qty        INT  NOT NULL DEFAULT 0,
            PRIMARY KEY (dock, piece_type)
        );

        -- Backward-compatible migrations for older DBs.
        ALTER TABLE pending_pieces
            ADD COLUMN IF NOT EXISTS wire_piece_id INT;
        ALTER TABLE pending_pieces
            ADD COLUMN IF NOT EXISTS cell_started_at TIMESTAMPTZ;
        -- The ops the MES sent for this piece: list of
        -- {cell, slot, tool, time_s, out, tool_change}. Written at dispatch so
        -- a mid-day restart can still attribute the machine work when the
        -- piece completes; committed into the stats tables on COMPLETED.
        ALTER TABLE pending_pieces
            ADD COLUMN IF NOT EXISTS dispatched_ops JSONB;

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

        -- production_timing held measured wall-clock per-piece durations. The
        -- MES no longer measures real machine time (statistics now come from
        -- the SCHEDULED op times, see statistics.py); drop the stale table.
        DROP TABLE IF EXISTS production_timing;
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


def mark_dispatched(piece_pk, cell, init_piece_id, wire_piece_id=None,
                    dispatched_ops=None):
    """Mark a piece IN_PROGRESS. `dispatched_ops` (a list of per-op stat
    records) is persisted as JSONB so its machine work can be committed to the
    statistics tables when the piece completes — even across an MES restart."""
    ops_json = json.dumps(dispatched_ops) if dispatched_ops is not None else None
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""UPDATE pending_pieces
                          SET status='IN_PROGRESS', assigned_cell=%s,
                              init_piece_id=%s, wire_piece_id=%s,
                              dispatched_ops=%s, started_at=NOW()
                        WHERE id=%s""",
                    (cell, init_piece_id, wire_piece_id, ops_json, piece_pk))
        conn.commit()


def set_dispatched_ops(piece_pk, dispatched_ops):
    """Attach (or overwrite) the dispatched-ops record for a piece already
    marked IN_PROGRESS (used by the complex orchestrator, which learns its
    final routes after mark_dispatched)."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE pending_pieces SET dispatched_ops=%s WHERE id=%s",
                    (json.dumps(dispatched_ops), piece_pk))
        conn.commit()


# ---------- persisted mounted-tool state (TASK 6) ----------

def load_tool_state():
    """Return {(cell, slot): tool} of currently-mounted tools from the DB, or
    {} when nothing has been stored yet (a fresh DB)."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT cell, slot, tool FROM machine_tool_state")
        return {(int(c), int(s)): int(t) for c, s, t in cur.fetchall()}


def save_tool_state(cell, slot, tool):
    """Write through one machine's currently-mounted tool."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO machine_tool_state(cell, slot, tool, updated_at)
               VALUES(%s,%s,%s,NOW())
               ON CONFLICT (cell, slot) DO UPDATE
                   SET tool = EXCLUDED.tool, updated_at = NOW()""",
            (int(cell), int(slot), int(tool)))
        conn.commit()


def save_all_tool_state(mapping):
    """Bulk write {(cell, slot): tool} (used on PLC reconnect reset, when every
    machine returns to its startup tool)."""
    with get_conn() as conn:
        cur = conn.cursor()
        for (cell, slot), tool in mapping.items():
            cur.execute(
                """INSERT INTO machine_tool_state(cell, slot, tool, updated_at)
                   VALUES(%s,%s,%s,NOW())
                   ON CONFLICT (cell, slot) DO UPDATE
                       SET tool = EXCLUDED.tool, updated_at = NOW()""",
                (int(cell), int(slot), int(tool)))
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


# ---------- unloader books persistence (TASK 4) ----------

def unloader_save_books(lines, w2_stock, dock_owner, dock_count):
    """Rewrite the three small unloader books in one transaction (write-through
    on every mutation). `lines` is {order_line_id: {client, order, piece_type,
    ddate, target, need, on_docks, docks:set}}; `w2_stock` is {piece_type: qty};
    `dock_owner`/`dock_count` are {dock: owner_line_id|None} / {dock: count}."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM unloader_lines")
        for lid, L in lines.items():
            cur.execute(
                """INSERT INTO unloader_lines(order_line_id, client, order_id,
                       piece_type, ddate, target, need, on_docks, docks)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (int(lid), L.get("client"), L.get("order"),
                 L.get("piece_type"), L.get("ddate"), L.get("target"),
                 L.get("need"), L.get("on_docks"),
                 json.dumps(sorted(L.get("docks", set())))))
        cur.execute("DELETE FROM unloader_w2_stock")
        for ptype, qty in w2_stock.items():
            if qty:
                cur.execute("INSERT INTO unloader_w2_stock(piece_type, qty) "
                            "VALUES(%s,%s)", (ptype, int(qty)))
        cur.execute("DELETE FROM dock_state")
        for d, owner in dock_owner.items():
            cur.execute("INSERT INTO dock_state(dock, owner_line_id, count) "
                        "VALUES(%s,%s,%s)",
                        (int(d), owner, int(dock_count.get(d, 0))))
        conn.commit()


def unloader_load_books():
    """Load the persisted unloader books. Returns (lines, w2_stock, owners):
    lines = {order_line_id: {...}} (docks as a set), w2_stock = {piece_type:
    qty}, owners = {dock: owner_line_id|None}. Empty when nothing stored yet."""
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM unloader_lines")
        lines = {}
        for r in cur.fetchall():
            docks = r["docks"]
            if isinstance(docks, str):
                docks = json.loads(docks)
            lines[int(r["order_line_id"])] = {
                "client":     r["client"],
                "order":      r["order_id"],
                "piece_type": r["piece_type"],
                "ddate":      int(r["ddate"]) if r["ddate"] is not None
                              else 10 ** 9,
                "target":     int(r["target"] or 0),
                "need":       int(r["need"] or 0),
                "on_docks":   int(r["on_docks"] or 0),
                "docks":      set(int(d) for d in (docks or [])),
            }
        cur.execute("SELECT piece_type, qty FROM unloader_w2_stock")
        w2 = {row["piece_type"]: int(row["qty"]) for row in cur.fetchall()}
        cur.execute("SELECT dock, owner_line_id FROM dock_state")
        owners = {int(row["dock"]):
                  (int(row["owner_line_id"])
                   if row["owner_line_id"] is not None else None)
                  for row in cur.fetchall()}
        return lines, w2, owners


def unloader_record_unloaded(dock, piece_type, qty):
    """Accumulate `qty` pieces of `piece_type` discharged (delivered) from
    `dock` into the lifetime per-dock per-type tally (Req 4.3)."""
    if not piece_type or qty <= 0:
        return
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO unloaded_pieces(dock, piece_type, qty)
               VALUES(%s,%s,%s)
               ON CONFLICT (dock, piece_type) DO UPDATE
                   SET qty = unloaded_pieces.qty + EXCLUDED.qty""",
            (int(dock), piece_type, int(qty)))
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
                    "cell_started_at, dispatched_ops "
                    "FROM pending_pieces WHERE wire_piece_id=%s "
                    "ORDER BY id DESC LIMIT 1", (wire_id,))
        return cur.fetchone()


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