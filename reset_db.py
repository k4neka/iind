"""Database reset script for IndFin 4.0.
Clears all simulation data and resets the clock to Day 0.
"""
import psycopg2
import os
import sys

# Load config from ERP
sys.path.append(os.path.join(os.getcwd(), 'erp'))
from config import DB_CONFIG

def reset():
    tables = [
        "db_erp.production_plan",
        "db_erp.purchase_plan",
        "db_erp.client_orders",
        "db_erp.order_lines",
        "db_erp.inventory",
        "db_erp.sim_state",
        "db_erp.mes_status_log",
        "db_erp.production_costs",
        "db_erp.stock_reservations",
        "db_mes.pending_pieces",
        "db_mes.consumed_messages",
        # Scheduled-time machine statistics (TASK 1) + persisted mounted tools
        # (TASK 6): emptying machine_tool_state makes the MES re-seed it to the
        # startup tools on next boot.
        "db_mes.machine_stats",
        "db_mes.machine_tool_seconds",
        "db_mes.machine_piece_counts",
        "db_mes.machine_tool_state",
        # Persisted unloader books + per-type unloaded tallies (TASK 4).
        "db_mes.unloader_lines",
        "db_mes.unloader_w2_stock",
        "db_mes.dock_state",
        "db_mes.unloaded_pieces",
    ]
    
    conn = None
    try:
        print(f"[reset] Connecting to {DB_CONFIG['host']}...")
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        
        print("[reset] Truncating simulation tables...")
        # Clear tables one by one. RESTART IDENTITY might still fail if not owner,
        # but standard TRUNCATE or DELETE should work.
        for table in tables:
            try:
                print(f"  -> Clearing {table}...")
                cur.execute(f"TRUNCATE TABLE {table} CASCADE;")
            except Exception:
                # Fallback to DELETE if TRUNCATE is restricted
                print(f"  -> Truncate failed, falling back to DELETE for {table}")
                conn.rollback()
                cur.execute(f"DELETE FROM {table}")
        
        # Reset the clock
        print("[reset] Resetting sim_base_day to 0...")
        cur.execute("DELETE FROM db_erp.sim_state WHERE key='sim_base_day'")
        cur.execute("INSERT INTO db_erp.sim_state(key, value) VALUES('sim_base_day', '0')")
        
        # Reset inventory
        print("[reset] Initializing empty inventory...")
        for p in ("Wood", "Metal", "RWW", "SWW", "RWM", "SWM", "RMM", "SMM"):
            cur.execute("UPDATE db_erp.inventory SET quantity = 0 WHERE piece_type = %s", (p,))
            # Ensure it exists if UPDATE matched 0 rows
            cur.execute("INSERT INTO db_erp.inventory(piece_type, quantity) VALUES(%s, 0) ON CONFLICT (piece_type) DO UPDATE SET quantity = 0", (p,))

        conn.commit()
        print("[reset] Database successfully cleared. Back to Day 0.")
    except Exception as e:
        if conn: conn.rollback()
        print(f"[reset] ERROR: {e}")
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    reset()
