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
from typing import Dict, List, Optional, Tuple

import paho.mqtt.client as mqtt


OPTIONS_PATH = "/data/options.json"

MQTT_HOST = "core-mosquitto"
MQTT_PORT = 1883
MQTT_USERNAME = "ufandy"
MQTT_PASSWORD = "Fanda18067"
MQTT_DISCOVERY_PREFIX = "homeassistant"
MQTT_BASE_TOPIC = "hmpd"
MQTT_CLIENT_ID = "hmpd_bridge"
MQTT_KEEPALIVE = 60
MQTT_RETRY_SECONDS = 10

CONTROLLERS = [
    {"name": "usb0", "dev": "/dev/ttyUSB0", "baud": 4800},
    {"name": "usb1", "dev": "/dev/ttyUSB1", "baud": 4800},
]

# valid current temp polling
POLL_INTERVAL = 10

# slower regs/name/target refresh
REG_REFRESH_INTERVAL = 600

TEMPS_TIMEOUT = 15
REGS_TIMEOUT = 20
SET_TIMEOUT = 8

TEMP_MIN = 16.0
TEMP_MAX = 32.0
TEMP_STEP = 0.1

CURRENT_TEMP_MIN = 5.0
CURRENT_TEMP_MAX = 50.0

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
        self.temps_timeout = TEMPS_TIMEOUT
        self.regs_timeout = REGS_TIMEOUT
        self.set_timeout = SET_TIMEOUT
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
        self.controllers_by_name: Dict[str, Controller] = {
            controller.name: controller for controller in self.controllers
        }

        self.zones: Dict[str, Zone] = {}
        self.latest_temps: Dict[str, Dict[int, float]] = {}
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

    def zone_unique_id(self, controller_name: str, zone_index: int) -> str:
        return f"{self.slugify(controller_name)}_{int(zone_index)}"

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
        if str(reason_code) == "Success":
            self.mqtt_connected = True
            log.info("Connected to MQTT broker %s:%s", self.mqtt_host, self.mqtt_port)
            client.subscribe(f"{self.base_topic}/+/set_target")
            client.subscribe(f"{self.base_topic}/bridge/resync")
        else:
            self.mqtt_connected = False
            log.error("MQTT authorization/connection failed: %s", reason_code)

    def on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        self.mqtt_connected = False
        log.warning("MQTT disconnected (reason=%s)", reason_code)

    def on_message(self, client, userdata, msg):
        topic = msg.topic
        payload = msg.payload.decode("utf-8", errors="ignore").strip()

        if DEBUG:
            log.debug("MQTT message: %s => %s", topic, payload)

        try:
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

            value = round(max(self.temp_min, min(self.temp_max, value)), 1)
            self.set_zone_target(zone, value)
        except Exception as exc:
            log.error("MQTT message handling failed for topic %s: %s", topic, exc)

    def build_hmpd_cmd(self, controller: Controller, action_args: List[str]) -> List[str]:
        hmpd = self.ensure_hmpd()
        base = [hmpd, "--dev", controller.dev, "--baud", str(controller.baud), *action_args]
        if self.stdbuf_path:
            return [self.stdbuf_path, "-oL", *base]
        return base

    def run_hmpd(self, controller: Controller, action_args: List[str], timeout: int) -> Tuple[List[str], bool]:
        cmd = self.build_hmpd_cmd(controller, action_args)
        if DEBUG:
            log.debug("Running: %s", shlex.join(cmd))

        with self.command_lock:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
                timed_out = False
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
                timed_out = True

        stdout = (stdout or "").replace("\r", "")
        stderr = (stderr or "").replace("\r", "")

        if DEBUG and stdout.strip():
            if timed_out:
                log.debug("hmpd partial stdout after timeout:\n%s", stdout.strip())
            else:
                log.debug("hmpd stdout:\n%s", stdout.strip())

        if DEBUG and stderr.strip():
            if timed_out:
                log.debug("hmpd partial stderr after timeout:\n%s", stderr.strip())
            else:
                log.debug("hmpd stderr:\n%s", stderr.strip())

        lines = [line.strip() for line in stdout.splitlines() if line.strip()]

        if timed_out:
            if action_args and action_args[0] == "regs" and lines:
                log.warning(
                    "hmpd regs timed out for %s, using partial output (%s lines)",
                    controller.name,
                    len(lines),
                )
                return lines, True

            raise RuntimeError(f"Command {cmd!r} timed out after {timeout} seconds")

        if proc.returncode != 0:
            raise RuntimeError(f"hmpd exited {proc.returncode}: {stderr.strip() or stdout.strip()}")

        return lines, False

    def valid_current_temp(self, value: float) -> bool:
        return CURRENT_TEMP_MIN < value < CURRENT_TEMP_MAX

    def parse_regs(self, controller: Controller, lines: List[str]) -> Dict[int, dict]:
        parsed: Dict[int, dict] = {}

        for line in lines:
            original = line
            try:
                parts = [p.strip() for p in line.split("|")]
                if len(parts) < 5:
                    continue

                idx = int(parts[0].strip())
                name = parts[1].strip()
                if not name:
                    continue

                m_cur = re.search(r"cur:\s*(-?\d+(?:\.\d+)?)", parts[2])
                m_tgt = re.search(r"tgt:\s*(-?\d+(?:\.\d+)?)", parts[3])

                current_temp = None
                if m_cur:
                    raw_cur = float(m_cur.group(1))
                    if self.valid_current_temp(raw_cur):
                        current_temp = round(raw_cur, 1)

                target_temp = None
                if m_tgt:
                    raw_tgt = float(m_tgt.group(1))
                    target_temp = round(max(self.temp_min, min(self.temp_max, raw_tgt)), 1)

                enabled = "EN" in parts[4]

                parsed[idx] = {
                    "name": name,
                    "current_temp": current_temp,
                    "target_temp": target_temp,
                    "enabled": enabled,
                }

                if DEBUG:
                    log.debug(
                        "REG parsed [%s] => idx=%s name=%s cur=%s tgt=%s enabled=%s raw=%s",
                        controller.name,
                        idx,
                        name,
                        current_temp,
                        target_temp,
                        enabled,
                        original,
                    )
            except Exception as exc:
                log.error("REG parse error [%s]: %s | raw=%s", controller.name, exc, original)

        return parsed

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
                        controller.name,
                        idx,
                        parsed[idx],
                        original,
                    )
            except Exception as exc:
                log.error("TEMP parse error [%s]: %s | raw=%s", controller.name, exc, original)

        return parsed

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

    def publish_state(self, zone: Zone) -> None:
        topic = f"{self.base_topic}/{zone.unique_id}/state"
        payload = {
            "current_temp": zone.current_temp if zone.current_temp is not None else self.temp_min,
            "target_temp": zone.target_temp if zone.target_temp is not None else self.temp_min,
            "mode": "heat",
        }
        self.mqtt.publish(topic, json.dumps(payload), qos=1, retain=self.retain_state)

    def remove_zone(self, unique_id: str) -> None:
        discovery_topic = f"{self.discovery_prefix}/climate/hmpd_{unique_id}/config"
        state_topic = f"{self.base_topic}/{unique_id}/state"
        self.mqtt.publish(discovery_topic, "", qos=1, retain=True)
        self.mqtt.publish(state_topic, "", qos=1, retain=True)
        if unique_id in self.zones:
            del self.zones[unique_id]
        log.info("Removed thermostat %s", unique_id)

    def sync_temps(self, controller: Controller) -> None:
        lines, _ = self.run_hmpd(controller, ["temps"], timeout=self.temps_timeout)
        temps = self.parse_temps(controller, lines)
        self.latest_temps[controller.name] = temps

        current_valid_ids = {
            self.zone_unique_id(controller.name, idx)
            for idx in temps.keys()
        }

        to_remove = [
            unique_id
            for unique_id, zone in self.zones.items()
            if zone.controller_name == controller.name and unique_id not in current_valid_ids
        ]
        for unique_id in to_remove:
            self.remove_zone(unique_id)

        updated = 0
        for idx, current_temp in temps.items():
            unique_id = self.zone_unique_id(controller.name, idx)
            zone = self.zones.get(unique_id)
            if zone:
                zone.current_temp = current_temp
                self.publish_state(zone)
                updated += 1

        total_active = len(
            [zone for zone in self.zones.values() if zone.controller_name == controller.name]
        )

        log.info(
            "Cached %s temperatures for controller %s (%s active zones updated, %s total active)",
            len(temps),
            controller.name,
            updated,
            total_active,
        )

    def sync_regs(self, controller: Controller) -> None:
        lines, is_partial = self.run_hmpd(controller, ["regs"], timeout=self.regs_timeout)
        parsed = self.parse_regs(controller, lines)
        temps = self.latest_temps.get(controller.name, {})

        created = 0
        updated = 0

        for idx, data in parsed.items():
            name = data["name"].strip() if data["name"] else ""
            if not name:
                continue

            if idx not in temps:
                continue

            current_temp = temps[idx]
            unique_id = self.zone_unique_id(controller.name, idx)
            zone = self.zones.get(unique_id)

            if zone is None:
                zone = Zone(
                    controller_name=controller.name,
                    controller_dev=controller.dev,
                    zone_index=idx,
                    zone_name=name,
                    unique_id=unique_id,
                    current_temp=current_temp,
                    target_temp=data["target_temp"],
                    enabled=data["enabled"],
                    discovered=False,
                )
                self.zones[unique_id] = zone
                created += 1
            else:
                zone.zone_name = name
                zone.current_temp = current_temp
                if data["target_temp"] is not None:
                    zone.target_temp = data["target_temp"]
                zone.enabled = data["enabled"]
                updated += 1

            if not zone.discovered:
                self.publish_discovery(zone)

            self.publish_state(zone)

        log.info(
            "Synced regs for %s (%s created, %s updated)%s",
            controller.name,
            created,
            updated,
            " (partial)" if is_partial else "",
        )

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
        controller = self.controllers_by_name.get(zone.controller_name)
        if controller is None:
            log.error("Unknown controller for zone %s", zone.unique_id)
            return

        try:
            self.run_hmpd(
                controller,
                ["set", str(zone.zone_index), f"{target:.1f}"],
                timeout=self.set_timeout,
            )
            zone.target_temp = target
            self.publish_state(zone)
            log.info("Set %s (%s) to %.1f", zone.zone_name, zone.unique_id, target)
        except Exception as exc:
            log.error(
                "Failed to set %s (%s) to %.1f: %s",
                zone.zone_name,
                zone.unique_id,
                target,
                exc,
            )

    def start(self):
        self.find_hmpd()
        log.info("=== HMPD Thermostat Bridge starting ===")
        for controller in self.controllers:
            log.info("Configured controller %s -> %s @ %s", controller.name, controller.dev, controller.baud)

        self.mqtt_connect_loop()

        # First temps so we know what has valid current temperature.
        self.sync_all_temps()

        # Then regs, which creates/updates only named zones that also have valid temp.
        self.sync_all_regs()

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
