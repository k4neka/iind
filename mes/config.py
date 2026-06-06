"""MES configuration."""
import os

# --- OPC-UA ---
OPCUA_URL_PRIMARY  = "opc.tcp://172.25.128.1:4840"
OPCUA_URL_FALLBACK = "opc.tcp://localhost:4840"
OPCUA_NS           = 4
OPCUA_PREFIX       = "|var|CODESYS Control Win V3 x64.Application.GVL."

# --- MQTT ---
MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT   = int(os.getenv("MQTT_PORT", 1883))

TOPIC_MATERIAL_LOAD       = "factory/erp/material_load"
TOPIC_MATERIAL_LOAD_WOOD  = "factory/erp/material_load/wood"
TOPIC_MATERIAL_LOAD_METAL = "factory/erp/material_load/metal"

TOPIC_PRODUCTION_ORDERS = "factory/erp/production_orders"
TOPIC_MES_STATUS        = "factory/mes/status"

# Delivery orders from the ERP: pieces to place on the unloading docks
# during the day, then discharge at end-of-day. The ERP also sends an
# explicit end-of-day notification (it owns the sim clock) on which the
# MES discharges every loaded dock.
TOPIC_DELIVERY_ORDERS   = "factory/erp/delivery_orders"
TOPIC_END_OF_DAY        = "factory/erp/end_of_day"

# --- PostgreSQL ---
DB_CONFIG = {
    "host":     os.getenv("PG_HOST", "db.fe.up.pt"),
    "port":     int(os.getenv("PG_PORT", 5432)),
    "dbname":   os.getenv("PG_DB",   "meec00910"),
    "user":     os.getenv("PG_USER", "meec00910"),
    "password": os.getenv("PG_PASS", "YfBWTHD3OwR2"),
    "options":  "-c search_path=db_mes,public",
}

# --- Physical limits ---
WAREHOUSE_CAPACITY = 32
NUM_CELLS          = 4

# --- Unloading docks (PDF §2.4) ---
# 5 unloading docks, each a slider holding at most 6 workpieces. An order
# of more than DOCK_CAPACITY pieces must be split across several docks
# (PDF §4.1: "Orders with more than 6 work-pieces must use more than one
# dock"). The PLC exposes per-dock occupancy in g_Unloader_DockCount[1..5].
NUM_DOCKS     = 5
DOCK_CAPACITY = 6

LOADER_BATCH_SIZE = 5

# Safety cap on the MES production queue depth (v3 Bug 8B). The ERP should
# already only dispatch BOM-covered pieces, but this stops the MES from
# accepting far more pieces than it could ever start if the ERP misbehaves.
MAX_QUEUED = 2 * NUM_CELLS * 2   # = 16

# --- Polling intervals (seconds) ---
POLL_WAREHOUSE_INTERVAL = 1.0
DISPATCH_INTERVAL       = 0.5

# --- Cell capabilities ---
CELL_TOOLS = {
    1: {1: [1, 2, 3], 2: [1, 2, 3], 3: [8, 9, 11]},
    2: {1: [1, 2, 3], 2: [1, 2, 3], 3: [8, 9, 10]},
    3: {1: [4, 5, 6], 2: [4, 5, 6], 3: [8, 9, 11]},
    4: {1: [4, 5, 6], 2: [4, 5, 6], 3: [8, 9, 10]},
}

# NOTE: per-cell startup tools live in tool_state.py (_INIT_TOOLS).
# There is NO PRODUCT_BATCH_SIZE: the SFS M3 assembly consumes EVERY parked
# leg when a top arrives, so exactly 2 legs are parked before 1 top — one
# product per chunk, always. This is a physical invariant, not a parameter.

PIECE_ID = {
    "Wood":  1, "Metal": 2,
    "RtopW": 3, "StopW": 4, "LegW": 5,
    "RtopM": 6, "StopM": 7, "LegM": 8,
    "RWW":   9, "SWW":   10,
    "RWM":   11, "SWM":  12,
    "RMM":   13, "SMM":  14,
}

# --- Complex (mixed-material) pieces ---
COMPLEX_PIECES = {"RWM", "SWM"}

COMPLEX_RECIPE = {
    "RWM": {
        "top": {"piece": "RtopW", "id": 3, "raw": "Wood", "raw_id": 1,
                "cells": [1, 2], "machine": 1, "tool": 1, "time_s": 30},
        "leg": {"piece": "LegM", "id": 8, "raw": "Metal", "raw_id": 2,
                "count": 2, "tool": 5, "time_s": 30, "machines": [2, 1]},
        "asm": {"cells": [3, 4], "machine": 3, "tool": 9, "time_s": 10},
    },
    "SWM": {
        "top": {"piece": "StopW", "id": 4, "raw": "Wood", "raw_id": 1,
                "cells": [1, 2], "machine": 1, "tool": 2, "time_s": 20},
        "leg": {"piece": "LegM", "id": 8, "raw": "Metal", "raw_id": 2,
                "count": 2, "tool": 5, "time_s": 30, "machines": [2, 1]},
        "asm": {"cells": [3, 4], "machine": 3, "tool": 9, "time_s": 10},
    },
}