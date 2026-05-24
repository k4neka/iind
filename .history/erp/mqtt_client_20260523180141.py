"""MQTT bridge to the MES (publishes orders, subscribes to status)."""
import json
import time
import threading
import uuid

from config import (MQTT_BROKER, MQTT_PORT,
                    MQTT_TOPIC_PRODUCTION, MQTT_TOPIC_DELIVERY,
                    MQTT_TOPIC_MES_STATUS,
                    MQTT_TOPIC_MATERIAL_LOAD_WOOD,
                    MQTT_TOPIC_MATERIAL_LOAD_METAL,
                    MQTT_TOPIC_MATERIAL_LOAD)
from database import log_mes_status

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None


class MQTTBridge:
    def __init__(self):
        self.connected = False
        self.client = None
        if mqtt is None:
            print("[mqtt] paho-mqtt not installed; running in stub mode")
            return
        self.client = mqtt.Client(client_id="ERP")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, rc):
        self.connected = (rc == 0)
        print(f"[mqtt] connected rc={rc}")
        client.subscribe(MQTT_TOPIC_MES_STATUS)

    def _on_message(self, client, userdata, msg):
        try:
            payload = msg.payload.decode("utf-8")
            log_mes_status(time.time(), msg.topic, payload)
        except Exception as e:
            print(f"[mqtt] on_message error: {e}")

    def start(self):
        if self.client is None:
            return
        try:
            self.client.connect(MQTT_BROKER, MQTT_PORT, 60)
            threading.Thread(target=self.client.loop_forever, daemon=True).start()
        except Exception as e:
            print(f"[mqtt] could not connect to {MQTT_BROKER}:{MQTT_PORT} ({e})")

    def _publish(self, topic, payload, retain=False):
        msg = json.dumps(payload)
        if self.client and self.connected:
            self.client.publish(topic, msg, qos=1, retain=retain)
        print(f"[mqtt-out] {topic} -> {msg}")

    def send_production_order(self, sim_day, items):
        self._publish(MQTT_TOPIC_PRODUCTION,
                      {"sim_day": sim_day, "items": items})

    def send_delivery_order(self, sim_day, items):
        self._publish(MQTT_TOPIC_DELIVERY,
                      {"sim_day": sim_day, "items": items})

    def send_material_load_command(self, material_type, quantity):
        """Tell the MES to activate cell L and load raw material into W1.
        Uses retain=True so the message survives MES restarts.
        Each material has its own topic to avoid retain collision."""
        if material_type == "Wood":
            topic = MQTT_TOPIC_MATERIAL_LOAD_WOOD
        elif material_type == "Metal":
            topic = MQTT_TOPIC_MATERIAL_LOAD_METAL
        else:
            topic = MQTT_TOPIC_MATERIAL_LOAD

        self._publish(topic, {
            "command":    "load_material",
            "type":       material_type,
            "quantity":   quantity,
            "message_id": str(uuid.uuid4()),
        }, retain=True)