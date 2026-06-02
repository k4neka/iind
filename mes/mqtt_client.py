"""MQTT bridge: subscribes to ERP topics and publishes MES status.

Maintains a *local* W1 estimate (wood/metal) because the PLC's
g_W1_Count is not authoritative in the current CODESYS project. The
estimate is incremented when raw material is successfully dispatched
to the Loader and decremented when the dispatcher consumes a piece
for production.
"""
import asyncio
import json
import threading
import time

import paho.mqtt.client as mqtt

from config import (MQTT_BROKER, MQTT_PORT,
                    TOPIC_MATERIAL_LOAD, TOPIC_MATERIAL_LOAD_WOOD,
                    TOPIC_MATERIAL_LOAD_METAL,
                    TOPIC_PRODUCTION_ORDERS, TOPIC_MES_STATUS)
from database import (enqueue_piece, is_message_consumed,
                      mark_message_consumed)


# Ignore retained messages that arrive within this many seconds of
# connecting. They are assumed to be stale leftovers from a previous
# run that the ERP hasn't had time to wipe yet.
RETAINED_IGNORE_WINDOW_S = 5.0


class MESMqtt:
    def __init__(self, loop, plc):
        self.loop = loop
        self.plc = plc
        self.client = mqtt.Client(client_id="MES")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.connected = False
        self._connect_ts = None

        self._pending_wood = 0
        self._pending_metal = 0
        self._load_lock = threading.Lock()

        self._flushing = False

        # Local W1 inventory model. Tracked here because the PLC does
        # not maintain g_W1_Count reliably in the current project.
        self._w1_local = {"Wood": 0, "Metal": 0}
        self._w1_lock = threading.Lock()

    # ---- W1 estimate API -----------------------------------------------

    def w1_estimate(self):
        with self._w1_lock:
            return dict(self._w1_local)

    def w1_consume(self, material, qty=1):
        with self._w1_lock:
            self._w1_local[material] = max(
                0, self._w1_local.get(material, 0) - qty)
            print(f"[w1] -{qty} {material} (now wood:"
                  f"{self._w1_local['Wood']} "
                  f"metal:{self._w1_local['Metal']})")

    def _w1_add(self, wood, metal):
        with self._w1_lock:
            self._w1_local["Wood"] += wood
            self._w1_local["Metal"] += metal
            print(f"[w1] +wood={wood} +metal={metal} (now wood:"
                  f"{self._w1_local['Wood']} "
                  f"metal:{self._w1_local['Metal']})")

    def w1_reset(self, wood=0, metal=0):
        """Reset the local W1 model. Call this on MES startup if the
        physical line was reset, or when re-syncing with a trusted
        external source of truth."""
        with self._w1_lock:
            self._w1_local["Wood"] = int(wood)
            self._w1_local["Metal"] = int(metal)
            print(f"[w1] RESET to wood={wood} metal={metal}")

    # ---- generic piece tracking (sub-parts staged via Transfer Cell) ----

    def w1_add_piece(self, name, qty=1):
        """Add an arbitrary piece type to the W1 model (e.g. a sub-part
        RtopW/LegM brought back from W2 through the Transfer Cell)."""
        with self._w1_lock:
            self._w1_local[name] = self._w1_local.get(name, 0) + qty
            print(f"[w1] +{qty} {name} (now {self._w1_local.get(name)})")

    def w1_has(self, name, qty=1) -> bool:
        with self._w1_lock:
            return self._w1_local.get(name, 0) >= qty

    # ---- MQTT plumbing -------------------------------------------------

    def start(self):
        try:
            self.client.connect(MQTT_BROKER, MQTT_PORT, 60)
            threading.Thread(target=self.client.loop_forever,
                             daemon=True).start()
        except Exception as e:
            print(f"[mqtt] could not connect: {e}")

    def _on_connect(self, client, userdata, flags, rc):
        self.connected = (rc == 0)
        self._connect_ts = time.time()
        print(f"[mqtt] connected rc={rc}")
        client.subscribe(TOPIC_MATERIAL_LOAD)
        client.subscribe(TOPIC_MATERIAL_LOAD_WOOD)
        client.subscribe(TOPIC_MATERIAL_LOAD_METAL)
        client.subscribe(TOPIC_PRODUCTION_ORDERS)

    def _on_message(self, client, userdata, msg):
        if not msg.payload:
            return

        if msg.retain and msg.topic.startswith("factory/erp/material_load"):
            elapsed = time.time() - (self._connect_ts or time.time())
            if elapsed < RETAINED_IGNORE_WINDOW_S:
                print(f"[mqtt] ignoring stale retained on {msg.topic} "
                      f"(boot window {elapsed:.1f}s)")
                return

        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception as e:
            print(f"[mqtt] bad payload on {msg.topic}: {e}")
            return

        if msg.topic.startswith("factory/erp/material_load"):
            self._handle_material_load(payload)
        elif msg.topic == TOPIC_PRODUCTION_ORDERS:
            self._handle_production_orders(payload)

    # ---- material_load -------------------------------------------------

    def _handle_material_load(self, p):
        msg_id = p.get("message_id")
        if not msg_id:
            print("[mqtt] material_load without message_id - skipped")
            return
        if is_message_consumed(msg_id):
            print(f"[mqtt] material_load {msg_id} already consumed - "
                  f"skip")
            return
        mark_message_consumed(msg_id)

        w_added = m_added = 0
        if "items" in p:
            for it in p["items"]:
                t = it.get("type")
                q = int(it.get("quantity", 0))
                if t == "Wood":
                    w_added += q
                elif t == "Metal":
                    m_added += q
        else:
            t = p.get("type")
            q = int(p.get("quantity", 0))
            if t == "Wood":
                w_added = q
            elif t == "Metal":
                m_added = q

        with self._load_lock:
            self._pending_wood += w_added
            self._pending_metal += m_added
            print(f"[mqtt] material_load +wood={w_added} "
                  f"+metal={m_added} (pending buffer: "
                  f"wood={self._pending_wood} "
                  f"metal={self._pending_metal})")

        asyncio.run_coroutine_threadsafe(self.flush_loader(), self.loop)

    async def flush_loader(self):
        if self._flushing:
            return
        try:
            self._flushing = True

            status = await self.plc.read_loader_status()
            if status != 0:
                with self._load_lock:
                    w, m = self._pending_wood, self._pending_metal
                if w > 0 or m > 0:
                    print(f"[mqtt] flush_loader skipped: PLC busy "
                          f"(status={status}), buffer kept "
                          f"(wood={w} metal={m})")
                return

            with self._load_lock:
                w, m = self._pending_wood, self._pending_metal
                if w == 0 and m == 0:
                    return
                self._pending_wood = 0
                self._pending_metal = 0

            print(f"[mqtt] flush_loader dispatching wood={w} metal={m}")
            await self.plc.trigger_loader_batched(w, m)
            self._w1_add(w, m)
        except Exception as e:
            print(f"[mqtt] flush_loader error: {e}")
        finally:
            self._flushing = False

    # ---- production orders ---------------------------------------------

    def _handle_production_orders(self, p):
        items = p.get("items", [])
        order_id = p.get("order_id")
        total = 0
        for it in items:
            qty = int(it.get("quantity", 1))
            total += qty
            for _ in range(qty):
                enqueue_piece(order_id=it.get("order_id") or order_id,
                              order_line_id=it.get("order_line_id"),
                              piece_type=it["piece_type"])
        print(f"[mqtt] queued {total} pieces from production_orders")

    # ---- status publishing ---------------------------------------------

    def publish_status(self, payload):
        msg = json.dumps(payload)
        self.client.publish(TOPIC_MES_STATUS, msg, qos=1)
        print(f"[mqtt-out] {TOPIC_MES_STATUS} -> {msg}")