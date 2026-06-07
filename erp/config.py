"""Global configuration constants for the ERP."""
import os

# --- Networking ---
TCP_HOST = "0.0.0.0"
TCP_PORT = 6666

MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT   = int(os.getenv("MQTT_PORT", 1883))

# Tópicos alinhados com o MES (prefixo factory/)
MQTT_TOPIC_PRODUCTION  = "factory/erp/production_orders"
MQTT_TOPIC_DELIVERY    = "factory/erp/delivery_orders"
MQTT_TOPIC_END_OF_DAY  = "factory/erp/end_of_day"
MQTT_TOPIC_MES_STATUS  = "factory/mes/status"

# Material load: um tópico distinto por material para evitar colisão de retain
MQTT_TOPIC_MATERIAL_LOAD       = "factory/erp/material_load"        # legado
MQTT_TOPIC_MATERIAL_LOAD_WOOD  = "factory/erp/material_load/wood"
MQTT_TOPIC_MATERIAL_LOAD_METAL = "factory/erp/material_load/metal"

# --- PostgreSQL ---
DB_CONFIG = {
    "host":     os.getenv("PG_HOST", "db.fe.up.pt"),
    "port":     int(os.getenv("PG_PORT", 5432)),
    "dbname":   os.getenv("PG_DB",   "meec00910"),
    "user":     os.getenv("PG_USER", "meec00910"),
    "password": os.getenv("PG_PASS", "YfBWTHD3OwR2"),
    "options":  "-c search_path=db_erp,public"
}

# --- Simulation ---
SECONDS_PER_DAY       = 60
PLANNING_HORIZON_DAYS = 30

# --- Production time model (cost-benefit estimation) ---
# Used by time_model.estimate_production_seconds to decide whether a
# cheaper-but-slower supplier would push a piece past its deadline.
TOOL_CHANGE_TIME_S = 30    # changing between any two tools (PDF §2)
TRANSFER_TIME_S    = 5     # moving a piece one hop in a clear cell
CELL_HOPS          = 6     # Wout->M1->T1->M2->T2->M3->Win
QUEUE_FACTOR       = 1.5   # inflate transfer time when the line is busy
# "+/- ~5 s, not exact": deadline checks use the PESSIMISTIC est + this margin
# (so we never promise a delivery we might miss); est - this is optimistic
# (display only). See time_model.deadline_finish_seconds / optimistic_seconds.
TIME_TOLERANCE_S   = 5

# --- Plant limits ---
WAREHOUSE_CAPACITY = 32
MAX_UNLOAD_PER_DAY = 30

# --- Predictive pre-ordering (buffer stock) ---
# Baseline raw-material buffer the ERP keeps in W1 at all times,
# independent of client orders, so production can start the instant an
# order arrives. Values are sized to the cheap bulk-supplier minimum
# batches (Wood>=12, Metal>=8) and stay within WAREHOUSE_CAPACITY.
BASELINE_STOCK = {
    "Wood":  12,
    "Metal": 8,
}

# --- Suppliers ---
SUPPLIERS = {
    "SupplierA": {
        "Wood":  {"min": 2,  "price": 10, "lead": 0},
        "Metal": {"min": 4,  "price": 15, "lead": 0},
    },
    "SupplierB": {
        "Wood":  {"min": 12, "price": 2,  "lead": 2},
        "Metal": {"min": 8,  "price": 4,  "lead": 4},
    },
}

# --- Final products clients can order ---
FINAL_PRODUCTS = {"RWW", "SWW", "RWM", "SWM", "RMM", "SMM"}

# --- Sales Prices (Revenue) ---
# COMPETITIVE STRATEGY: Low prices to make Supplier A a safe 5-10€ profit 
# and make Supplier B a high-risk/high-reward gamble.
PRODUCT_PRICES = {
    "RWW": 35.0,
    "RWM": 45.0,
    "RMM": 50.0,
    "SWW": 40.0,
    "SWM": 50.0,
    "SMM": 55.0,
}

# --- Bill of materials ---
BOM = {
    "RWW": {"Wood": 3, "Metal": 0},
    "SWW": {"Wood": 3, "Metal": 0},
    "RWM": {"Wood": 1, "Metal": 2},
    "SWM": {"Wood": 1, "Metal": 2},
    "RMM": {"Wood": 0, "Metal": 3},
    "SMM": {"Wood": 0, "Metal": 3},
}