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

        # 1. Current Day (SimClock uses 'sim_base_day')
        cur.execute("SELECT value FROM sim_state WHERE key='sim_base_day'")
        row = cur.fetchone()
        current_day = int(row['value']) if row else 0
        
        # Check for inventory table
        cur.execute("SELECT to_regclass('db_erp.inventory')")
        if not cur.fetchone()['to_regclass']:
            return {"type": "dashboard_update", "current_day": current_day, "warehouse": {"w1": 0, "w2": 0}, "inventory": {}, "orders": [], "cell_times": [], "purchases": []}

        # 2. Inventory (W1/W2)
        cur.execute("SELECT piece_type, quantity FROM inventory")
        inv_rows = cur.fetchall()
        w1_count = 0
        w2_count = 0
        inventory_full = {}
        for r in inv_rows:
            qty = int(r['quantity'])
            inventory_full[r['piece_type']] = qty
            if r['piece_type'] in ['Wood', 'Metal']:
                w1_count += qty
            else:
                w2_count += qty
                
        # 3. Orders Summary
        cur.execute("""
            SELECT co.order_id, 
                   sum(ol.quantity) as total_pieces,
                   sum(ol.produced) as produced,
                   sum(ol.quantity - ol.produced) as pending,
                   sum(ol.quantity * ol.penalty) as potential_penalty,
                   co.status
            FROM client_orders co
            JOIN order_lines ol ON ol.client_order_id = co.id
            GROUP BY co.id, co.order_id, co.status
            ORDER BY co.id DESC LIMIT 10
        """)
        orders = cur.fetchall()
        for o in orders:
            # Add a mock cost or calculate if possible. 
            # For now, let's use a simple heuristic or leave it as 0
            o['total_cost'] = float(o['produced']) * 5.5 # Example rate
            
        # 4. Cell Performance (from MES schema)
        # Since we are connected with db_erp search path, we must use explicit schema for MES
        try:
            cur.execute("SELECT cell, sum(duration_s) as total_time FROM db_mes.machine_occupancy GROUP BY cell")
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

        return {
            "type": "dashboard_update",
            "current_day": current_day,
            "warehouse": {"w1": w1_count, "w2": w2_count},
            "inventory": inventory_full,
            "orders": orders,
            "cell_times": cell_times,
            "purchases": purchases
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
