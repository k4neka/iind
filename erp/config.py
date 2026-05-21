"""Global configuration constants for the ERP."""
import os

# --- Networking ---
TCP_HOST = "0.0.0.0"
TCP_PORT = 6666

MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
MQTT_TOPIC_PRODUCTION = "erp/production_orders"
MQTT_TOPIC_DELIVERY = "erp/delivery_orders"
MQTT_TOPIC_PURCHASE = "erp/purchase_orders"
MQTT_TOPIC_MES_STATUS = "mes/status/#"

# --- PostgreSQL ---
DB_CONFIG = {
    "host":     os.getenv("PG_HOST", "db.fe.up.pt"),
    "port":     int(os.getenv("PG_PORT", 5432)),
    "dbname":   os.getenv("PG_DB",   "meec00910"),
    "user":     os.getenv("PG_USER", "meec00910"),
    "password": os.getenv("PG_PASS", "YfBWTHD3OwR2"),
    "options":  "-c search_path=db_iind,public"
}

# --- Simulation ---
SECONDS_PER_DAY = 60
PLANNING_HORIZON_DAYS = 30

# --- Plant limits ---
WAREHOUSE_CAPACITY = 32
MAX_UNLOAD_PER_DAY = 30

# --- Suppliers (Table 4 in PDF) ---
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

# --- Final products clients can order (Rxx / Sxx, x ∈ {W,M}) ---
FINAL_PRODUCTS = {"RWW", "SWW", "RWM", "SWM", "RMM", "SMM"}

# --- Bill of materials (raw pieces required per finished piece) ---
# Each final piece = 1 top + 2 legs. RtopW/StopW come from Wood, RtopM/StopM
# from Metal. LegW from Wood, LegM from Metal.
BOM = {
    "RWW": {"Wood": 3, "Metal": 0},  # RtopW(1W) + 2*LegW(2W)
    "SWW": {"Wood": 3, "Metal": 0},
    "RWM": {"Wood": 1, "Metal": 2},  # RtopW(1W) + 2*LegM(2M)
    "SWM": {"Wood": 1, "Metal": 2},
    "RMM": {"Wood": 0, "Metal": 3},  # RtopM(1M) + 2*LegM(2M)
    "SMM": {"Wood": 0, "Metal": 3},
}
