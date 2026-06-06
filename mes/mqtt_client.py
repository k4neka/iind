"""MQTT bridge: subscribes to ERP topics and publishes MES status.

Maintains a *local* W1 estimate (wood/metal) for fast dispatch decisions,
but periodically snaps it to the PLC's authoritative W1Count[1..2] (see
w1_sync_from_plc) so a queued-but-not-yet-started order can't make the MES
believe it consumed material that never physically left W1.
"""
import asyncio
import json
import threading
import time

import paho.mqtt.client as mqtt

from config import (MQTT_BROKER, MQTT_PORT,
                    TOPIC_MATERIAL_LOAD, TOPIC_MATERIAL_LOAD_WOOD,
                    TOPIC_MATERIAL_LOAD_METAL,
                    TOPIC_PRODUCTION_ORDERS, TOPIC_MES_STATUS, MAX_QUEUED,
                    TOPIC_DELIVERY_ORDERS, TOPIC_END_OF_DAY)
from database import (enqueue_piece, queued_pieces, is_message_consumed,
                      mark_message_consumed)


# Ignore retained messages that arrive within this many seconds of
# connecting. They are assumed to be stale leftovers from a previous
# run that the ERP hasn't had time to wipe yet.
RETAINED_IGNORE_WINDOW_S = 5.0


class MESMqtt:
    def __init__(self, loop, plc, unloader=None):
        self.loop = loop
        self.plc = plc
        self.unloader = unloader
        self.client = mqtt.Client(client_id="MES")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.connected = False
        self._connect_ts = None

        self._pending_wood = 0
        self._pending_metal = 0
        self._load_lock = threading.Lock()

        self._flushing = False

        # Local W1 inventory model. Used for fast dispatch decisions; snapped
        # to the PLC's W1Count by w1_sync_from_plc on the warehouse poll loop.
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

    def w1_sync_from_plc(self, counts: dict):
        """Overwrite the local W1 estimate with the PLC's authoritative
        W1Count[1..2] (Wood/Metal).

        This is the fix for the desync described in the brief: when the MES
        dispatches an order to a cell, it optimistically debits its local W1
        model — but if that cell's WarehouseOut conveyors are full, no raw
        piece actually leaves W1, so the order sits queued and the physical
        W1 still holds the material. The PLC's W1Count only drops when a piece
        REALLY exits W1 (WarehouseOut Receive_Workpiece_Data), so periodically
        snapping the local model back to it stops the MES from believing it
        consumed material it never did, and lets a stalled order's material
        be counted as available again once the order finally starts.
        """
        if not counts:
            return
        with self._w1_lock:
            changed = (self._w1_local != counts)
            self._w1_local = {"Wood": int(counts.get("Wood", 0)),
                              "Metal": int(counts.get("Metal", 0))}
            if changed:
                print(f"[w1] PLC sync -> wood:{self._w1_local['Wood']} "
                      f"metal:{self._w1_local['Metal']}")

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
        client.subscribe(TOPIC_DELIVERY_ORDERS)
        client.subscribe(TOPIC_END_OF_DAY)

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
        elif msg.topic == TOPIC_DELIVERY_ORDERS:
            self._handle_delivery_orders(payload)
        elif msg.topic == TOPIC_END_OF_DAY:
            self._handle_end_of_day(payload)

    # ---- delivery / unloading ------------------------------------------

    def _handle_delivery_orders(self, p):
        """ERP delivery order: place these finished pieces on the docks
        during the day. Dispatched to the unloader on the asyncio loop."""
        if self.unloader is None:
            print("[mqtt] delivery_orders received but no unloader wired")
            return
        items = p.get("items", [])
        if not items:
            return
        print(f"[mqtt] delivery_orders: {len(items)} item(s) "
              f"for sim_day {p.get('sim_day')}")
        asyncio.run_coroutine_threadsafe(
            self.unloader.handle_delivery_order(items), self.loop)

    def _handle_end_of_day(self, p):
        """ERP end-of-day notification: discharge every loaded dock."""
        if self.unloader is None:
            print("[mqtt] end_of_day received but no unloader wired")
            return
        print(f"[mqtt] end_of_day (sim_day {p.get('sim_day')}): "
              f"discharging docks")
        asyncio.run_coroutine_threadsafe(
            self.unloader.discharge_all(), self.loop)

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
        queued = total = 0
        # Safety net (v3 Bug 8B): never let the queue grow past MAX_QUEUED.
        depth = len(queued_pieces())
        for it in items:
            qty = int(it.get("quantity", 1))
            total += qty
            for _ in range(qty):
                if depth + queued >= MAX_QUEUED:
                    print(f"[mqtt] MES queue at cap ({MAX_QUEUED}); "
                          f"dropping remaining production order pieces "
                          f"({total - queued} not queued)")
                    print(f"[mqtt] queued {queued}/{total} pieces "
                          f"from production_orders")
                    return
                enqueue_piece(order_id=it.get("order_id") or order_id,
                              order_line_id=it.get("order_line_id"),
                              piece_type=it["piece_type"])
                queued += 1
        print(f"[mqtt] queued {queued} pieces from production_orders")

    # ---- status publishing ---------------------------------------------

    def publish_status(self, payload):
        msg = json.dumps(payload)
        self.client.publish(TOPIC_MES_STATUS, msg, qos=1)
        print(f"[mqtt-out] {TOPIC_MES_STATUS} -> {msg}")