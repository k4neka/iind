"""Dashboard data service.

Reads db_erp + db_mes and PUBLISHES an aggregated snapshot on the normal MQTT
topic ``factory/mes/dashboard`` (broker port 1883). The HTML page subscribes to
that topic over MQTT-over-WebSocket (broker port 9001) — see dashboard.html and
mosquitto_ws.conf. This process never serves HTTP; it only publishes MQTT.

It also derives the per-machine statistics (TASK 1/8) from the db_mes stats
tables and includes them in the snapshot so the dashboard can render machine
tool usage, occupation, tool-change counts and pieces-by-type.
"""
import json
import time
import os
import sys
import subprocess
import importlib.util

import psycopg2
import psycopg2.extras
import paho.mqtt.client as mqtt

# dashboards/ lives directly under the project root. The ERP and MES packages
# BOTH define a module named ``config``, so we import the ERP one by adding
# erp/ to sys.path, and load the MES CELL_TOOLS table from its file directly
# (under a private module name) to avoid the config-name clash.
DASH_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(DASH_DIR)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "erp"))
from config import (DB_CONFIG, MQTT_BROKER, MQTT_PORT,          # noqa: E402
                    PRODUCT_PRICES, SECONDS_PER_DAY)

# Port of the broker's WebSocket listener the HTML page connects to (the
# service itself publishes over plain TCP MQTT_PORT). Override with MQTT_WS_PORT.
WS_PORT = int(os.getenv("MQTT_WS_PORT", 9001))

FINAL_PRODUCTS = ["RWW", "SWW", "RWM", "SWM", "RMM", "SMM"]


