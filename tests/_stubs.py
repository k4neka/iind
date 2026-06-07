"""Shared test stubs: fake out psycopg2/asyncua/paho so the MES/ERP modules
import without a live DB, broker or PLC, and provide an in-memory fake of the
ERP `database` module so planner/time_model logic can be exercised end-to-end.

Tests run with plain stdlib `unittest` (no pytest needed):
    python3 tests/run_all.py
"""
import sys
import types
import os

HERE = os.path.dirname(__file__)
ERP = os.path.abspath(os.path.join(HERE, "..", "erp"))
MES = os.path.abspath(os.path.join(HERE, "..", "mes"))


# --------------------------------------------------------------------------
# Fake third-party modules (only what the code touches at import time).
# --------------------------------------------------------------------------
def install_network_stubs():
    if "psycopg2" not in sys.modules:
        pg = types.ModuleType("psycopg2")
        pg.OperationalError = type("OperationalError", (Exception,), {})
        pg.InterfaceError = type("InterfaceError", (Exception,), {})
        extras = types.ModuleType("psycopg2.extras")
        extras.RealDictCursor = object
        extras.execute_batch = lambda *a, **k: None
        pool = types.ModuleType("psycopg2.pool")
        pool.ThreadedConnectionPool = object
        pg.extras = extras
        pg.pool = pool
        sys.modules["psycopg2"] = pg
        sys.modules["psycopg2.extras"] = extras
        sys.modules["psycopg2.pool"] = pool
    if "paho" not in sys.modules:
        paho = types.ModuleType("paho")
        mqtt_mod = types.ModuleType("paho.mqtt")
        client_mod = types.ModuleType("paho.mqtt.client")
        client_mod.Client = lambda *a, **k: types.SimpleNamespace(
            on_connect=None, on_message=None,
            connect=lambda *a, **k: None, subscribe=lambda *a, **k: None,
            publish=lambda *a, **k: None, loop_forever=lambda *a, **k: None,
            loop_start=lambda *a, **k: None)
        paho.mqtt = mqtt_mod
        mqtt_mod.client = client_mod
        sys.modules["paho"] = paho
        sys.modules["paho.mqtt"] = mqtt_mod
        sys.modules["paho.mqtt.client"] = client_mod
    if "asyncua" not in sys.modules:
        ua_pkg = types.ModuleType("asyncua")
        ua_pkg.Client = object

        class _VT:
            Int16 = "Int16"; Int32 = "Int32"; Int64 = "Int64"
            UInt32 = "UInt32"; UInt64 = "UInt64"
            Double = "Double"; Float = "Float"; Boolean = "Boolean"
        ua_ns = types.SimpleNamespace(
            VariantType=_VT, Variant=lambda *a, **k: None,
            DataValue=lambda *a, **k: None)
        ua_pkg.ua = ua_ns
        sys.modules["asyncua"] = ua_pkg


def use_erp_path():
    install_network_stubs()
    for p in (MES, ERP):
        while p in sys.path:
            sys.path.remove(p)
    sys.path.insert(0, MES)
    sys.path.insert(0, ERP)   # ERP first


def use_mes_path():
    install_network_stubs()
    for p in (ERP, MES):
        while p in sys.path:
            sys.path.remove(p)
    sys.path.insert(0, ERP)
    sys.path.insert(0, MES)   # MES first


