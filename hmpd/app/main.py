import json
import logging
import os
import queue
import re
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from typing import Dict, List, Optional, Tuple

import paho.mqtt.client as mqtt

OPTIONS_FILE = "/data/options.json"


def load_options() -> dict:
    with open(OPTIONS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


OPTIONS = load_options()
LOG_LEVEL = getattr(logging, str(OPTIONS.get("log_level", "INFO")).upper(), logging.INFO)
DEBUG_RAW = bool(OPTIONS.get("debug_raw_output", False))
DEBUG_DUMP_PARSED = bool(OPTIONS.get("debug_dump_parsed_data", False))
DEBUG_SAVE_LOG_FILE = bool(OPTIONS.get("debug_save_log_file", True))
DEBUG_LOG_FILE = str(OPTIONS.get("debug_log_file", "/config/hmpd_bridge.log"))


def build_logger() -> logging.Logger:
    logger = logging.getLogger("hmpd-addon")
    logger.setLevel(LOG_LEVEL)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    if DEBUG_SAVE_LOG_FILE:
        try:
            os.makedirs(os.path.dirname(DEBUG_LOG_FILE), exist_ok=True)
            file_handler = RotatingFileHandler(DEBUG_LOG_FILE, maxBytes=1_000_000, backupCount=3)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except Exception as exc:
            logger.error("Failed to initialize log file %s: %s", DEBUG_LOG_FILE, exc)

    return logger


log = build_logger()


@dataclass
class Controller:
    key: str
    dev: str
    baud: int


@dataclass
class ZoneState:
    controller_key: str
    zone_index: int
    name: str
    current_temp: Optional[float] = None
    target_temp: Optional[float] = None
    enabled: Optional[bool] = True
    discovered: bool = False
    pending_target: Optional[float] = None
    last_seen: float = 0.0

    @property
    def zone_key(self) -> str:
        return f"{self.controller_key}_{self.zone_index}"

    @property
    def unique_id(self) -> str:
        return f"hmpd_{self.controller_key}_{self.zone_index}"

    @property
    def device_identifiers(self) -> List[str]:
        # One Home Assistant device per thermostat zone.
        return [f"hmpd_zone_{self.controller_key}_{self.zone_index}"]


class HMPDBridge:
    def __init__(self, options: dict) -> None:
        self.options = options
        self.hmpd_path = str(options.get("hmpd_path", "/homeassistant/hmpd"))
        self.poll_interval = int(options.get("poll_interval", 3))
        self.reg_refresh_interval = int(options.get("reg_refresh_interval", 60))
        self.command_timeout = int(options.get("command_timeout", 10))
        self.temp_min = float(options.get("temp_min", 10.0))
        self.temp_max = float(options.get("temp_max", 35.5))
        self.temp_step = float(options.get("temp_step", 0.1))
        self.retain_discovery = bool(options.get("retain_discovery", True))
        self.retain_state = bool(options.get("retain_state", True))

        self.controllers: List[Controller] = []
        for item in options.get("controllers", []):
            if not isinstance(item, dict):
                continue
            key = str(item.get("name", "")).strip()
            dev = str(item.get("dev", "")).strip()
            baud = int(item.get("baud", 4800))
            if key and dev:
                self.controllers.append(Controller(key=key, dev=dev, baud=baud))

        self.zones: Dict[Tuple[str, int], ZoneState] = {}
        self.command_lock = threading.Lock()
        self.command_queue: "queue.Queue[Tuple[str, int, float]]" = queue.Queue()

        self.discovery_prefix = str(options.get("mqtt_discovery_prefix", "homeassistant")).strip("/")
        self.base_topic = str(options.get("mqtt_base_topic", "hmpd")).strip("/")
        self.client_id = str(options.get("mqtt_client_id", "hmpd_bridge"))

        self.mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.client_id)
        user = str(options.get("mqtt_username", ""))
        password = str(options.get("mqtt_password", ""))
        if user:
            self.mqtt.username_pw_set(user, password)
        self.mqtt.on_connect = self.on_connect
        self.mqtt.on_disconnect = self.on_disconnect
        self.mqtt.on_message = self.on_message
        self.mqtt.reconnect_delay_set(
            min_delay=int(options.get("mqtt_reconnect_min_delay", 1)),
            max_delay=int(options.get("mqtt_reconnect_max_delay", 30)),
        )
        self.mqtt.will_set(f"{self.base_topic}/bridge/status", payload="offline", qos=1, retain=True)
        self.mqtt_host = str(options.get("mqtt_host", "core-mosquitto"))
        self.mqtt_port = int(options.get("mqtt_port", 1883))
        self.mqtt_keepalive = int(options.get("mqtt_keepalive", 60))

    def validate_startup(self) -> None:
        if not self.controllers:
            raise RuntimeError("No controllers configured")
        if not os.path.exists(self.hmpd_path):
            raise FileNotFoundError(
                f"hmpd binary not found at {self.hmpd_path}. Put the binary next to configuration.yaml on the HA host, so the addon can read it as /homeassistant/hmpd."
            )
        if not os.access(self.hmpd_path, os.X_OK):
            raise PermissionError(
                f"hmpd binary is not executable: {self.hmpd_path}. Run chmod +x on the HA host."
            )
        log.info("Using hmpd binary: %s", self.hmpd_path)
        for controller in self.controllers:
            log.info("Configured controller %s -> %s @ %s", controller.key, controller.dev, controller.baud)

    def run_hmpd(self, controller: Controller, args: List[str]) -> List[str]:
        cmd = [self.hmpd_path, "--dev", controller.dev, "--baud", str(controller.baud)] + args
        if DEBUG_RAW:
            log.debug("RUN %s", shlex.join(cmd))
        with self.command_lock:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.command_timeout,
                check=False,
            )
        if DEBUG_RAW and proc.stdout:
            for line in proc.stdout.splitlines():
                log.debug("STDOUT %s", line)
        if DEBUG_RAW and proc.stderr:
            for line in proc.stderr.splitlines():
                log.debug("STDERR %s", line)
        if proc.returncode != 0:
            raise RuntimeError(f"hmpd returned {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}")
        return proc.stdout.splitlines()

    def clamp_temp(self, value: float) -> float:
        return max(self.temp_min, min(self.temp_max, round(float(value), 1)))

    def topic(self, *parts: str) -> str:
        return "/".join([self.base_topic, *parts])

    def get_zone(self, controller_key: str, zone_index: int, zone_name: Optional[str] = None) -> ZoneState:
        key = (controller_key, zone_index)
        if key not in self.zones:
            self.zones[key] = ZoneState(
                controller_key=controller_key,
                zone_index=zone_index,
                name=zone_name or f"Zone {zone_index}",
                last_seen=time.time(),
            )
        zone = self.zones[key]
        if zone_name:
            zone.name = zone_name
        zone.last_seen = time.time()
        return zone

    def publish_discovery(self, zone: ZoneState) -> None:
        availability_topic = self.topic("bridge", "status")
        state_topic = self.topic(zone.zone_key, "state")
        command_topic = self.topic(zone.zone_key, "set_target")
        config_topic = f"{self.discovery_prefix}/climate/{zone.unique_id}/config"
        payload = {
            "name": zone.name,
            "unique_id": zone.unique_id,
            "object_id": zone.unique_id,
            "availability_topic": availability_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "current_temperature_topic": state_topic,
            "current_temperature_template": "{{ value_json.current_temperature }}",
            "temperature_state_topic": state_topic,
            "temperature_state_template": "{{ value_json.target_temperature }}",
            "temperature_command_topic": command_topic,
            "mode_state_topic": state_topic,
            "mode_state_template": "{{ value_json.hvac_mode }}",
            "modes": ["heat"],
            "min_temp": self.temp_min,
            "max_temp": self.temp_max,
            "temp_step": self.temp_step,
            "precision": 0.1,
            "temp_unit": "C",
            "device": {
                "identifiers": zone.device_identifiers,
                "name": zone.name,
                "manufacturer": "HMPD",
                "model": "Thermostat Zone",
                "via_device": f"hmpd_controller_{zone.controller_key}",
                "suggested_area": zone.controller_key,
            },
            "entity_category": None,
        }
        self.mqtt.publish(config_topic, json.dumps(payload), qos=1, retain=self.retain_discovery)
        zone.discovered = True
        log.info("Discovery published for %s (%s)", zone.name, zone.zone_key)

    def publish_state(self, zone: ZoneState) -> None:
        payload = {
            "name": zone.name,
            "zone_index": zone.zone_index,
            "controller": zone.controller_key,
            "current_temperature": zone.current_temp,
            "target_temperature": zone.target_temp,
            "hvac_mode": "heat",
            "enabled": True,
            "pending_target": zone.pending_target,
            "last_seen": int(zone.last_seen),
        }
        self.mqtt.publish(self.topic(zone.zone_key, "state"), json.dumps(payload), qos=1, retain=self.retain_state)

    def parse_regs_lines(self, controller: Controller, lines: List[str]) -> int:
        count = 0
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            parts = [part.strip() for part in line.split("|")]
            if len(parts) < 5:
                log.warning("[%s] Unrecognized regs line: %s", controller.key, line)
                continue
            try:
                zone_index = int(parts[0])
                zone_name = parts[1]
                target_match = re.search(r"(-?\d+(?:\.\d+)?)", parts[3])
                if not target_match:
                    raise ValueError("missing target in regs line")
                target_temp = float(target_match.group(1))
                enabled = "EN" in parts[4].upper()
                zone = self.get_zone(controller.key, zone_index, zone_name)
                zone.target_temp = self.clamp_temp(target_temp)
                zone.enabled = enabled
                zone.pending_target = None if zone.pending_target is not None and abs(zone.pending_target - zone.target_temp) < 0.05 else zone.pending_target
                if not zone.discovered:
                    self.publish_discovery(zone)
                self.publish_state(zone)
                count += 1
                if DEBUG_DUMP_PARSED:
                    log.debug("PARSED REGS %s => idx=%s name=%s target=%s enabled=%s", controller.key, zone_index, zone_name, zone.target_temp, enabled)
            except Exception as exc:
                log.error("[%s] Failed to parse regs line '%s': %s", controller.key, line, exc)
        return count

    def parse_temps_lines(self, controller: Controller, lines: List[str]) -> int:
        count = 0
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            match = re.match(r"^(\d+)\s*:\s*(-?\d+(?:\.\d+)?)$", line)
            if not match:
                log.warning("[%s] Unrecognized temps line: %s", controller.key, line)
                continue
            try:
                zone_index = int(match.group(1))
                current_temp = float(match.group(2))
                zone = self.get_zone(controller.key, zone_index)
                zone.current_temp = current_temp
                self.publish_state(zone)
                count += 1
                if DEBUG_DUMP_PARSED:
                    log.debug("PARSED TEMPS %s => idx=%s current=%s", controller.key, zone_index, current_temp)
            except Exception as exc:
                log.error("[%s] Failed to parse temps line '%s': %s", controller.key, line, exc)
        return count

    def sync_regs(self, controller: Controller) -> None:
        lines = self.run_hmpd(controller, ["regs"])
        count = self.parse_regs_lines(controller, lines)
        log.info("[%s] regs sync complete: %s zones", controller.key, count)

    def sync_temps(self, controller: Controller) -> None:
        lines = self.run_hmpd(controller, ["temps"])
        count = self.parse_temps_lines(controller, lines)
        log.info("[%s] temps sync complete: %s zones", controller.key, count)

    def set_target(self, controller_key: str, zone_index: int, requested: float) -> None:
        controller = next((c for c in self.controllers if c.key == controller_key), None)
        if controller is None:
            raise KeyError(f"Unknown controller {controller_key}")
        zone = self.get_zone(controller_key, zone_index)
        target = self.clamp_temp(requested)
        zone.pending_target = target
        self.publish_state(zone)
        self.run_hmpd(controller, ["set", str(zone_index), f"{target:.1f}"])
        zone.target_temp = target
        zone.pending_target = None
        self.publish_state(zone)
        log.info("[%s] set zone %s -> %.1f", controller_key, zone_index, target)

    def on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        log.info("Connected to MQTT broker %s:%s (reason=%s)", self.mqtt_host, self.mqtt_port, reason_code)
        self.mqtt.publish(self.topic("bridge", "status"), "online", qos=1, retain=True)
        self.mqtt.subscribe(self.topic("+", "set_target"), qos=1)
        self.mqtt.subscribe(self.topic("bridge", "resync"), qos=1)

    def on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties) -> None:
        log.warning("MQTT disconnected (reason=%s)", reason_code)

    def on_message(self, client, userdata, msg) -> None:
        topic = msg.topic
        payload = msg.payload.decode("utf-8", errors="ignore").strip()
        if DEBUG_RAW:
            log.debug("MQTT RX %s => %s", topic, payload)
        if topic == self.topic("bridge", "resync"):
            for controller in self.controllers:
                try:
                    self.sync_regs(controller)
                    self.sync_temps(controller)
                except Exception as exc:
                    log.error("Resync failed for %s: %s", controller.key, exc)
            return
        match = re.match(rf"^{re.escape(self.base_topic)}/([^/]+_[0-9]+)/set_target$", topic)
        if not match:
            return
        zone_key = match.group(1)
        controller_key, index_text = zone_key.rsplit("_", 1)
        try:
            target = float(payload)
            self.command_queue.put((controller_key, int(index_text), target))
            log.info("Queued set command for %s -> %s", zone_key, target)
        except Exception as exc:
            log.error("Invalid MQTT command on %s: %s", topic, exc)

    def start(self) -> None:
        self.validate_startup()
        self.mqtt.connect(self.mqtt_host, self.mqtt_port, keepalive=self.mqtt_keepalive)
        self.mqtt.loop_start()

        for controller in self.controllers:
            self.sync_regs(controller)
            self.sync_temps(controller)

        last_regs = time.monotonic()
        while True:
            while not self.command_queue.empty():
                controller_key, zone_index, target = self.command_queue.get()
                try:
                    self.set_target(controller_key, zone_index, target)
                except Exception as exc:
                    log.error("Failed to apply queued set for %s_%s: %s", controller_key, zone_index, exc)
            for controller in self.controllers:
                try:
                    self.sync_temps(controller)
                except Exception as exc:
                    log.error("temps sync failed for %s: %s", controller.key, exc)
            if time.monotonic() - last_regs >= self.reg_refresh_interval:
                for controller in self.controllers:
                    try:
                        self.sync_regs(controller)
                    except Exception as exc:
                        log.error("regs sync failed for %s: %s", controller.key, exc)
                last_regs = time.monotonic()
            time.sleep(self.poll_interval)


if __name__ == "__main__":
    log.info("=== HMPD Thermostat Bridge starting ===")
    bridge = HMPDBridge(OPTIONS)
    bridge.start()
