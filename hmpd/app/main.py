import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import paho.mqtt.client as mqtt


OPTIONS_PATH = "/data/options.json"


def load_options() -> dict:
    with open(OPTIONS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


OPTIONS = load_options()

LOG_LEVEL = getattr(logging, str(OPTIONS.get("log_level", "INFO")).upper(), logging.INFO)
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("hmpd_bridge")

DEBUG_RAW = bool(OPTIONS.get("debug_raw_output", False))
DEBUG_DUMP = bool(OPTIONS.get("debug_dump_parsed_data", False))
DEBUG_SAVE = bool(OPTIONS.get("debug_save_log_file", True))
DEBUG_LOG_FILE = OPTIONS.get("debug_log_file", "/config/hmpd_bridge.log")


def append_debug_file(message: str) -> None:
    if not DEBUG_SAVE:
        return
    try:
        os.makedirs(os.path.dirname(DEBUG_LOG_FILE), exist_ok=True)
        with open(DEBUG_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(message.rstrip() + "\n")
    except Exception as exc:
        log.warning("Could not write debug file %s: %s", DEBUG_LOG_FILE, exc)


class FileLoggerHandler(logging.Handler):
    def emit(self, record):
        append_debug_file(self.format(record))


if DEBUG_SAVE:
    fh = FileLoggerHandler()
    fh.setLevel(LOG_LEVEL)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(fh)


@dataclass
class Controller:
    name: str
    dev: str
    baud: int


@dataclass
class Zone:
    controller_name: str
    controller_dev: str
    zone_index: int
    zone_name: str
    unique_id: str
    current_temp: Optional[float] = None
    target_temp: Optional[float] = None
    discovered: bool = False


class HMPDBridge:
    def __init__(self, options: dict):
        self.options = options
        self.discovery_prefix = options.get("mqtt_discovery_prefix", "homeassistant").strip("/")
        self.base_topic = options.get("mqtt_base_topic", "hmpd").strip("/")
        self.mqtt_host = options.get("mqtt_host", "")
        self.mqtt_port = int(options.get("mqtt_port", 1883))
        self.mqtt_username = options.get("mqtt_username", "")
        self.mqtt_password = options.get("mqtt_password", "")
        self.mqtt_client_id = options.get("mqtt_client_id", "hmpd_bridge")
        self.mqtt_keepalive = int(options.get("mqtt_keepalive", 60))
        self.mqtt_retry_seconds = int(options.get("mqtt_retry_seconds", 10))

        self.poll_interval = int(options.get("poll_interval", 10))
        self.reg_refresh_interval = int(options.get("reg_refresh_interval", 300))
        self.command_timeout = int(options.get("command_timeout", 15))
        self.temp_min = float(options.get("temp_min", 10.0))
        self.temp_max = float(options.get("temp_max", 35.5))
        self.temp_step = float(options.get("temp_step", 0.1))
        self.retain_discovery = bool(options.get("retain_discovery", True))
        self.retain_state = bool(options.get("retain_state", True))
        self.configured_hmpd_path = str(options.get("hmpd_path", "")).strip()

        self.controllers: List[Controller] = []
        for item in options.get("controllers", []):
            self.controllers.append(
                Controller(
                    name=item["name"],
                    dev=item["dev"],
                    baud=int(item["baud"]),
                )
            )

        self.zones: Dict[str, Zone] = {}
        self.command_lock = threading.Lock()
        self.last_regs_refresh = 0.0
        self.mqtt_connected = False

        self.mqtt = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=self.mqtt_client_id,
        )
        if self.mqtt_username:
            self.mqtt.username_pw_set(self.mqtt_username, self.mqtt_password)
        self.mqtt.on_connect = self.on_connect
        self.mqtt.on_disconnect = self.on_disconnect
        self.mqtt.on_message = self.on_message
        self.mqtt.will_set(f"{self.base_topic}/bridge/status", payload="offline", qos=1, retain=True)

        self.hmpd_path = self.find_hmpd()

    def slugify(self, value: str) -> str:
        value = value.strip().lower()
        value = re.sub(r"[^a-z0-9]+", "_", value)
        value = re.sub(r"_+", "_", value).strip("_")
        return value or "zone"

    def find_hmpd(self) -> str:
        candidates = []
        if self.configured_hmpd_path:
            candidates.append(self.configured_hmpd_path)
        candidates.extend([
            "/ha_config/hmpd",
            "/config/hmpd",
            "/homeassistant/hmpd",
            "/app/hmpd",
            "./hmpd",
        ])

        checked = []
        for path in candidates:
            checked.append(path)
            if os.path.isfile(path):
                if not os.access(path, os.X_OK):
                    try:
                        os.chmod(path, 0o755)
                    except Exception:
                        pass
                if os.access(path, os.X_OK):
                    log.info("Using hmpd binary: %s", path)
                    return path

        msg = "Could not find executable hmpd. Checked: " + ", ".join(checked)
        log.error(msg)
        raise FileNotFoundError(msg)

    def mqtt_connect_loop(self) -> None:
        while True:
            try:
                log.info("Connecting to MQTT broker %s:%s", self.mqtt_host, self.mqtt_port)
                self.mqtt_connected = False
                self.mqtt.connect(self.mqtt_host, self.mqtt_port, keepalive=self.mqtt_keepalive)
                self.mqtt.loop_start()

                timeout = time.time() + 10
                while time.time() < timeout:
                    if self.mqtt_connected:
                        return
                    time.sleep(0.2)

                log.error("MQTT connect timed out")
                self.mqtt.loop_stop()
                try:
                    self.mqtt.disconnect()
                except Exception:
                    pass
            except Exception as exc:
                log.error("MQTT connect failed: %s", exc)

            time.sleep(self.mqtt_retry_seconds)

    def on_connect(self, client, userdata, flags, reason_code, properties):
        reason_text = str(reason_code)

        if reason_text == "Success":
            self.mqtt_connected = True
            log.info("Connected to MQTT broker %s:%s", self.mqtt_host, self.mqtt_port)
            client.publish(f"{self.base_topic}/bridge/status", "online", qos=1, retain=True)
            client.subscribe(f"{self.base_topic}/+/set_target")
            client.subscribe(f"{self.base_topic}/bridge/resync")
        else:
            self.mqtt_connected = False
            log.error("MQTT authorization/connection failed: %s", reason_text)

    def on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        self.mqtt_connected = False
        log.warning("MQTT disconnected (reason=%s)", reason_code)

    def on_message(self, client, userdata, msg):
        topic = msg.topic
        payload = msg.payload.decode("utf-8", errors="ignore").strip()
        log.debug("MQTT message: %s => %s", topic, payload)

        if topic == f"{self.base_topic}/bridge/resync":
            self.sync_all_regs()
            self.sync_all_temps()
            return

        m = re.match(rf"^{re.escape(self.base_topic)}/([^/]+)/set_target$", topic)
        if not m:
            return

        zone_key = m.group(1)
        zone = self.zones.get(zone_key)
        if not zone:
            log.warning("Unknown zone key from MQTT: %s", zone_key)
            return

        try:
            value = float(payload)
        except ValueError:
            log.warning("Invalid target payload for %s: %s", zone_key, payload)
            return

        value = max(self.temp_min, min(self.temp_max, value))
        self.set_zone_target(zone, value)

    def run_hmpd(self, controller: Controller, action_args: List[str]) -> List[str]:
        cmd = [
            self.hmpd_path,
            "--dev", controller.dev,
            "--baud", str(controller.baud),
            *action_args,
        ]
        if DEBUG_RAW:
            log.debug("Running: %s", shlex.join(cmd))

        with self.command_lock:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.command_timeout,
                check=False,
            )

        stdout = (proc.stdout or "").replace("\r", "")
        stderr = (proc.stderr or "").replace("\r", "")

        if DEBUG_RAW and stdout.strip():
            log.debug("hmpd stdout:\n%s", stdout.strip())
        if DEBUG_RAW and stderr.strip():
            log.debug("hmpd stderr:\n%s", stderr.strip())

        if proc.returncode != 0:
            raise RuntimeError(f"hmpd exited {proc.returncode}: {stderr.strip() or stdout.strip()}")

        return [line.strip() for line in stdout.splitlines() if line.strip()]

    def parse_regs(self, controller: Controller, lines: List[str]) -> List[Zone]:
        zones: List[Zone] = []

        for idx, line in enumerate(lines):
            original = line
            line = re.sub(r"\s+", " ", line).strip()

            target = None
            m_temp = re.search(r"(-?\d+(?:\.\d+)?)", line)
            if m_temp:
                try:
                    target = float(m_temp.group(1))
                except Exception:
                    target = None

            name = line
            if m_temp:
                name = (line[:m_temp.start()] + line[m_temp.end():]).strip(" :-\t")
            if not name:
                name = f"Zone {idx + 1}"

            unique = f"{self.slugify(controller.name)}_{idx+1}"
            zone = self.zones.get(unique)
            if zone is None:
                zone = Zone(
                    controller_name=controller.name,
                    controller_dev=controller.dev,
                    zone_index=idx + 1,
                    zone_name=name,
                    unique_id=unique,
                )
            zone.zone_name = name
            if target is not None:
                zone.target_temp = target
            zones.append(zone)

            if DEBUG_DUMP:
                log.debug(
                    "REG parsed [%s] => idx=%s name=%s target=%s raw=%s",
                    controller.name, idx + 1, name, target, original
                )

        return zones

    def parse_temps(self, controller: Controller, lines: List[str]) -> Dict[int, float]:
        parsed: Dict[int, float] = {}
        zone_idx = 1

        for line in lines:
            original = line
            line = re.sub(r"\s+", " ", line).strip()

            matches = re.findall(r"(-?\d+(?:\.\d+)?)", line)
            if not matches:
                continue

            value = None
            for candidate in reversed(matches):
                try:
                    value = float(candidate)
                    break
                except Exception:
                    continue

            if value is None:
                continue

            parsed[zone_idx] = value

            if DEBUG_DUMP:
                log.debug(
                    "TEMP parsed [%s] => idx=%s temp=%s raw=%s",
                    controller.name, zone_idx, value, original
                )

            zone_idx += 1

        return parsed

    def discovery_payload(self, zone: Zone) -> dict:
        state_topic = f"{self.base_topic}/{zone.unique_id}/state"
        command_topic = f"{self.base_topic}/{zone.unique_id}/set_target"
        availability_topic = f"{self.base_topic}/bridge/status"

        return {
            "name": zone.zone_name,
            "unique_id": f"hmpd_{zone.unique_id}",
            "availability_topic": availability_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "current_temperature_topic": state_topic,
            "current_temperature_template": "{{ value_json.current_temp }}",
            "temperature_state_topic": state_topic,
            "temperature_state_template": "{{ value_json.target_temp }}",
            "temperature_command_topic": command_topic,
            "mode_state_topic": state_topic,
            "mode_state_template": "{{ value_json.mode }}",
            "modes": ["heat"],
            "min_temp": self.temp_min,
            "max_temp": self.temp_max,
            "temp_step": self.temp_step,
            "device": {
                "identifiers": [f"hmpd_{zone.unique_id}"],
                "name": zone.zone_name,
                "manufacturer": "HMPD",
                "model": "Thermostat Regulator",
                "via_device": f"hmpd_bridge_{self.slugify(zone.controller_name)}",
            },
        }

    def publish_discovery(self, zone: Zone) -> None:
        topic = f"{self.discovery_prefix}/climate/hmpd_{zone.unique_id}/config"
        payload = self.discovery_payload(zone)
        self.mqtt.publish(topic, json.dumps(payload), qos=1, retain=self.retain_discovery)
        zone.discovered = True
        log.info("Published discovery for %s", zone.zone_name)

    def publish_state(self, zone: Zone) -> None:
        topic = f"{self.base_topic}/{zone.unique_id}/state"
        payload = {
            "current_temp": zone.current_temp,
            "target_temp": zone.target_temp,
            "mode": "heat",
        }
        self.mqtt.publish(topic, json.dumps(payload), qos=1, retain=self.retain_state)

    def sync_regs(self, controller: Controller) -> None:
        lines = self.run_hmpd(controller, ["regs"])
        zones = self.parse_regs(controller, lines)

        for zone in zones:
            self.zones[zone.unique_id] = zone
            if not zone.discovered:
                self.publish_discovery(zone)
            self.publish_state(zone)

        log.info("Synced %s zones from regs for controller %s", len(zones), controller.name)

    def sync_temps(self, controller: Controller) -> None:
        lines = self.run_hmpd(controller, ["temps"])
        temps = self.parse_temps(controller, lines)

        updated = 0
        for zone in self.zones.values():
            if zone.controller_name != controller.name:
                continue
            if zone.zone_index in temps:
                zone.current_temp = temps[zone.zone_index]
                self.publish_state(zone)
                updated += 1

        log.info("Synced %s temperatures for controller %s", updated, controller.name)

    def sync_all_regs(self) -> None:
        for controller in self.controllers:
            try:
                self.sync_regs(controller)
            except Exception as exc:
                log.error("sync_regs failed for %s: %s", controller.name, exc)

    def sync_all_temps(self) -> None:
        for controller in self.controllers:
            try:
                self.sync_temps(controller)
            except Exception as exc:
                log.error("sync_temps failed for %s: %s", controller.name, exc)

    def set_zone_target(self, zone: Zone, target: float) -> None:
        controller = next(c for c in self.controllers if c.name == zone.controller_name)
        self.run_hmpd(controller, ["set", str(zone.zone_index), f"{target:.1f}"])
        zone.target_temp = target
        self.publish_state(zone)
        log.info("Set %s to %.1f", zone.zone_name, target)

    def start(self):
        log.info("=== HMPD Thermostat Bridge starting ===")
        for controller in self.controllers:
            log.info("Configured controller %s -> %s @ %s", controller.name, controller.dev, controller.baud)

        self.mqtt_connect_loop()
        self.sync_all_regs()
        self.sync_all_temps()
        self.last_regs_refresh = time.monotonic()

        while True:
            now = time.monotonic()
            if now - self.last_regs_refresh >= self.reg_refresh_interval:
                self.sync_all_regs()
                self.last_regs_refresh = now

            self.sync_all_temps()
            time.sleep(self.poll_interval)


if __name__ == "__main__":
    HMPDBridge(OPTIONS).start()