# --------------------------------------------------------------------------
# In-memory fake of the ERP `database` module.
# --------------------------------------------------------------------------
class FakeERPDB(types.ModuleType):
    def __init__(self):
        super().__init__("database")
        self.reset()

    def reset(self):
        self.client_orders = []
        self.order_lines = []      # {id, piece_type, quantity, ddate, penalty, produced}
        self.production_plan = []  # {id, sim_day, piece_type, quantity, order_line_id, dispatched, from_stock}
        self.purchase_plan = []    # {id, order_day, arrival_day, supplier, material, quantity, cost, placed}
        self.inventory = {}        # piece_type -> qty
        self.reservations = []     # {order_line_id, piece_type, quantity}
        self.sim_state = {}        # key -> value (str)
        self.penalties = []
        self._pp_id = 0
        self._buy_id = 0

    # ---- order lines ----
    def add_order_line(self, piece_type, quantity, ddate, penalty, produced=0):
        lid = len(self.order_lines) + 1
        self.order_lines.append({"id": lid, "piece_type": piece_type,
                                 "quantity": quantity, "ddate": ddate,
                                 "penalty": penalty, "produced": produced})
        return lid

    def get_pending_order_lines(self):
        return [dict(l) for l in self.order_lines
                if l["produced"] < l["quantity"]]

    # ---- production plan ----
    def get_production_plan(self):
        return [dict(r) for r in self.production_plan]

    def add_production_entry(self, sim_day, piece_type, qty, order_line_id,
                             from_stock=False):
        # Enforce the FK like the real DB so tests catch bad scheduling.
        if order_line_id is not None and not any(
                l["id"] == order_line_id for l in self.order_lines):
            raise ValueError(f"FK: order_line_id {order_line_id} missing")
        self._pp_id += 1
        self.production_plan.append({
            "id": self._pp_id, "sim_day": sim_day, "piece_type": piece_type,
            "quantity": qty, "order_line_id": order_line_id,
            "dispatched": False, "from_stock": from_stock})

    def production_due_on(self, day):
        out = []
        for r in self.production_plan:
            if r["sim_day"] <= day and not r["dispatched"]:
                line = next((l for l in self.order_lines
                             if l["id"] == r["order_line_id"]), {})
                rr = dict(r)
                rr["ddate"] = line.get("ddate")
                rr["penalty"] = line.get("penalty")
                out.append(rr)
        return out

    def mark_production_dispatched(self, ids):
        for r in self.production_plan:
            if r["id"] in ids:
                r["dispatched"] = True

    # ---- purchase plan ----
    def get_purchase_plan(self):
        return [dict(p) for p in self.purchase_plan]

    def add_purchase_entry(self, order_day, arrival_day, supplier, material,
                           qty, cost):
        self._buy_id += 1
        self.purchase_plan.append({
            "id": self._buy_id, "order_day": order_day,
            "arrival_day": arrival_day, "supplier": supplier,
            "material": material, "quantity": qty, "cost": cost,
            "placed": False})

    def purchases_arriving_on(self, day):
        return [dict(p) for p in self.purchase_plan
                if p["arrival_day"] == day and not p["placed"]]

    def mark_purchase_placed(self, ids):
        for p in self.purchase_plan:
            if p["id"] in ids:
                p["placed"] = True

    # ---- inventory ----
    def get_inventory(self):
        return [{"piece_type": k, "quantity": v}
                for k, v in self.inventory.items()]

    def update_inventory(self, piece_type, delta):
        self.inventory[piece_type] = self.inventory.get(piece_type, 0) + delta

    # ---- reservations ----
    def reserve_stock(self, order_line_id, piece_type, qty):
        self.reservations.append({"order_line_id": order_line_id,
                                  "piece_type": piece_type, "quantity": qty})

    def get_reserved(self, piece_type):
        return sum(r["quantity"] for r in self.reservations
                   if r["piece_type"] == piece_type)

    def release_reservation(self, order_line_id):
        self.reservations = [r for r in self.reservations
                             if r["order_line_id"] != order_line_id]

    # ---- client orders / status log ----
    def save_client_order(self, client_name, nif, order_id, received_day, lines):
        coid = len(self.client_orders) + 1
        self.client_orders.append({"id": coid, "order_id": order_id})
        for (t, q, d, p) in lines:
            self.add_order_line(t, q, d, p)
        return coid

    def log_mes_status(self, *a):
        pass

    # ---- misc ----
    def save_penalty_cost(self, *a):
        self.penalties.append(a)

    def get_state(self, key, default=None):
        return self.sim_state.get(key, default)

    def set_state(self, key, value):
        self.sim_state[key] = str(value)


def install_fake_erp_db():
    """Replace sys.modules['database'] with a fresh in-memory fake; returns it.
    Drops cached ERP modules so they re-bind to the fake on next import."""
    use_erp_path()
    fake = FakeERPDB()
    sys.modules["database"] = fake
    for m in ("planner", "time_model", "tcp_server", "sim_clock",
              "mqtt_client", "config"):
        sys.modules.pop(m, None)
    return fake


class FakeMESDB(types.ModuleType):
    def __init__(self):
        super().__init__("database")
        self.queue = []           # QUEUED pending_pieces
        self.consumed = set()

    def enqueue_piece(self, order_id, order_line_id, piece_type):
        self.queue.append({"id": len(self.queue) + 1, "order_id": order_id,
                           "order_line_id": order_line_id,
                           "piece_type": piece_type, "status": "QUEUED"})
        return len(self.queue)

    def queued_pieces(self):
        return [dict(r) for r in self.queue if r["status"] == "QUEUED"]

    def is_message_consumed(self, mid):
        return mid in self.consumed

    def mark_message_consumed(self, mid):
        self.consumed.add(mid)

    # ---- unloader books (TASK 4): in-memory mirror of the DB tables ----
    def unloader_save_books(self, lines, w2_stock, dock_owner, dock_count):
        import copy
        self._u_lines = copy.deepcopy(lines)
        self._u_w2 = dict(w2_stock)
        self._u_owners = dict(dock_owner)
        self._u_counts = dict(dock_count)

    def unloader_load_books(self):
        import copy
        return (copy.deepcopy(getattr(self, "_u_lines", {})),
                dict(getattr(self, "_u_w2", {})),
                dict(getattr(self, "_u_owners", {})))

    def unloader_record_unloaded(self, dock, piece_type, qty):
        self._u_unloaded = getattr(self, "_u_unloaded", {})
        key = (dock, piece_type)
        self._u_unloaded[key] = self._u_unloaded.get(key, 0) + qty


def install_fake_mes_db():
    """Fresh in-memory MES DB; drops cached MES modules. Returns it."""
    use_mes_path()
    fake = FakeMESDB()
    sys.modules["database"] = fake
    for m in ("mqtt_client", "dispatcher", "main", "config", "unloader",
              "statistics"):
        sys.modules.pop(m, None)
    return fake


class CollectBridge:
    """Captures what dispatch_today publishes."""
    def __init__(self):
        self.production = []
        self.delivery = []
        self.loads = []
        self.connected = True

    def send_production_order(self, day, items):
        self.production.append((day, items))

    def send_delivery_order(self, day, items):
        self.delivery.append((day, items))

    def send_material_load_command(self, material, qty):
        self.loads.append((material, qty))
