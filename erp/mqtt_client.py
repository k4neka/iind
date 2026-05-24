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


# Topics whose retained payload must be wiped on ERP startup so the
# MES never picks up ghosts from a previous run.
_RETAINED_TOPICS = [
    MQTT_TOPIC_MATERIAL_LOAD_WOOD,
    MQTT_TOPIC_MATERIAL_LOAD_METAL,
    MQTT_TOPIC_MATERIAL_LOAD,
]


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
        # Wipe stale retained messages so the MES doesn't replay them.
        self._wipe_retained()

    def _wipe_retained(self):
        # Publishing an empty payload with retain=True deletes the
        # broker-stored retained message for that topic.
        for topic in _RETAINED_TOPICS:
            self.client.publish(topic, payload=b"", qos=1, retain=True)
        print("[mqtt] wiped retained messages on material_load topics")

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
            threading.Thread(target=self.client.loop_forever,
                             daemon=True).start()
        except Exception as e:
            print(f"[mqtt] could not connect to "
                  f"{MQTT_BROKER}:{MQTT_PORT} ({e})")

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
        # Retain=True so the MES can still consume the command if it
        # boots a few seconds after the ERP. The ERP wipes retained
        # messages at startup so old runs never bleed into new ones.
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