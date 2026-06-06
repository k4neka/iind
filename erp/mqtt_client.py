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
                    MQTT_TOPIC_MATERIAL_LOAD,
                    MQTT_TOPIC_END_OF_DAY)
from database import log_mes_status, update_inventory

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
        # piece_db_id of every COMPLETED already counted this run. Belt-and-
        # suspenders against a duplicate completion from the CODESYS corridor
        # defect (§7.1): even if a stray re-report slips past the MES dedupe,
        # the ERP must not double-count inventory / drive raw stock negative.
        self._counted_pieces = set()
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
            return
        self._sync_inventory(payload)

    def _sync_inventory(self, raw_payload):
        """Keep the ERP inventory table in lock-step with the plant.

        Each COMPLETED piece adds +1 finished product to stock. Raw material
        is NOT decremented here: it is consumed at dispatch time in
        dispatch_today (v3 Bug 4), mirroring the MES W1 drain, so the ERP raw
        inventory matches the physical floor. Deduped by piece_db_id so a
        re-reported completion can't double-count (§7.1).
        """
        try:
            data = json.loads(raw_payload)
        except Exception:
            return
        if data.get("status") != "COMPLETED":
            return
        piece = data.get("piece_type")
        if not piece:
            return
        pdid = data.get("piece_db_id")
        if pdid is not None:
            if pdid in self._counted_pieces:
                print(f"[erp] inventory sync: piece_db_id={pdid} already "
                      f"counted this run; ignoring duplicate COMPLETED")
                return
            self._counted_pieces.add(pdid)
        update_inventory(piece, 1)
        print(f"[erp] inventory sync: +1 {piece} (finished)")

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
    
    def send_end_of_day(self, sim_day):
        """Tell the MES the sim day has ended so it discharges every loaded
        unloading dock (PDF §2.4 / §4.1: pieces are placed during the day and
        unloaded automatically at the end of the day). The ERP owns the sim
        clock, so it is the authority on when a day ends."""
        self._publish(MQTT_TOPIC_END_OF_DAY, {"sim_day": sim_day})
        