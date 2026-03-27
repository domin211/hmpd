import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import paho.mqtt.client as mqtt


OPTIONS_PATH = "/data/options.json"

# Hardcoded config
MQTT_HOST = "core-mosquitto"
MQTT_PORT = 1883
MQTT_USERNAME = "ufandy"
MQTT_PASSWORD = "Fanda18067"
MQTT_DISCOVERY_PREFIX = "homeassistant"
MQTT_BASE_TOPIC = "hmpd"
MQTT_CLIENT_ID = "hmpd_bridge"
MQTT_KEEPALIVE = 60
MQTT_RETRY_SECONDS = 10

# Your original script already used both usb0 and usb1. :contentReference[oaicite:0]{index=0}
CONTROLLERS = [
    {"name": "usb0", "dev": "/dev/ttyUSB0", "baud": 4800},
    {"name": "usb1", "dev": "/dev/ttyUSB1", "baud": 4800},
]

POLL_INTERVAL = 10
REG_REFRESH_INTERVAL = 300
COMMAND_TIMEOUT = 60

TEMP_MIN = 16.0
TEMP_MAX = 32.0
TEMP_STEP = 0.1

# Current-temperature sanity filter for deciding whether a thermostat exists at all.
CURRENT_TEMP_MIN = 0.0
CURRENT_TEMP_MAX = 100.0

HMPD_PATH = "/homeassistant/hmpd"

RETAIN_DISCOVERY = True
RETAIN_STATE = True

DEBUG_LOG_FILE = "/config/hmpd_bridge.log"


def load_options() -> dict:
    try:
        with open(OPTIONS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"debug": False}


OPTIONS = load_options()
DEBUG = bool(OPTIONS.get("debug", False))
LOG_LEVEL = logging.DEBUG if DEBUG else logging.INFO

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("hmpd_bridge")


def append_debug_file(message: str) -> None:
    if not DEBUG:
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


if DEBUG:
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
    enabled: Optional[bool] = None
    discovered: bool = False