def _load_cell_tools():
    """Load CELL_TOOLS from mes/config.py without importing it as ``config``
    (which would clash with the ERP config already imported)."""
    path = os.path.join(PROJECT_ROOT, "mes", "config.py")
    spec = importlib.util.spec_from_file_location("_mes_config_for_dash", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return {c: {s: list(t) for s, t in slots.items()}
            for c, slots in mod.CELL_TOOLS.items()}


try:
    CELL_TOOLS = _load_cell_tools()
except Exception as e:
    print(f"[dashboard-svc] could not load CELL_TOOLS: {e}")
    CELL_TOOLS = {}


# --------------------------------------------------------------------------
# Machine statistics (TASK 1/8) — derived from db_mes stats tables.
# --------------------------------------------------------------------------
def _read_machines(cur, elapsed_seconds):
    """One row per machine (every (cell, slot) in CELL_TOOLS) with mounted +
    available tools, cumulative per-tool seconds, tool-change count,
    pieces-operated by type, and occupation % computed BOTH ways against
    `elapsed_seconds` (sim time so far)."""
    cur.execute("SELECT cell, slot, tool FROM db_mes.machine_tool_state")
    mounted = {(r["cell"], r["slot"]): int(r["tool"]) for r in cur.fetchall()}

    cur.execute("SELECT * FROM db_mes.machine_stats")
    stats = {(r["cell"], r["slot"]): r for r in cur.fetchall()}

    cur.execute("SELECT cell, slot, tool, seconds FROM db_mes.machine_tool_seconds")
    tool_secs = {}
    for r in cur.fetchall():
        tool_secs.setdefault((r["cell"], r["slot"]), {})[int(r["tool"])] = \
            float(r["seconds"])

    cur.execute("SELECT cell, slot, piece_type, count "
                "FROM db_mes.machine_piece_counts")
    counts = {}
    for r in cur.fetchall():
        counts.setdefault((r["cell"], r["slot"]), {})[r["piece_type"]] = \
            int(r["count"])

    out = []
    for cell in sorted(CELL_TOOLS):
        for slot in sorted(CELL_TOOLS[cell]):
            key = (cell, slot)
            st = stats.get(key)
            op_s = float(st["operating_seconds"]) if st else 0.0
            ch_s = float(st["tool_change_seconds"]) if st else 0.0
            if elapsed_seconds > 0:
                occ_op = round(min(1.0, op_s / elapsed_seconds), 4)
                occ_busy = round(min(1.0, (op_s + ch_s) / elapsed_seconds), 4)
            else:
                occ_op = occ_busy = 0.0
            out.append({
                "cell": cell, "slot": slot,
                "machine": f"C{cell}-M{slot}",
                "mounted_tool": mounted.get(key),
                "available_tools": sorted(CELL_TOOLS[cell][slot]),
                "operating_seconds": op_s,
                "tool_change_seconds": ch_s,
                "tool_changes": int(st["tool_changes"]) if st else 0,
                "pieces_operated": int(st["pieces_operated"]) if st else 0,
                "occupation_operating": occ_op,
                "occupation_busy": occ_busy,
                # tool->seconds and piece->count keyed by string for JSON.
                "tool_seconds": {str(k): v
                                 for k, v in tool_secs.get(key, {}).items()},
                "piece_counts": counts.get(key, {}),
            })
    return out


def _read_unloaded(cur):
    """Per-dock per-type lifetime unloaded (delivered) counts (TASK 4/8)."""
    cur.execute("SELECT dock, piece_type, qty FROM db_mes.unloaded_pieces "
                "ORDER BY dock, piece_type")
    return [{"dock": r["dock"], "piece_type": r["piece_type"],
             "qty": int(r["qty"])} for r in cur.fetchall()]


def _read_dock_state(cur):
    """Current per-dock owner + occupancy (TASK 4/8)."""
    try:
        cur.execute("SELECT dock, owner_line_id, count FROM db_mes.dock_state "
                    "ORDER BY dock")
        return [{"dock": r["dock"], "owner_line_id": r["owner_line_id"],
                 "count": int(r["count"])} for r in cur.fetchall()]
    except Exception:
        return []


def get_data():
    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Tables ready? (ERP + MES may still be creating them.)
        cur.execute("SELECT to_regclass('db_erp.sim_state')")
        if not cur.fetchone()['to_regclass']:
            return None
        cur.execute("SELECT to_regclass('db_mes.pending_pieces')")
        if not cur.fetchone()['to_regclass']:
            return None

        # 1. Current sim day.
        cur.execute("SELECT value FROM sim_state WHERE key='sim_base_day'")
        row = cur.fetchone()
        current_day = int(row['value']) if row else 0
        elapsed_seconds = current_day * SECONDS_PER_DAY

        cur.execute("SELECT to_regclass('db_erp.inventory')")
        if not cur.fetchone()['to_regclass']:
            return {"type": "dashboard_update", "current_day": current_day,
                    "warehouse": {"w1": 0, "w2": 0}, "inventory": {},
                    "orders": [], "machines": [], "unloaded": [],
                    "purchases": []}

        # 2. Inventory (W1) + physical W2 (produced - delivered).
        cur.execute("SELECT piece_type, quantity FROM inventory")
        inventory_raw = {r['piece_type']: int(r['quantity'])
                         for r in cur.fetchall()}

        cur.execute("""SELECT piece_type, count(*) as qty
                       FROM db_mes.pending_pieces
                       WHERE status = 'COMPLETED' GROUP BY piece_type""")
        produced_map = {r['piece_type']: int(r['qty']) for r in cur.fetchall()}

        cur.execute("""SELECT payload FROM db_erp.mes_status_log
                       WHERE topic = 'factory/mes/status'
                         AND payload LIKE '%ORDER_DISPATCHED%'""")
        delivered_map = {}
        for dr in cur.fetchall():
            try:
                p = json.loads(dr['payload'])
                ptype = p.get('piece_type')
                if ptype:
                    delivered_map[ptype] = (delivered_map.get(ptype, 0)
                                            + int(p.get('quantity', 0)))
            except Exception:
                continue

        w1_count = inventory_raw.get('Wood', 0) + inventory_raw.get('Metal', 0)
        w2_count = 0
        inventory_display = {'Wood': inventory_raw.get('Wood', 0),
                             'Metal': inventory_raw.get('Metal', 0)}
        for ptype in FINAL_PRODUCTS:
            phys = produced_map.get(ptype, 0) - delivered_map.get(ptype, 0)
            inventory_display[ptype] = max(0, phys)
            w2_count += inventory_display[ptype]

        # 3. Orders summary.
        cur.execute("""SELECT column_name FROM information_schema.columns
                       WHERE table_name='client_orders'
                         AND column_name='created_at'""")
        has_created_at = cur.fetchone() is not None
        start_col = "co.created_at" if has_created_at else "CURRENT_TIMESTAMP"
        cur.execute(f"""
            SELECT co.order_id,
                   sum(ol.quantity) as total_pieces,
                   sum(COALESCE(done.qty, 0)) as produced,
                   sum(ol.quantity) - sum(COALESCE(done.qty, 0)) as pending,
                   sum(ol.quantity * ol.penalty) as potential_penalty,
                   co.status,
                   COALESCE(min(done.first_piece), {start_col}) as started_at,
                   max(done.last_piece) as finished_at
            FROM client_orders co
            JOIN order_lines ol ON ol.client_order_id = co.id
            LEFT JOIN (
                SELECT order_line_id,
                       count(*) FILTER (WHERE status = 'COMPLETED') as qty,
                       min(created_at) as first_piece,
                       max(finished_at) as last_piece
                FROM db_mes.pending_pieces GROUP BY order_line_id
            ) done ON done.order_line_id = ol.id
            GROUP BY co.id, co.order_id, co.status, {start_col}
            ORDER BY co.id DESC LIMIT 20
        """)
        orders = cur.fetchall()

        # 4. Machine statistics + unloaded-by-type + dock state (TASK 1/4/8).
        try:
            machines = _read_machines(cur, elapsed_seconds)
        except Exception as e:
            print(f"[dashboard-svc] machine stats read failed: {e}")
            machines = []
        try:
            unloaded = _read_unloaded(cur)
        except Exception:
            unloaded = []
        docks = _read_dock_state(cur)

        # 5. Purchase plan (upcoming).
        cur.execute("""SELECT supplier, material, quantity, arrival_day, cost,
                              placed
                       FROM purchase_plan WHERE arrival_day >= %s
                       ORDER BY arrival_day ASC LIMIT 8""", (current_day,))
        purchases = cur.fetchall()

        # 6. Financials (P&L).
        cur.execute("SELECT SUM(cost) as total FROM purchase_plan "
                    "WHERE placed=TRUE")
        total_mat_cost = float(cur.fetchone()['total'] or 0)
        cur.execute("SELECT SUM(penalty_cost) as total FROM production_costs")
        total_penalties = float(cur.fetchone()['total'] or 0)
        total_revenue = sum(PRODUCT_PRICES.get(pt, 0) * qty
                            for pt, qty in produced_map.items())
        total_profit = total_revenue - total_mat_cost - total_penalties

        return {
            "type": "dashboard_update",
            "current_day": current_day,
            "elapsed_seconds": elapsed_seconds,
            "warehouse": {"w1": w1_count, "w2": w2_count},
            "inventory": inventory_display,
            "orders": orders,
            "machines": machines,
            "unloaded": unloaded,
            "docks": docks,
            "purchases": purchases,
            "financials": {
                "revenue": total_revenue,
                "mat_cost": total_mat_cost,
                "penalties": total_penalties,
                "profit": total_profit,
            },
        }
    except psycopg2.Error:
        return None        # tables likely not ready yet
    except Exception as e:
        print(f"[dashboard-svc] Unexpected Error: {e}")
        return None
    finally:
        if conn:
            conn.close()


# --------------------------------------------------------------------------
# Connection info (TASK 8/9): print BOTH localhost and the WSL eth0 IP so the
# user can point a Windows browser at whichever it can reach, and write it to
# .dashboard_conn for run_all.sh to echo.
# --------------------------------------------------------------------------
def _detect_hosts():
    hosts = ["localhost"]
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True,
                             text=True, timeout=3).stdout.split()
        if out and out[0] not in hosts:
            hosts.append(out[0])
    except Exception:
        pass
    return hosts


