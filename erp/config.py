"""Global configuration constants for the ERP."""
import os

# --- Networking ---
TCP_HOST = "127.0.0.1"
TCP_PORT = 6666

MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT   = int(os.getenv("MQTT_PORT", 1883))

# Tópicos alinhados com o MES (prefixo factory/)
MQTT_TOPIC_PRODUCTION  = "factory/erp/production_orders"
MQTT_TOPIC_DELIVERY    = "factory/erp/delivery_orders"
MQTT_TOPIC_PURCHASE    = "factory/erp/purchase_orders"
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
SECONDS_PER_DAY       = 30
PLANNING_HORIZON_DAYS = 30

# --- Plant limits ---
WAREHOUSE_CAPACITY = 32
MAX_UNLOAD_PER_DAY = 30

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

# --- Bill of materials ---
BOM = {
    "RWW": {"Wood": 3, "Metal": 0},
    "SWW": {"Wood": 3, "Metal": 0},
    "RWM": {"Wood": 1, "Metal": 2},
    "SWM": {"Wood": 1, "Metal": 2},
    "RMM": {"Wood": 0, "Metal": 3},
    "SMM": {"Wood": 0, "Metal": 3},
}