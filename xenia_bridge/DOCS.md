# Xenia Bridge

Verbindet die Mahlkönig Xenia über ihre lokale Web-API mit Home Assistant. Kein Cloud-Zugang, keine YAML-Konfiguration.

Nach dem Start legt die App per MQTT-Discovery das Gerät **Xenia** an:

| Entität | Inhalt |
|---|---|
| `switch.xenia` | Maschine ein/aus |
| `sensor.xenia_brew_boiler` | Brühboiler-Temperatur |
| `sensor.xenia_brew_group` | Brühgruppen-Temperatur |
| `sensor.xenia_steam_pressure` | Dampfboiler-Druck |
| `sensor.xenia_last_shot` | Dauer des letzten Bezugs |

Voraussetzung: Mosquitto-App und MQTT-Integration laufen. Die Zugangsdaten holt sich die App selbst.

Optionen: `host` (IP der Xenia, Standard 192.168.178.84), `poll_interval_s` (Abfrageintervall, Standard 15 s).
