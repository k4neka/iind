"""MQTT bridge: subscribes to ERP topics and publishes MES status."""
import json
import threading

import paho.mqtt.client as mqtt

from config import (MQTT_BROKER, MQTT_PORT,
                    TOPIC_MATERIAL_LOAD, TOPIC_PRODUCTION_ORDERS,
                    TOPIC_MES_STATUS)
from database import enqueue_piece


class MESMqtt:
    def __init__(self, loop, plc):
        self.loop = loop           # asyncio loop (to schedule PLC writes)
        self.plc = plc
        self.client = mqtt.Client(client_id="MES")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.connected = False

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
        client.subscribe(TOPIC_MATERIAL_LOAD)
        client.subscribe(TOPIC_PRODUCTION_ORDERS)

    # ---- Inbound messages ----
    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception as e:
            print(f"[mqtt] bad payload on {msg.topic}: {e}")
            return

        if msg.topic == TOPIC_MATERIAL_LOAD:
            self._handle_material_load(payload)
        elif msg.topic == TOPIC_PRODUCTION_ORDERS:
            self._handle_production_orders(payload)

    def _handle_material_load(self, p):
        """Forward to PLC Loader. Aggregates qty per material type."""
        wood = metal = 0
        if p.get("type") == "Wood":
            wood = int(p.get("quantity", 0))
        elif p.get("type") == "Metal":
            metal = int(p.get("quantity", 0))
        print(f"[mqtt] material_load wood={wood} metal={metal}")

        async def _run():
            try:
                await self.plc.trigger_loader(wood, metal)
            except Exception as e:
                print(f"[mqtt] loader trigger failed: {e}")
        import asyncio
        asyncio.run_coroutine_threadsafe(_run(), self.loop)

    def _handle_production_orders(self, p):
        """Expects: {"sim_day": N, "items": [{order_line_id, piece_type, quantity, ...}, ...]}"""
        items = p.get("items", [])
        order_id = p.get("order_id")  # optional aggregate id
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