class HMPDBridge:
    def __init__(self):
        self.discovery_prefix = MQTT_DISCOVERY_PREFIX
        self.base_topic = MQTT_BASE_TOPIC
        self.mqtt_host = MQTT_HOST
        self.mqtt_port = MQTT_PORT
        self.mqtt_username = MQTT_USERNAME
        self.mqtt_password = MQTT_PASSWORD
        self.mqtt_client_id = MQTT_CLIENT_ID
        self.mqtt_keepalive = MQTT_KEEPALIVE
        self.mqtt_retry_seconds = MQTT_RETRY_SECONDS

        self.poll_interval = POLL_INTERVAL
        self.reg_refresh_interval = REG_REFRESH_INTERVAL
        self.command_timeout = COMMAND_TIMEOUT
        self.temp_min = TEMP_MIN
        self.temp_max = TEMP_MAX
        self.temp_step = TEMP_STEP
        self.retain_discovery = RETAIN_DISCOVERY
        self.retain_state = RETAIN_STATE
        self.configured_hmpd_path = HMPD_PATH

        self.controllers: List[Controller] = [
            Controller(name=item["name"], dev=item["dev"], baud=int(item["baud"]))
            for item in CONTROLLERS
        ]

        self.zones: Dict[str, Zone] = {}
        self.command_lock = threading.Lock()
        self.last_regs_refresh = 0.0
        self.mqtt_connected = False

        self.mqtt = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=self.mqtt_client_id,
        )
        self.mqtt.username_pw_set(self.mqtt_username, self.mqtt_password)
        self.mqtt.on_connect = self.on_connect
        self.mqtt.on_disconnect = self.on_disconnect
        self.mqtt.on_message = self.on_message

        self.hmpd_path = ""
        self.stdbuf_path = shutil.which("stdbuf")

    def slugify(self, value: str) -> str:
        value = value.strip().lower()
        value = re.sub(r"[^a-z0-9]+", "_", value)
        value = re.sub(r"_+", "_", value).strip("_")
        return value or "zone"

    def hmpd_candidates(self) -> List[str]:
        candidates = [
            self.configured_hmpd_path,
            "/homeassistant/hmpd",
            "/config/hmpd",
            "/ha_config/hmpd",
            "/app/hmpd",
            "./hmpd",
        ]
        seen = set()
        ordered: List[str] = []
        for path in candidates:
            if path and path not in seen:
                ordered.append(path)
                seen.add(path)
        return ordered

    def find_hmpd(self) -> str:
        checked = []
        for path in self.hmpd_candidates():
            checked.append(path)
            if os.path.isfile(path):
                if not os.access(path, os.X_OK):
                    try:
                        os.chmod(path, 0o755)
                    except Exception:
                        pass
                if os.access(path, os.X_OK):
                    if self.hmpd_path != path:
                        log.info("Using hmpd binary: %s", path)
                    self.hmpd_path = path
                    return path

        msg = "Could not find executable hmpd. Checked: " + ", ".join(checked)
        raise FileNotFoundError(msg)

    def ensure_hmpd(self) -> str:
        if self.hmpd_path and os.path.isfile(self.hmpd_path) and os.access(self.hmpd_path, os.X_OK):
            return self.hmpd_path
        return self.find_hmpd()

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
        if DEBUG:
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

    def build_hmpd_cmd(self, controller: Controller, action_args: List[str]) -> List[str]:
        hmpd = self.ensure_hmpd()
        base = [hmpd, "--dev", controller.dev, "--baud", str(controller.baud), *action_args]
        if self.stdbuf_path:
            return [self.stdbuf_path, "-oL", *base]
        return base

    def run_hmpd(self, controller: Controller, action_args: List[str]) -> List[str]:
        cmd = self.build_hmpd_cmd(controller, action_args)
        if DEBUG:
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

        if DEBUG and stdout.strip():
            log.debug("hmpd stdout:\n%s", stdout.strip())
        if DEBUG and stderr.strip():
            log.debug("hmpd stderr:\n%s", stderr.strip())

        if proc.returncode != 0:
            raise RuntimeError(f"hmpd exited {proc.returncode}: {stderr.strip() or stdout.strip()}")

        return [line.strip() for line in stdout.splitlines() if line.strip()]

    def valid_current_temp(self, value: float) -> bool:
        return CURRENT_TEMP_MIN <= value <= CURRENT_TEMP_MAX

    def parse_regs(self, controller: Controller, lines: List[str]) -> List[Zone]:
        zones: List[Zone] = []

        for line in lines:
            original = line
            try:
                parts = [p.strip() for p in line.split("|")]
                if len(parts) < 5:
                    continue

                idx = int(parts[0])
                name = parts[1].strip()
                if not name:
                    continue

                current_temp = None
                target_temp = None

                m_cur = re.search(r"cur:\s*(-?\d+(?:\.\d+)?)", parts[2])
                if m_cur:
                    current_temp = float(m_cur.group(1))

                m_tgt = re.search(r"tgt:\s*(-?\d+(?:\.\d+)?)", parts[3])
                if m_tgt:
                    target_temp = float(m_tgt.group(1))

                enabled = "EN" in parts[4]

                if current_temp is None or not self.valid_current_temp(current_temp):
                    if DEBUG:
                        log.debug(
                            "Skipping zone with invalid current temp [%s] idx=%s name=%s cur=%s raw=%s",
                            controller.name, idx, name, current_temp, original
                        )
                    continue

                unique = f"{self.slugify(controller.name)}_{idx}"
                zone = self.zones.get(unique)
                if zone is None:
                    zone = Zone(
                        controller_name=controller.name,
                        controller_dev=controller.dev,
                        zone_index=idx,
                        zone_name=name,
                        unique_id=unique,
                    )

                zone.zone_name = name
                zone.current_temp = round(current_temp, 1)
                zone.enabled = enabled

                if target_temp is not None:
                    zone.target_temp = round(
                        max(self.temp_min, min(self.temp_max, target_temp)),
                        1,
                    )

                zones.append(zone)

                if DEBUG:
                    log.debug(
                        "REG parsed [%s] => idx=%s name=%s cur=%s tgt=%s enabled=%s raw=%s",
                        controller.name,
                        idx,
                        name,
                        zone.current_temp,
                        zone.target_temp,
                        enabled,
                        original,
                    )
            except Exception as exc:
                log.error("REG parse error [%s]: %s | raw=%s", controller.name, exc, original)

        return zones

    def parse_temps(self, controller: Controller, lines: List[str]) -> Dict[int, float]:
        parsed: Dict[int, float] = {}

        for line in lines:
            original = line
            try:
                idx_str, val_str = line.split(":", 1)
                idx = int(idx_str.strip())
                val = float(val_str.strip())

                if not self.valid_current_temp(val):
                    continue

                parsed[idx] = round(val, 1)

                if DEBUG:
                    log.debug(
                        "TEMP parsed [%s] => idx=%s temp=%s raw=%s",
                        controller.name, idx, parsed[idx], original
                    )
            except Exception as exc:
                log.error("TEMP parse error [%s]: %s | raw=%s", controller.name, exc, original)

        return parsed

    def remove_zone(self, unique_id: str) -> None:
        discovery_topic = f"{self.discovery_prefix}/climate/hmpd_{unique_id}/config"
        state_topic = f"{self.base_topic}/{unique_id}/state"
        self.mqtt.publish(discovery_topic, "", qos=1, retain=True)
        self.mqtt.publish(state_topic, "", qos=1, retain=True)
        if unique_id in self.zones:
            del self.zones[unique_id]
        log.info("Removed thermostat %s", unique_id)

    def remove_stale_zones_for_controller(self, controller: Controller, valid_ids: set[str]) -> None:
        to_remove = [
            unique_id
            for unique_id, zone in self.zones.items()
            if zone.controller_name == controller.name and unique_id not in valid_ids
        ]

        for unique_id in to_remove:
            self.remove_zone(unique_id)

    def discovery_payload(self, zone: Zone) -> dict:
        state_topic = f"{self.base_topic}/{zone.unique_id}/state"
        command_topic = f"{self.base_topic}/{zone.unique_id}/set_target"

        return {
            "name": zone.zone_name,
            "unique_id": f"hmpd_{zone.unique_id}",
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
        if zone.current_temp is None or not self.valid_current_temp(zone.current_temp):
            self.remove_zone(zone.unique_id)
            return

        topic = f"{self.base_topic}/{zone.unique_id}/state"
        payload = {
            "current_temp": zone.current_temp,
            "target_temp": zone.target_temp if zone.target_temp is not None else self.temp_min,
            "mode": "heat",
        }
        self.mqtt.publish(topic, json.dumps(payload), qos=1, retain=self.retain_state)

    def sync_regs(self, controller: Controller) -> None:
        lines = self.run_hmpd(controller, ["regs"])
        zones = self.parse_regs(controller, lines)

        valid_ids: set[str] = set()
        for zone in zones:
            valid_ids.add(zone.unique_id)
            self.zones[zone.unique_id] = zone
            self.publish_discovery(zone)
            self.publish_state(zone)

        self.remove_stale_zones_for_controller(controller, valid_ids)
        log.info("Synced %s valid named zones from regs for controller %s", len(zones), controller.name)

    def sync_temps(self, controller: Controller) -> None:
        lines = self.run_hmpd(controller, ["temps"])
        temps = self.parse_temps(controller, lines)

        valid_temp_ids = {
            f"{self.slugify(controller.name)}_{idx}"
            for idx in temps.keys()
        }

        to_remove = [
            unique_id
            for unique_id, zone in self.zones.items()
            if zone.controller_name == controller.name and unique_id not in valid_temp_ids
        ]
        for unique_id in to_remove:
            self.remove_zone(unique_id)

        updated = 0
        for zone in list(self.zones.values()):
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
        self.find_hmpd()
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
    HMPDBridge().start()
