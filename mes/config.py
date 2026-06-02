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
REG_SIZE           = 15

LOADER_BATCH_SIZE = 5

# --- Polling intervals (seconds) ---
POLL_REG_INTERVAL       = 0.2
POLL_WAREHOUSE_INTERVAL = 1.0
DISPATCH_INTERVAL       = 0.5

# --- Cost rates ---
MACHINE_RATE_PER_SEC = 0.05

# --- Cell capabilities ---
CELL_TOOLS = {
    1: {1: [1, 2, 3], 2: [1, 2, 3], 3: [8, 9, 11]},
    2: {1: [1, 2, 3], 2: [1, 2, 3], 3: [8, 9, 10]},
    3: {1: [4, 5, 6], 2: [4, 5, 6], 3: [8, 9, 11]},
    4: {1: [4, 5, 6], 2: [4, 5, 6], 3: [8, 9, 10]},
}

PIECE_ID = {
    "Wood":  1, "Metal": 2,
    "RtopW": 3, "StopW": 4, "LegW": 5,
    "RtopM": 6, "StopM": 7, "LegM": 8,
    "RWW":   9, "SWW":   10,
    "RWM":   11, "SWM":  12,
    "RMM":   13, "SMM":  14,
}

# --- Complex (mixed-material) pieces ---
# RWM/SWM need a wood top + two metal legs. No single cell can shape both
# wood (T1-T3) and metal (T4-T6) on its M1/M2 machines, so they cannot be
# built in one pass. Following the working Order_generator(PRG), the
# ComplexOrchestrator builds them like this:
#   1. shape the two metal legs INSIDE the assembly cell (a metal+assembly
#      cell, 3 or 4): leg 1 on M2, leg 2 on M1 (so they shape in parallel),
#      each with a trailing no-op at M3 so it parks in the assembly buffer
#      and never leaves the cell;
#   2. shape the wood top in a wood cell (1 or 2); it exits to W2;
#   3. use the Transfer_Cell to bring ONLY the top from W2 back to W1;
#   4. re-inject the top into the assembly cell at M3 with tool 9; the two
#      parked legs are consumed and the finished product exits to W2.
# Only the top crosses the Transfer_Cell; the legs stay put. This mirrors
# Order_generator exactly.
COMPLEX_PIECES = {"RWM", "SWM"}

# top : the wood top, shaped in a wood cell, transferred W2->W1, then
#       re-injected (by its shaped `id`) into the assembly cell for tool 9.
# leg : two metal legs shaped in the assembly cell itself. `machines`
#       gives the per-leg machine slot (M2 then M1 -> parallel shaping);
#       each leg also gets a trailing no-op park at M3 (added by the
#       orchestrator). `count` legs total.
# asm : the assembly cell pool (metal+assembly cells) and the tool-9 step.
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

# --- Production batching ---
# How many final products of the SAME type to group together in one
# dispatch to a cell. With BATCH_SIZE=N the dispatcher sends 2*N legs
# followed by N tops to the same cell. Larger batches drastically
# reduce tool swaps (T3 stays mounted while ALL legs flow through) at
# the cost of needing more buffer slots in the M3 assembly storage.
# Order_generator uses N=1 implicitly. Start at 1; raise to 2 only
# after confirming the CODESYS M3 buffer can hold 6 simultaneous
# pieces (4 legs + 2 tops in flight).
PRODUCT_BATCH_SIZE = 1