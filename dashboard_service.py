import json
import time
import threading
import os
import psycopg2
import psycopg2.extras
import paho.mqtt.client as mqtt

# Load config from ERP (they are mostly the same)
import sys
sys.path.append(os.path.join(os.getcwd(), 'erp'))
from config import DB_CONFIG, MQTT_BROKER, MQTT_PORT

def get_data():
    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # Check if the ERP tables are ready yet (ERP might still be creating them)
        cur.execute("SELECT to_regclass('db_erp.sim_state')")
        if not cur.fetchone()['to_regclass']:
            return None # Not ready yet

        # Check if the MES tables are ready yet
        cur.execute("SELECT to_regclass('db_mes.pending_pieces')")
        if not cur.fetchone()['to_regclass']:
            return None # Not ready yet

        # 1. Current Day (SimClock uses 'sim_base_day')
        cur.execute("SELECT value FROM sim_state WHERE key='sim_base_day'")
        row = cur.fetchone()
        current_day = int(row['value']) if row else 0
        
        # Check for inventory table
        cur.execute("SELECT to_regclass('db_erp.inventory')")
        if not cur.fetchone()['to_regclass']:
            return {"type": "dashboard_update", "current_day": current_day, "warehouse": {"w1": 0, "w2": 0}, "inventory": {}, "orders": [], "cell_times": [], "purchases": []}

        # 2. Inventory (W1/W2)
        # We calculate physical W2 stock by taking (Total Produced) - (Total Delivered).
        # Total Produced is from db_mes.pending_pieces.
        # Total Delivered is parsed from db_erp.mes_status_log.
        cur.execute("SELECT piece_type, quantity FROM inventory")
        inv_rows = cur.fetchall()
        inventory_raw = {r['piece_type']: int(r['quantity']) for r in inv_rows}

        # Calculate Physical W2
        cur.execute("""
            SELECT piece_type, count(*) as qty 
            FROM db_mes.pending_pieces 
            WHERE status = 'COMPLETED'
            GROUP BY piece_type
        """)
        produced_map = {r['piece_type']: int(r['qty']) for r in cur.fetchall()}

        cur.execute("""
            SELECT payload 
            FROM db_erp.mes_status_log 
            WHERE topic = 'factory/mes/status' 
              AND payload LIKE '%ORDER_DISPATCHED%'
        """)
        dispatched_rows = cur.fetchall()
        delivered_map = {}
        for dr in dispatched_rows:
            try:
                p = json.loads(dr['payload'])
                ptype = p.get('piece_type')
                qty = int(p.get('quantity', 0))
                if ptype:
                    delivered_map[ptype] = delivered_map.get(ptype, 0) + qty
            except: continue

        w1_count = inventory_raw.get('Wood', 0) + inventory_raw.get('Metal', 0)
        w2_count = 0
        inventory_display = {}
        
        # Wood/Metal come from the inventory table (W1)
        inventory_display['Wood'] = inventory_raw.get('Wood', 0)
        inventory_display['Metal'] = inventory_raw.get('Metal', 0)

        # Finished goods are calculated physically
        for ptype in ['RWW', 'SWW', 'RWM', 'SWM', 'RMM', 'SMM']:
            phys = produced_map.get(ptype, 0) - delivered_map.get(ptype, 0)
            inventory_display[ptype] = max(0, phys)
            w2_count += inventory_display[ptype]
                
        # 3. Orders Summary
        # Check if created_at exists (migration check)
        # 3. Orders Summary
        # Check if created_at exists (migration check)
        cur.execute("""
            SELECT column_name FROM information_schema.columns 
            WHERE table_name='client_orders' AND column_name='created_at'
        """)
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
                FROM db_mes.pending_pieces 
                GROUP BY order_line_id
            ) done ON done.order_line_id = ol.id
            GROUP BY co.id, co.order_id, co.status, {start_col}
            ORDER BY co.id DESC LIMIT 20
        """)

        orders = cur.fetchall()
        for o in orders:
            # Simple heuristic for cost: 5.50€ per produced piece.
            o['total_cost'] = float(o['produced']) * 5.5
            
        # 4. Cell Performance (from MES timing samples)
        try:
            cur.execute("""
                SELECT cell, avg(actual_seconds) as avg_time 
                FROM db_mes.production_timing 
                GROUP BY cell 
                ORDER BY cell
            """)
            cell_times = cur.fetchall()
        except:
            cell_times = []
            
        # 5. Purchase Plan (Upcoming)
        cur.execute("""
            SELECT supplier, material, quantity, arrival_day, cost, placed
            FROM purchase_plan
            WHERE arrival_day >= %s
            ORDER BY arrival_day ASC LIMIT 8
        """, (current_day,))
        purchases = cur.fetchall()

        # 6. Financials (The "Artur" P&L Model)
        # 6a. Total Material Cost (All purchases placed so far)
        cur.execute("SELECT SUM(cost) as total FROM purchase_plan WHERE placed=TRUE")
        total_mat_cost = float(cur.fetchone()['total'] or 0)

        # 6b. Total Penalties (Actual penalties recorded)
        cur.execute("SELECT SUM(penalty_cost) as total FROM production_costs")
        total_penalties = float(cur.fetchone()['total'] or 0)

        # 6c. Total Revenue (Value of pieces in WH2)
        # We use produced_map because the user wants revenue to go up when tables are "done" (W2).
        from erp.config import PRODUCT_PRICES
        total_revenue = 0.0
        for ptype, qty in produced_map.items():
            price = PRODUCT_PRICES.get(ptype, 0)
            total_revenue += (price * qty)

        total_profit = total_revenue - total_mat_cost - total_penalties

        return {
            "type": "dashboard_update",
            "current_day": current_day,
            "warehouse": {"w1": w1_count, "w2": w2_count},
            "inventory": inventory_display,
            "orders": orders,
            "cell_times": cell_times,
            "purchases": purchases,
            "financials": {
                "revenue": total_revenue,
                "mat_cost": total_mat_cost,
                "penalties": total_penalties,
                "profit": total_profit
            }
        }
    except psycopg2.Error as e:
        # Just return None if it's a DB error (likely tables not ready)
        return None
    except Exception as e:
        print(f"[dashboard-svc] Unexpected Error: {e}")
        return None
    finally:
        if conn:
            conn.close()

def main():
    print(f"[dashboard-svc] Starting broadcaster. Broker: {MQTT_BROKER}")
    client = mqtt.Client(client_id="DashboardService")
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        client.loop_start()
    except Exception as e:
        print(f"[dashboard-svc] MQTT Connect failed: {e}")
        return

    while True:
        data = get_data()
        if data:
            client.publish("factory/mes/dashboard", json.dumps(data, default=str))
        time.sleep(2)

if __name__ == "__main__":
    main()
