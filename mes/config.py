"""MES configuration."""
import os

# --- OPC-UA ---
OPCUA_URL_PRIMARY  = "opc.tcp://172.25.128.1:4840"
OPCUA_URL_FALLBACK = "opc.tcp://localhost:4840"
OPCUA_NS = 4
OPCUA_PREFIX = "|var|CODESYS Control Win V3 x64.Application.GVL."

# --- MQTT ---
MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT   = int(os.getenv("MQTT_PORT", 1883))

TOPIC_MATERIAL_LOAD     = "factory/erp/material_load"
TOPIC_PRODUCTION_ORDERS = "factory/erp/production_orders"
TOPIC_MES_STATUS        = "factory/mes/status"

# --- PostgreSQL (MES has its own DB) ---
DB_CONFIG = {
    "host":     os.getenv("PG_HOST", "db.fe.up.pt"),
    "port":     int(os.getenv("PG_PORT", 5432)),
    "dbname":   os.getenv("PG_DB",   "meec00910"),
    "user":     os.getenv("PG_USER", "meec00910"),
    "password": os.getenv("PG_PASS", "YfBWTHD3OwR2"),
    "options":  "-c search_path=db_iind,public"
}

# --- Physical limits ---
WAREHOUSE_CAPACITY = 32
NUM_CELLS = 4
REG_SIZE  = 15           # GVL.Reg[0..14]

# --- Polling intervals (seconds) ---
POLL_REG_INTERVAL       = 0.2
POLL_WAREHOUSE_INTERVAL = 1.0
DISPATCH_INTERVAL       = 0.3

# --- Cost rates (€ per second occupied) ---
MACHINE_RATE_PER_SEC = 0.05      # used by cost_tracker

# --- Cell capabilities (Table 1 of the PDF) ---
# Each cell has 3 machines: M1, M2, M3. Tool set per cell:
CELL_TOOLS = {
    1: {1: [1, 2, 3],  2: [1, 2, 3],  3: [8, 9, 11]},
    2: {1: [1, 2, 3],  2: [1, 2, 3],  3: [8, 9, 10]},
    3: {1: [4, 5, 6],  2: [4, 5, 6],  3: [8, 9, 11]},
    4: {1: [4, 5, 6],  2: [4, 5, 6],  3: [8, 9, 10]},
}

# --- Piece ID map (Table 2) ---
PIECE_ID = {
    "Wood": 1, "Metal": 2,
    "RtopW": 3, "StopW": 4, "LegW": 5,
    "RtopM": 6, "StopM": 7, "LegM": 8,
    "RWW": 9,  "SWW": 10,
    "RWM": 11, "SWM": 12,
    "RMM": 13, "SMM": 14,
}