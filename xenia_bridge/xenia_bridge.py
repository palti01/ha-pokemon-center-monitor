"""Xenia Bridge: bindet die Mahlkönig Xenia lokal per MQTT-Discovery in Home Assistant ein.

Die Xenia hat eine lokale Web-API (dieselbe, die ihre Weboberfläche benutzt):
  GET  /api/v2/diagram/get        -> Live-Daten als JSON (MA_STATUS 1 = an, 0 = aus)
  POST /api/v2/machine/control    -> Body {"action":"1"} an, {"action":"0"} aus
Der Body ist JSON-Text, gesendet als application/x-www-form-urlencoded (wie im Browser).
"""

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

import paho.mqtt.client as mqtt

log = logging.getLogger("xenia")

BASE = "xenia"
DISCOVERY = "homeassistant"
AVAIL_TOPIC = f"{BASE}/availability"
STATE_TOPIC = f"{BASE}/state"
CMD_TOPIC = f"{BASE}/power/set"

DEVICE = {
    "identifiers": ["mahlkoenig_xenia"],
    "name": "Xenia",
    "manufacturer": "Mahlkönig",
    "model": "Xenia",
}


def load_options(path="/data/options.json"):
    opts = {"host": "192.168.178.84", "poll_interval_s": 15}
    try:
        with open(path, encoding="utf-8") as f:
            opts.update({k: v for k, v in json.load(f).items() if v not in (None, "")})
    except FileNotFoundError:
        pass
    return opts


def mqtt_credentials(opts):
    """MQTT-Zugang vom Supervisor holen (Mosquitto-App), sonst aus den Optionen."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if token and not opts.get("mqtt_host"):
        req = urllib.request.Request(
            "http://supervisor/services/mqtt", headers={"Authorization": f"Bearer {token}"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.load(r)["data"]
        return d["host"], int(d["port"]), d.get("username"), d.get("password")
    return (
        opts.get("mqtt_host", "localhost"),
        int(opts.get("mqtt_port", 1883)),
        opts.get("mqtt_user"),
        opts.get("mqtt_password"),
    )


class Xenia:
    def __init__(self, host, timeout=6):
        self.base = f"http://{host}"
        self.timeout = timeout
        self.lock = threading.Lock()

    def status(self):
        req = urllib.request.Request(
            f"{self.base}/api/v2/diagram/get",
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
        )
        with self.lock, urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.load(r)

    def set_power(self, on):
        body = json.dumps({"action": "1" if on else "0"}, separators=(",", ":")).encode()
        req = urllib.request.Request(
            f"{self.base}/api/v2/machine/control",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        with self.lock, urllib.request.urlopen(req, timeout=self.timeout) as r:
            return r.read().decode(errors="replace")


def discovery_messages():
    avail = {"availability_topic": AVAIL_TOPIC, "device": DEVICE}
    msgs = {
        f"{DISCOVERY}/switch/xenia/power/config": {
            **avail,
            "name": None,  # Entität heißt wie das Gerät -> switch.xenia
            "unique_id": "xenia_power",
            "default_entity_id": "switch.xenia",
            "icon": "mdi:coffee-maker",
            "command_topic": CMD_TOPIC,
            "state_topic": STATE_TOPIC,
            "value_template": "{{ 'ON' if (value_json.MA_STATUS | int(0)) == 1 else 'OFF' }}",
        },
    }
    sensors = [
        ("brew_boiler", "Brühboiler", "BB_SENS_TEMP_A", "°C", "temperature", None),
        ("brew_group", "Brühgruppe", "BG_SENS_TEMP_A", "°C", "temperature", None),
        ("steam_pressure", "Dampfboiler-Druck", "SB_SENS_PRESS", "bar", "pressure", None),
        ("last_shot", "Letzte Bezugszeit", "EXTRACTION_TIME_MS", "s", "duration", "/ 1000"),
    ]
    for key, name, field, unit, dc, conv in sensors:
        tpl = f"{{{{ value_json.{field} }}}}" if not conv else f"{{{{ (value_json.{field} | float(0)) {conv} }}}}"
        msgs[f"{DISCOVERY}/sensor/xenia/{key}/config"] = {
            **avail,
            "name": name,
            "unique_id": f"xenia_{key}",
            "default_entity_id": f"sensor.xenia_{key}",
            "state_topic": STATE_TOPIC,
            "value_template": tpl,
            "unit_of_measurement": unit,
            "device_class": dc,
            "state_class": "measurement",
            "suggested_display_precision": 1,
        }
    return msgs


class Bridge:
    def __init__(self, opts):
        self.opts = opts
        self.xenia = Xenia(opts["host"])
        self.poll_now = threading.Event()
        self.online = None
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="xenia_bridge")
        self.client.will_set(AVAIL_TOPIC, "offline", retain=True)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

    def on_connect(self, client, userdata, flags, reason_code, properties):
        log.info("MQTT verbunden (%s)", reason_code)
        client.subscribe(CMD_TOPIC)
        client.subscribe(f"{DISCOVERY}/status")
        self.publish_discovery()
        self.online = None  # Verfügbarkeit nach (Wieder-)Verbindung neu melden
        self.poll_now.set()

    def publish_discovery(self):
        for topic, payload in discovery_messages().items():
            self.client.publish(topic, json.dumps(payload, ensure_ascii=False), retain=True)

    def on_message(self, client, userdata, msg):
        payload = msg.payload.decode(errors="replace").strip()
        if msg.topic == f"{DISCOVERY}/status":
            if payload == "online":  # HA neu gestartet -> Discovery erneut senden
                self.publish_discovery()
                self.online = None
                self.poll_now.set()
            return
        if msg.topic == CMD_TOPIC and payload in ("ON", "OFF"):
            try:
                resp = self.xenia.set_power(payload == "ON")
                log.info("Befehl %s gesendet, Antwort: %s", payload, resp)
            except (urllib.error.URLError, OSError) as e:
                log.warning("Befehl %s fehlgeschlagen: %s", payload, e)
            self.poll_now.set()

    def set_online(self, online):
        if online != self.online:
            self.client.publish(AVAIL_TOPIC, "online" if online else "offline", retain=True)
            log.info("Xenia %s", "erreichbar" if online else "nicht erreichbar")
            self.online = online

    def poll(self):
        try:
            data = self.xenia.status()
        except (urllib.error.URLError, OSError, ValueError) as e:
            log.debug("Abruf fehlgeschlagen: %s", e)
            self.set_online(False)
            return
        self.client.publish(STATE_TOPIC, json.dumps(data), retain=True)
        self.set_online(True)

    def run(self):
        host, port, user, pw = mqtt_credentials(self.opts)
        if user:
            self.client.username_pw_set(user, pw)
        self.client.connect_async(host, port, keepalive=60)
        self.client.loop_start()
        interval = int(self.opts.get("poll_interval_s", 15))
        fast_until = 0.0
        while True:
            triggered = self.poll_now.wait(timeout=2 if time.monotonic() < fast_until else interval)
            self.poll_now.clear()
            if triggered:
                time.sleep(1)  # Maschine kurz schalten lassen
                fast_until = time.monotonic() + 20  # nach Befehl 20 s lang alle 2 s abfragen
            if self.client.is_connected():
                self.poll()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    opts = load_options()
    log.info("Xenia Bridge startet, Maschine: %s", opts["host"])
    Bridge(opts).run()


if __name__ == "__main__":
    main()