def announce_connection(ws_port=WS_PORT):
    hosts = _detect_hosts()
    lines = [f"Dashboard: open dashboards/dashboard.html, connect MQTT-WS to "
             f"{h}:{ws_port}" for h in hosts]
    banner = "\n".join(lines)
    print("=" * 72)
    print(banner)
    print(f"(open dashboard.html?host=<HOST>&port={ws_port} to preselect a "
          f"host; use whichever your Windows browser can reach)")
    print("=" * 72)
    try:
        with open(os.path.join(DASH_DIR, ".dashboard_conn"), "w") as f:
            f.write(banner + "\n")
            f.write(f"(append ?host=<HOST>&port={ws_port} to the HTML URL)\n")
    except Exception as e:
        print(f"[dashboard-svc] could not write .dashboard_conn: {e}")


def main():
    announce_connection()
    print(f"[dashboard-svc] reading DB + publishing 'factory/mes/dashboard' "
          f"to broker {MQTT_BROKER}:{MQTT_PORT} (TCP) every 2s")
    client = mqtt.Client(client_id="DashboardService")
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        client.loop_start()
    except Exception as e:
        print(f"[dashboard-svc] MQTT connect failed: {e}")
        return

    while True:
        data = get_data()
        if data:
            client.publish("factory/mes/dashboard",
                           json.dumps(data, default=str))
        time.sleep(2)


if __name__ == "__main__":
    main()
