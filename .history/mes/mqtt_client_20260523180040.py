"""MQTT bridge: subscribes to ERP topics and publishes MES status."""
import asyncio
import json
import threading

import paho.mqtt.client as mqtt

from config import (MQTT_BROKER, MQTT_PORT,
                    TOPIC_MATERIAL_LOAD, TOPIC_MATERIAL_LOAD_WOOD,
                    TOPIC_MATERIAL_LOAD_METAL,
                    TOPIC_PRODUCTION_ORDERS, TOPIC_MES_STATUS)
from database import enqueue_piece


class MESMqtt:
    def __init__(self, loop, plc):
        self.loop = loop
        self.plc = plc
        self.client = mqtt.Client(client_id="MES")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.connected = False

        # Buffer agregador de pedidos de material
        self._pending_wood = 0
        self._pending_metal = 0
        self._load_lock = threading.Lock()

        # Dedup de mensagens retidas (evita reprocessar em restart)
        self._seen_msg_ids = set()

        # Flag para evitar 2 flush_loader em paralelo
        self._flushing = False

    # ---- Lifecycle ----
    def start(self):
        try:
            self.client.connect(MQTT_BROKER, MQTT_PORT, 60)
            threading.Thread(target=self.client.loop_forever,
                             daemon=True).start()
        except Exception as e:
            print(f"[mqtt] could not connect: {e}")

    def _on_connect(self, client, userdata, flags, rc):
        self.connected = (rc == 0)
        print(f"[mqtt] connected rc={rc}")
        client.subscribe(TOPIC_MATERIAL_LOAD)            # legado
        client.subscribe(TOPIC_MATERIAL_LOAD_WOOD)
        client.subscribe(TOPIC_MATERIAL_LOAD_METAL)
        client.subscribe(TOPIC_PRODUCTION_ORDERS)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception as e:
            print(f"[mqtt] bad payload on {msg.topic}: {e}")
            return

        if msg.topic.startswith("factory/erp/material_load"):
            self._handle_material_load(payload)
        elif msg.topic == TOPIC_PRODUCTION_ORDERS:
            self._handle_production_orders(payload)

    # ---- Inbound: material_load ----
    def _handle_material_load(self, p):
        # Dedup via message_id (mensagens retidas reentram em cada arranque)
        msg_id = p.get("message_id")
        if msg_id and msg_id in self._seen_msg_ids:
            print(f"[mqtt] material_load ignored (duplicate id={msg_id[:8]}…)")
            return
        if msg_id:
            self._seen_msg_ids.add(msg_id)

        w_added = m_added = 0
        if "items" in p:
            for it in p["items"]:
                t = it.get("type"); q = int(it.get("quantity", 0))
                if t == "Wood":  w_added += q
                elif t == "Metal": m_added += q
        else:
            t = p.get("type"); q = int(p.get("quantity", 0))
            if t == "Wood":  w_added = q
            elif t == "Metal": m_added = q

        with self._load_lock:
            self._pending_wood  += w_added
            self._pending_metal += m_added
            print(f"[mqtt] material_load +wood={w_added} +metal={m_added} "
                  f"(buffer: wood={self._pending_wood} metal={self._pending_metal})")

        asyncio.run_coroutine_threadsafe(self.flush_loader(), self.loop)

    async def flush_loader(self):
        """Despacha o buffer de material para o PLC em lotes de 5."""
        if self._flushing:
            return
        try:
            self._flushing = True

            status = await self.plc.read_loader_status()
            if status != 0:
                with self._load_lock:
                    w, m = self._pending_wood, self._pending_metal
                if w > 0 or m > 0:
                    print(f"[mqtt] flush_loader skipped: PLC busy (status={status}), "
                          f"buffer kept (wood={w} metal={m})")
                return

            with self._load_lock:
                w, m = self._pending_wood, self._pending_metal
                if w == 0 and m == 0:
                    return
                self._pending_wood = 0
                self._pending_metal = 0

            print(f"[mqtt] flush_loader dispatching wood={w} metal={m} (batched)")
            await self.plc.trigger_loader_batched(w, m)
        except Exception as e:
            print(f"[mqtt] flush_loader error: {e}")
        finally:
            self._flushing = False

    # ---- Inbound: production_orders ----
    def _handle_production_orders(self, p):
        items = p.get("items", [])
        order_id = p.get("order_id")
        for it in items:
            qty = int(it.get("quantity", 1))
            for _ in range(qty):
                enqueue_piece(order_id=it.get("order_id") or order_id,
                              order_line_id=it.get("order_line_id"),
                              piece_type=it["piece_type"])
        print(f"[mqtt] queued {sum(int(i.get('quantity',1)) for i in items)} pieces")

    # ---- Outbound ----
    def publish_status(self, payload: dict):
        msg = json.dumps(payload)
        self.client.publish(TOPIC_MES_STATUS, msg, qos=1)
        print(f"[mqtt-out] {TOPIC_MES_STATUS} -> {msg}")