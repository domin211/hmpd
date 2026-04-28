import json
import logging
import logging.handlers
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import paho.mqtt.client as mqtt


OPTIONS_PATH = "/data/options.json"

MQTT_HOST = os.getenv("MQTT_HOST", "core-mosquitto")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USERNAME = os.getenv("MQTT_USERNAME", "")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")
MQTT_DISCOVERY_PREFIX = os.getenv("MQTT_DISCOVERY_PREFIX", "homeassistant")
MQTT_BASE_TOPIC = os.getenv("MQTT_BASE_TOPIC", "hmpd")
MQTT_CLIENT_ID = os.getenv("MQTT_CLIENT_ID", "hmpd_bridge")
MQTT_KEEPALIVE = 60
MQTT_RETRY_SECONDS = 10

CONTROLLERS = [
    {"name": "usb0", "dev": "/dev/ttyUSB0", "baud": 4800, "expected_regs": 64},
    {"name": "usb1", "dev": "/dev/ttyUSB1", "baud": 4800, "expected_regs": 39},
]

CURRENT_TEMP_SYNC_INTERVAL = 60
TARGET_SYNC_INTERVAL = 3600
AUTO_OFFSET_COOLDOWN_SECONDS = 60
EXTERNAL_SENSOR_TARGET_OFFSET = 2.0

TEMPS_TIMEOUT = 15
REGS_TIMEOUT = 20
SET_TIMEOUT = 20

COMMAND_GAP_SECONDS = 3.0
MAX_COMMAND_ATTEMPTS = 5
RETRY_DELAYS_SECONDS = [5, 10, 60, 60]

TEMP_MIN = 16.0
TEMP_MAX = 32.0
TEMP_STEP = 1.0
OFF_MODE_TARGET_THRESHOLD = 18.0

BOOKING_SYNC_INTERVAL = 60
BOOKING_ON_TEMP_DEFAULT = 23.0
BOOKING_OFF_TEMP_DEFAULT = 16.0
BOOKING_STATE_PATH = "/data/booking_state.json"

CURRENT_TEMP_MIN = 5.0
CURRENT_TEMP_MAX = 50.0

HMPD_PATH = os.getenv("HMPD_PATH", "/homeassistant/hmpd")
HMPD_FIND_RETRY_SECONDS = 30

HOME_ASSISTANT_API_URL = "http://supervisor/core/api"
HOME_ASSISTANT_SENSOR_TIMEOUT = 10

RETAIN_DISCOVERY = True
RETAIN_STATE = True

DEBUG_LOG_FILE = "/config/hmpd_bridge.log"
DEBUG_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10MB per file
DEBUG_LOG_BACKUP_COUNT = 5  # Keep 5 rotated backups = 60MB total max
MAX_ZONE_INDEX = 64


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

# Add rotating file handler for debug logging
if DEBUG:
    try:
        os.makedirs(os.path.dirname(DEBUG_LOG_FILE), exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            DEBUG_LOG_FILE,
            maxBytes=DEBUG_LOG_MAX_BYTES,
            backupCount=DEBUG_LOG_BACKUP_COUNT,
        )
        fh.setLevel(LOG_LEVEL)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.getLogger().addHandler(fh)
        log.info("Debug logging to %s with rotation (max %dMB, %d backups)", 
                 DEBUG_LOG_FILE, DEBUG_LOG_MAX_BYTES // (1024*1024), DEBUG_LOG_BACKUP_COUNT)
    except Exception as exc:
        log.warning("Could not setup debug file handler: %s", exc)


@dataclass(frozen=True)
class Controller:
    name: str
    dev: str
    baud: int
    expected_regs: int = 64

    @property
    def key(self) -> str:
        base = os.path.basename(self.dev) or self.name
        value = f"{self.name}_{base}"
        value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
        value = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()
        return value or self.name.lower()


@dataclass
class Zone:
    controller_name: str
    controller_key: str
    controller_dev: str
    zone_index: int
    zone_name: str
    unique_id: str
    current_temp: Optional[float] = None
    built_in_current_temp: Optional[float] = None
    controller_target_temp: Optional[float] = None
    target_temp: Optional[float] = None
    enabled: Optional[bool] = None
    external_sensor_entity_id: Optional[str] = None
    booking_entity_ids: List[str] = field(default_factory=list)
    booking_name: Optional[str] = None
    last_booking_off_temp_payload: Optional[str] = None
    last_offset_adjust_at: float = 0.0


@dataclass
class ControllerJob:
    kind: str
    action_args: List[str] = field(default_factory=list)
    timeout: int = 0
    zone_unique_id: Optional[str] = None
    target: Optional[float] = None
    reason: str = ""
    done: threading.Event = field(default_factory=threading.Event)
    result_lines: List[str] = field(default_factory=list)
    result_partial: bool = False
    error: Optional[Exception] = None


class HMPDBridge:
    def __init__(self):
        # Hardcoded MQTT discovery settings
        self.discovery_prefix = MQTT_DISCOVERY_PREFIX
        self.base_topic = MQTT_BASE_TOPIC

        # Configurable MQTT settings
        self.mqtt_host = OPTIONS.get("mqtt_host", MQTT_HOST)
        self.mqtt_port = int(OPTIONS.get("mqtt_port", MQTT_PORT))
        self.mqtt_username = OPTIONS.get("mqtt_username", MQTT_USERNAME)
        self.mqtt_password = OPTIONS.get("mqtt_password", MQTT_PASSWORD)

        self.mqtt_client_id = MQTT_CLIENT_ID
        self.mqtt_keepalive = MQTT_KEEPALIVE
        self.mqtt_retry_seconds = MQTT_RETRY_SECONDS
        
        # Shutdown signal
        self.shutdown_event = threading.Event()

        # Hardcoded sync intervals
        self.current_temp_sync_interval = CURRENT_TEMP_SYNC_INTERVAL
        self.target_sync_interval = TARGET_SYNC_INTERVAL
        self.auto_offset_cooldown_seconds = AUTO_OFFSET_COOLDOWN_SECONDS
        self.external_sensor_target_offset = EXTERNAL_SENSOR_TARGET_OFFSET

        # Hardcoded timeouts
        self.temps_timeout = TEMPS_TIMEOUT
        self.regs_timeout = REGS_TIMEOUT
        self.set_timeout = SET_TIMEOUT

        # Hardcoded command settings
        self.command_gap_seconds = COMMAND_GAP_SECONDS
        self.max_command_attempts = MAX_COMMAND_ATTEMPTS
        self.retry_delays_seconds = RETRY_DELAYS_SECONDS

        # Hardcoded temperature settings
        self.temp_min = TEMP_MIN
        self.temp_max = TEMP_MAX
        self.temp_step = TEMP_STEP
        self.booking_sync_interval = BOOKING_SYNC_INTERVAL
        self.booking_on_temp_default = self.snap_target(BOOKING_ON_TEMP_DEFAULT)
        self.booking_off_temp_default = self.snap_target(BOOKING_OFF_TEMP_DEFAULT)
        self.retain_discovery = RETAIN_DISCOVERY
        self.retain_state = RETAIN_STATE
        self.configured_hmpd_path = OPTIONS.get("hmpd_path", HMPD_PATH)

        self.ha_api_url = OPTIONS.get("home_assistant_api_url", HOME_ASSISTANT_API_URL).rstrip("/")
        self.ha_sensor_timeout = int(OPTIONS.get("home_assistant_sensor_timeout", HOME_ASSISTANT_SENSOR_TIMEOUT))
        self.supervisor_token = os.getenv("SUPERVISOR_TOKEN", "")
        self.external_temp_sensor_map = self.build_external_temp_sensor_map(
            OPTIONS.get("external_temp_sensors", [])
        )
        self.booking_entity_map = self.build_booking_entity_map(
            OPTIONS.get("booking_status_entities", [])
        )
        self.saved_booking_zone_state = self.load_saved_booking_zone_state()
        # Track which external sensors have warning issues - cleared periodically to avoid memory leak
        self.external_sensor_warnings_shown: Set[Tuple[str, int]] = set()
        self.last_external_sensor_warning_clear = time.monotonic()

        configured_controllers = OPTIONS.get("controllers", CONTROLLERS)
        self.controllers: List[Controller] = [
            Controller(
                name=item["name"],
                dev=item["dev"],
                baud=int(item["baud"]),
                expected_regs=int(item.get("expected_regs", 64)),
            )
            for item in configured_controllers
        ]

        self.controllers_by_name: Dict[str, Controller] = {
            controller.name: controller for controller in self.controllers
        }
        self.controllers_by_key: Dict[str, Controller] = {
            controller.key: controller for controller in self.controllers
        }

        self.zones: Dict[str, Zone] = {}
        self.latest_temps: Dict[str, Dict[int, float]] = {}
        self.latest_regs: Dict[str, Dict[int, dict]] = {}

        self.mqtt_connected = False
        self.hmpd_path = ""
        self.stdbuf_path = shutil.which("stdbuf")
        self.suppress_empty_command_logs = False

        self.queue_conditions: Dict[str, threading.Condition] = {
            controller.key: threading.Condition() for controller in self.controllers
        }
        self.queue_items: Dict[str, List[ControllerJob]] = {
            controller.key: [] for controller in self.controllers
        }
        self.queue_threads: Dict[str, threading.Thread] = {}
        self.last_command_finished_at: Dict[str, float] = {
            controller.key: 0.0 for controller in self.controllers
        }
        self.current_job_kind: Dict[str, Optional[str]] = {
            controller.key: None for controller in self.controllers
        }

        self.next_temp_sync_at: Dict[str, float] = {
            controller.key: 0.0 for controller in self.controllers
        }
        self.next_target_sync_at: Dict[str, float] = {
            controller.key: 0.0 for controller in self.controllers
        }
        self.next_booking_sync_at = 0.0

        self.mqtt = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=self.mqtt_client_id,
        )

        if self.mqtt_username:
            self.mqtt.username_pw_set(self.mqtt_username, self.mqtt_password)

        self.mqtt.on_connect = self.on_connect
        self.mqtt.on_disconnect = self.on_disconnect
        self.mqtt.on_message = self.on_message
        self.mqtt.reconnect_delay_set(min_delay=1, max_delay=30)

    def normalize_ascii(self, value: str) -> str:
        return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")

    def slugify(self, value: str) -> str:
        value = self.normalize_ascii(value).strip().lower()
        value = re.sub(r"[^a-z0-9]+", "_", value)
        value = re.sub(r"_+", "_", value).strip("_")
        return value or "zone"

    def snap_target(self, value: float) -> float:
        clamped = max(self.temp_min, min(self.temp_max, value))
        steps = round((clamped - self.temp_min) / self.temp_step)
        snapped = self.temp_min + steps * self.temp_step
        return round(max(self.temp_min, min(self.temp_max, snapped)), 1)

    def build_external_temp_sensor_map(self, configured: List[dict]) -> Dict[Tuple[str, int], str]:
        mapping: Dict[Tuple[str, int], str] = {}
        for item in configured or []:
            if not isinstance(item, dict):
                continue

            controller_name = str(item.get("controller", "")).strip()
            entity_id = str(item.get("entity_id", "")).strip()
            zone_raw = item.get("zone")

            if not controller_name or not entity_id or zone_raw in (None, ""):
                log.warning(
                    "Ignoring external_temp_sensors entry with missing controller/zone/entity_id: %s",
                    item,
                )
                continue

            try:
                zone_index = int(zone_raw)
            except Exception:
                log.warning("Ignoring external_temp_sensors entry with invalid zone %r: %s", zone_raw, item)
                continue

            mapping[(controller_name, zone_index)] = entity_id

        return mapping

    def get_external_sensor_entity_id(self, controller_name: str, zone_index: int) -> Optional[str]:
        entity_id = self.external_temp_sensor_map.get((controller_name, zone_index))
        if entity_id:
            return entity_id
        return None

    def build_booking_entity_map(self, configured: List[dict]) -> Dict[Tuple[str, int], Tuple[Optional[str], List[str]]]:
        mapping: Dict[Tuple[str, int], Tuple[Optional[str], List[str]]] = {}
        for item in configured or []:
            if not isinstance(item, dict):
                continue

            name = str(item.get("name", "")).strip() or None
            controller_name = str(item.get("controller", "")).strip()
            entity_ids_raw = item.get("entity_ids", [])
            zones_raw = item.get("zones", [])

            if not controller_name:
                log.warning(
                    "Ignoring booking_status_entities entry with missing controller: %s",
                    item,
                )
                continue

            # Handle entity_ids as list
            entity_ids: List[str] = []
            if isinstance(entity_ids_raw, list):
                entity_ids = [str(e).strip() for e in entity_ids_raw if str(e).strip()]
            elif isinstance(entity_ids_raw, str):
                # Support single entity_id string for backward compatibility
                entity_id = entity_ids_raw.strip()
                if entity_id:
                    entity_ids = [entity_id]

            if not entity_ids:
                log.warning(
                    "Ignoring booking_status_entities entry with missing entity_ids: %s",
                    item,
                )
                continue

            # Handle zones as array of integers
            if not isinstance(zones_raw, list):
                zones_raw = []

            if not zones_raw:
                log.warning(
                    "Ignoring booking_status_entities entry with missing zones: %s",
                    item,
                )
                continue

            for zone_raw in zones_raw:
                try:
                    zone_index = int(zone_raw)
                except Exception:
                    log.warning("Ignoring invalid zone %r in controller %s", zone_raw, controller_name)
                    continue

                mapping[(controller_name, zone_index)] = (name, entity_ids)
                if DEBUG:
                    log.debug(
                        "Mapped booking %s [%s] -> controller=%s zone=%s",
                        name or "calendar",
                        entity_ids[0],
                        controller_name,
                        zone_index,
                    )

        return mapping

    def get_booking_entity_info(self, controller_name: str, zone_index: int) -> Tuple[Optional[str], List[str]]:
        result = self.booking_entity_map.get((controller_name, zone_index))
        if result:
            return result
        return None, []

    def load_saved_booking_zone_state(self) -> Dict[str, dict]:
        try:
            with open(BOOKING_STATE_PATH, "r", encoding="utf-8") as f:
                payload = json.load(f)
            zones = payload.get("zones", {})
            if isinstance(zones, dict):
                return zones
        except FileNotFoundError:
            return {}
        except Exception as exc:
            log.warning("Could not read booking state file %s: %s", BOOKING_STATE_PATH, exc)
        return {}

    def persist_booking_zone_state(self) -> None:
        payload = {
            "zones": {
                unique_id: {
                    "on_temp": zone.booking_on_temp,
                    "off_temp": zone.booking_off_temp,
                    "booking_state": zone.booking_state,
                }
                for unique_id, zone in self.zones.items()
            }
        }

        try:
            os.makedirs(os.path.dirname(BOOKING_STATE_PATH), exist_ok=True)
            temp_path = f"{BOOKING_STATE_PATH}.tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, sort_keys=True)
            os.replace(temp_path, BOOKING_STATE_PATH)
            self.saved_booking_zone_state = payload["zones"]
        except Exception as exc:
            log.warning("Could not persist booking state file %s: %s", BOOKING_STATE_PATH, exc)

    def get_saved_booking_values(self, unique_id: str) -> Tuple[float, float, bool]:
        on_temp = self.booking_on_temp_default
        off_temp = self.booking_off_temp_default
        booking_state = False

        raw_values = self.saved_booking_zone_state.get(unique_id)
        if isinstance(raw_values, dict):
            raw_on = raw_values.get("on_temp")
            raw_off = raw_values.get("off_temp")
            raw_state = raw_values.get("booking_state")

            try:
                if raw_on is not None:
                    on_temp = self.snap_target(float(raw_on))
            except Exception:
                pass

            try:
                if raw_off is not None:
                    off_temp = self.snap_target(float(raw_off))
            except Exception:
                pass

            if isinstance(raw_state, bool):
                booking_state = raw_state
            elif isinstance(raw_state, str):
                parsed_state = self.parse_booking_state(raw_state)
                if parsed_state is not None:
                    booking_state = parsed_state

        return on_temp, off_temp, booking_state

    def parse_booking_state(self, raw_state: str) -> Optional[bool]:
        normalized = str(raw_state).strip().lower()
        if normalized in {"on", "true", "1", "yes", "booked", "occupied", "active"}:
            return True
        if normalized in {"off", "false", "0", "no", "unbooked", "vacant", "inactive", "idle"}:
            return False
        return None

    def booking_target_for_zone(self, zone: Zone) -> float:
        if zone.booking_state:
            return self.snap_target(zone.booking_on_temp)
        return self.snap_target(zone.booking_off_temp)

    def maybe_apply_booking_target(self, zone: Zone, reason: str) -> None:
        desired_display_target = self.booking_target_for_zone(zone)
        if zone.target_temp is not None and abs(zone.target_temp - desired_display_target) < (self.temp_step / 2):
            return

        controller_target = self.calculate_offset_adjusted_target(zone, desired_display_target)

        if self.is_job_kind_active_or_queued(zone.controller_key, "set"):
            zone.target_temp = desired_display_target
            self.publish_state(zone)
            return

        zone.target_temp = desired_display_target
        self.enqueue_set_zone_target(zone, controller_target, reason=f"booking_{reason}")
        self.publish_state(zone)

    def sync_booking_states_from_home_assistant(self) -> None:
        for zone in self.zones.values():
            if zone.booking_entity_ids:
                # Check all booking entities - if any is "on", booking is active
                booking_state = False
                for entity_id in zone.booking_entity_ids:
                    state = self.fetch_home_assistant_state(entity_id)
                    if state:
                        entity_state = self.parse_booking_state(state.get("state", ""))
                        if entity_state:
                            booking_state = True
                            break
                
                if booking_state != zone.booking_state:
                    zone.booking_state = booking_state
                    self.persist_booking_zone_state()
                self.maybe_apply_booking_target(zone, "calendar_sync")

            self.publish_booking_state(zone)
            self.publish_booking_temperatures(zone)

    def fetch_home_assistant_state(self, entity_id: str) -> Optional[dict]:
        if not self.supervisor_token:
            log.warning(
                "SUPERVISOR_TOKEN is not available, cannot read Home Assistant state for %s", entity_id
            )
            return None

        url = f"{self.ha_api_url}/states/{urllib.parse.quote(entity_id, safe='')}"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.supervisor_token}",
                "Content-Type": "application/json",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=self.ha_sensor_timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                log.warning("Failed to fetch Home Assistant state for %s: HTTP %s", entity_id, exc.code)
            return None
        except Exception as exc:
            log.warning("Failed to fetch Home Assistant state for %s: %s", entity_id, exc)
            return None

    def get_external_current_temp(self, entity_id: str) -> Optional[float]:
        state = self.fetch_home_assistant_state(entity_id)
        if not state:
            return None

        raw_state = state.get("state")
        if raw_state in (None, "", "unknown", "unavailable"):
            return None

        try:
            value = round(float(raw_state), 1)
        except Exception:
            log.warning("External sensor %s returned non-numeric state %r", entity_id, raw_state)
            return None

        if not self.valid_current_temp(value):
            log.warning("External sensor %s returned out-of-range temperature %s", entity_id, value)
            return None

        return value

    def apply_external_sensor_temperature(self, zone: Zone) -> bool:
        entity_id = zone.external_sensor_entity_id or self.get_external_sensor_entity_id(
            zone.controller_name, zone.zone_index
        )
        if not entity_id:
            return False

        zone.external_sensor_entity_id = entity_id
        external_temp = self.get_external_current_temp(entity_id)
        if external_temp is None:
            key = (zone.controller_name, zone.zone_index)
            if key not in self.external_sensor_warnings_shown:
                log.warning(
                    "External sensor %s for %s zone %s is unavailable or invalid, falling back to built-in reading",
                    entity_id,
                    zone.controller_name,
                    zone.zone_index,
                )
                self.external_sensor_warnings_shown.add(key)
            return False

        key = (zone.controller_name, zone.zone_index)
        self.external_sensor_warnings_shown.discard(key)
        zone.current_temp = external_temp
        
        # Periodically clear warnings set to prevent memory leak
        now = time.monotonic()
        if (now - self.last_external_sensor_warning_clear) > 3600:  # Clear every hour
            self.external_sensor_warnings_shown.clear()
            self.last_external_sensor_warning_clear = now
        
        return True

    def uses_external_sensor(self, zone: Zone) -> bool:
        return bool(zone.external_sensor_entity_id or self.get_external_sensor_entity_id(zone.controller_name, zone.zone_index))

    def external_sensor_directional_offset(self, requested_target: float, external_current: Optional[float]) -> float:
        if external_current is None:
            return 0.0
        if external_current < requested_target:
            return self.external_sensor_target_offset
        return -self.external_sensor_target_offset

    def calculate_offset_adjusted_target(self, zone: Zone, requested_target: float) -> float:
        if requested_target <= OFF_MODE_TARGET_THRESHOLD:
            return self.snap_target(requested_target)

        if not self.uses_external_sensor(zone):
            return self.snap_target(requested_target)

        built_in = zone.built_in_current_temp
        external = zone.current_temp
        if built_in is None or external is None:
            return self.snap_target(requested_target)

        offset = built_in - external + self.external_sensor_directional_offset(
            requested_target,
            external,
        )
        return self.snap_target(requested_target + offset)

    def infer_display_target_from_controller_target(self, zone: Zone) -> Optional[float]:
        controller_target = zone.controller_target_temp
        if controller_target is None:
            return None
        if controller_target <= OFF_MODE_TARGET_THRESHOLD:
            return controller_target

        if not self.uses_external_sensor(zone):
            return controller_target

        built_in = zone.built_in_current_temp
        external = zone.current_temp
        if built_in is None or external is None:
            return controller_target

        base_offset = built_in - external
        candidate_plus = self.snap_target(
            controller_target - base_offset - self.external_sensor_target_offset
        )
        candidate_minus = self.snap_target(
            controller_target - base_offset + self.external_sensor_target_offset
        )

        plus_controller_target = self.calculate_offset_adjusted_target(zone, candidate_plus)
        minus_controller_target = self.calculate_offset_adjusted_target(zone, candidate_minus)

        if abs(plus_controller_target - controller_target) <= abs(minus_controller_target - controller_target):
            return candidate_plus
        return candidate_minus

    def maybe_enqueue_offset_adjustment(self, zone: Zone, reason: str) -> None:
        if not self.uses_external_sensor(zone):
            return
        if self.is_zone_off_mode(zone):
            return
        if zone.target_temp is None or zone.controller_target_temp is None:
            return

        desired_controller_target = self.calculate_offset_adjusted_target(zone, zone.target_temp)
        if abs(desired_controller_target - zone.controller_target_temp) < (self.temp_step / 2):
            return

        now = time.monotonic()
        if (now - zone.last_offset_adjust_at) < self.auto_offset_cooldown_seconds:
            return

        if self.is_job_kind_active_or_queued(zone.controller_key, "set"):
            return

        zone.last_offset_adjust_at = now
        log.info(
            "Queueing offset adjustment for %s (%s): requested=%.1f controller_current=%.1f controller_desired=%.1f reason=%s",
            zone.zone_name,
            zone.unique_id,
            zone.target_temp,
            zone.controller_target_temp,
            desired_controller_target,
            reason,
        )
        self.enqueue_set_zone_target(zone, desired_controller_target, reason=f"offset_{reason}")

    def zone_unique_id(self, controller: Controller, zone_index: int) -> str:
        return f"{controller.key}_{int(zone_index)}"

    def state_topic(self, unique_id: str) -> str:
        return f"{self.base_topic}/{unique_id}/state"

    def command_topic(self, unique_id: str) -> str:
        return f"{self.base_topic}/{unique_id}/set_target"

    def booking_state_topic(self, unique_id: str) -> str:
        return f"{self.base_topic}/{unique_id}/booking/state"

    def booking_state_command_topic(self, unique_id: str) -> str:
        return f"{self.base_topic}/{unique_id}/booking/state/set"

    def booking_on_temp_state_topic(self, unique_id: str) -> str:
        return f"{self.base_topic}/{unique_id}/booking/on_temp/state"

    def booking_on_temp_command_topic(self, unique_id: str) -> str:
        return f"{self.base_topic}/{unique_id}/booking/on_temp/set"

    def booking_off_temp_state_topic(self, unique_id: str) -> str:
        return f"{self.base_topic}/{unique_id}/booking/off_temp/state"

    def booking_off_temp_command_topic(self, unique_id: str) -> str:
        return f"{self.base_topic}/{unique_id}/booking/off_temp/set"

    def discovery_topic(self, unique_id: str) -> str:
        return f"{self.discovery_prefix}/climate/hmpd_{unique_id}/config"

    def booking_discovery_topic(self, unique_id: str) -> str:
        return f"{self.discovery_prefix}/switch/hmpd_{unique_id}_booking/config"

    def booking_on_temp_discovery_topic(self, unique_id: str) -> str:
        return f"{self.discovery_prefix}/number/hmpd_{unique_id}_booking_on_temp/config"

    def booking_off_temp_discovery_topic(self, unique_id: str) -> str:
        return f"{self.discovery_prefix}/number/hmpd_{unique_id}_booking_off_temp/config"

    def cleanup_unique_id(self, unique_id: str) -> None:
        topics = [
            self.discovery_topic(unique_id),
            self.booking_discovery_topic(unique_id),
            self.booking_on_temp_discovery_topic(unique_id),
            self.booking_off_temp_discovery_topic(unique_id),
            self.state_topic(unique_id),
            self.command_topic(unique_id),
            self.booking_state_topic(unique_id),
            self.booking_state_command_topic(unique_id),
            self.booking_on_temp_state_topic(unique_id),
            self.booking_on_temp_command_topic(unique_id),
            self.booking_off_temp_state_topic(unique_id),
            self.booking_off_temp_command_topic(unique_id),
        ]
        for topic in topics:
            self.mqtt.publish(topic, "", qos=1, retain=True)

    def zone_alias_candidates(self, controller: Controller, idx: int, name: str) -> Set[str]:
        base_name = name.strip()
        ascii_name = self.normalize_ascii(base_name)
        name_slug = self.slugify(base_name)
        ascii_slug = self.slugify(ascii_name)
        ctrl_slug = self.slugify(controller.name)
        ctrl_key_slug = self.slugify(controller.key)

        raw_variants = {
            base_name,
            ascii_name,
            base_name.lower(),
            ascii_name.lower(),
            re.sub(r"\s+", " ", base_name),
            re.sub(r"\s+", " ", ascii_name),
        }

        candidates: Set[str] = {
            self.zone_unique_id(controller, idx),
            f"{ctrl_slug}_{idx}",
            f"{ctrl_key_slug}_{idx}",
            f"{ctrl_slug}_{idx}_{name_slug}",
            f"{ctrl_slug}_{idx}_{ascii_slug}",
            f"{ctrl_key_slug}_{idx}_{name_slug}",
            f"{ctrl_key_slug}_{idx}_{ascii_slug}",
            f"{ctrl_slug}_{name_slug}",
            f"{ctrl_slug}_{ascii_slug}",
            f"{name_slug}",
            f"{ascii_slug}",
            f"zone_{idx}",
            str(idx),
        }

        for raw in raw_variants:
            raw_slug = self.slugify(raw)
            candidates.add(raw_slug)
            candidates.add(f"{ctrl_slug}_{raw_slug}")
            candidates.add(f"{ctrl_slug}_{idx}_{raw_slug}")
            candidates.add(f"{ctrl_slug}_{raw_slug}_{idx}")
            candidates.add(f"{ctrl_key_slug}_{raw_slug}")
            candidates.add(f"{ctrl_key_slug}_{idx}_{raw_slug}")
            candidates.add(f"{ctrl_key_slug}_{raw_slug}_{idx}")

        return {c for c in candidates if c}

    def cleanup_all_retained_topics(self) -> None:
        candidates: Set[str] = set()
        for controller in self.controllers:
            for idx in range(MAX_ZONE_INDEX):
                candidates.add(self.zone_unique_id(controller, idx))
                candidates.add(f"{self.slugify(controller.name)}_{idx}")
        for unique_id in list(self.zones.keys()):
            candidates.add(unique_id)
        for unique_id in sorted(candidates):
            self.cleanup_unique_id(unique_id)
        log.info("Published retained cleanup for all indexed topics")

    def cleanup_legacy_alias_topics(self) -> None:
        candidates: Set[str] = set()
        for controller in self.controllers:
            try:
                lines, _ = self.run_hmpd_now(controller, ["regs"], timeout=self.regs_timeout)
                parsed = self.parse_regs(controller, lines)
                for idx, data in parsed.items():
                    name = (data.get("name") or "").strip()
                    if name:
                        candidates.update(self.zone_alias_candidates(controller, idx, name))
            except Exception as exc:
                log.warning("Legacy alias cleanup scan failed for %s: %s", controller.name, exc)

        generic = {
            "unnamed_device",
            "thermostat_regulator",
            "hmpd",
            "usb0",
            "usb1",
        }
        candidates.update(generic)

        for unique_id in sorted(candidates):
            self.cleanup_unique_id(unique_id)
        log.info("Published retained cleanup for %s legacy alias topic candidates", len(candidates))

    def scan_and_cleanup_discovery_topics(self, wait_seconds: float = 3.0) -> None:
        found_topics: Set[str] = set()
        found_state_topics: Set[str] = set()
        found_command_topics: Set[str] = set()
        prefix = f"{self.discovery_prefix}/climate/"

        def looks_like_hmpd_config(topic: str, payload_text: str) -> bool:
            if not topic.startswith(prefix):
                return False
            low_topic = topic.lower()
            low_payload = payload_text.lower()
            return any(
                marker in low_topic or marker in low_payload
                for marker in [
                    "/climate/hmpd",
                    '"manufacturer": "hmpd"',
                    '"manufacturer":"hmpd"',
                    '"model": "thermostat regulator"',
                    '"model":"thermostat regulator"',
                    '"via_device": "hmpd_bridge_',
                    '"via_device":"hmpd_bridge_',
                    '"identifiers": ["hmpd_',
                    '"identifiers":["hmpd_',
                    '"unique_id": "hmpd_',
                    '"unique_id":"hmpd_',
                    '"object_id": "hmpd_',
                    '"object_id":"hmpd_',
                ]
            )

        original_on_message = self.mqtt.on_message

        def scan_on_message(client, userdata, msg):
            payload_text = msg.payload.decode("utf-8", errors="ignore")
            matched = False
            if looks_like_hmpd_config(msg.topic, payload_text):
                matched = True
                found_topics.add(msg.topic)
                try:
                    data = json.loads(payload_text) if payload_text.strip() else {}
                except Exception:
                    data = {}
                state_topic = (
                    data.get("current_temperature_topic")
                    or data.get("temperature_state_topic")
                    or data.get("state_topic")
                )
                command_topic = data.get("temperature_command_topic") or data.get("command_topic")
                if isinstance(state_topic, str) and state_topic:
                    found_state_topics.add(state_topic)
                if isinstance(command_topic, str) and command_topic:
                    found_command_topics.add(command_topic)

            if not matched and original_on_message is not None:
                original_on_message(client, userdata, msg)

        self.mqtt.on_message = scan_on_message
        self.suppress_empty_command_logs = True
        try:
            self.mqtt.subscribe(f"{self.discovery_prefix}/climate/#", qos=1)
            end = time.time() + wait_seconds
            while time.time() < end:
                time.sleep(0.1)
        finally:
            try:
                self.mqtt.unsubscribe(f"{self.discovery_prefix}/climate/#")
            except Exception:
                pass
            self.mqtt.on_message = original_on_message
            self.suppress_empty_command_logs = False

        for topic in sorted(found_topics):
            self.mqtt.publish(topic, "", qos=1, retain=True)
        for topic in sorted(found_state_topics):
            self.mqtt.publish(topic, "", qos=1, retain=True)
        for topic in sorted(found_command_topics):
            self.mqtt.publish(topic, "", qos=1, retain=True)

        log.info(
            "Scanned discovery and removed %s retained HMPD config topics (%s state, %s command)",
            len(found_topics),
            len(found_state_topics),
            len(found_command_topics),
        )

    def hmpd_candidates(self) -> List[str]:
        candidates = [
            self.configured_hmpd_path,
            "/homeassistant/hmpd",
            "/homeassistant/hmpd/hmpd",
            "/homeassistant/config/hmpd",
            "/homeassistant/config/hmpd/hmpd",
            "/config/hmpd",
            "/config/hmpd/hmpd",
            "/config/bin/hmpd",
            "/ha_config/hmpd",
            "/ha_config/hmpd/hmpd",
            "/share/hmpd",
            "/share/hmpd/hmpd",
            "/app/hmpd",
            "/app/hmpd/hmpd",
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
        for candidate in self.hmpd_candidates():
            candidate_paths = [candidate]
            if os.path.isdir(candidate):
                candidate_paths.append(os.path.join(candidate, "hmpd"))

            for path in candidate_paths:
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
        raise FileNotFoundError("Could not find executable hmpd. Checked: " + ", ".join(checked))

    def wait_for_hmpd(self) -> str:
        while True:
            try:
                return self.find_hmpd()
            except FileNotFoundError as exc:
                log.error(
                    "%s. Set hmpd_path in add-on options or place the binary in one of the checked paths. Retrying in %ss",
                    exc,
                    HMPD_FIND_RETRY_SECONDS,
                )
                time.sleep(HMPD_FIND_RETRY_SECONDS)

    def ensure_hmpd(self) -> str:
        if self.hmpd_path and os.path.isfile(self.hmpd_path) and os.access(self.hmpd_path, os.X_OK):
            return self.hmpd_path
        return self.find_hmpd()

    def mqtt_connect_loop(self) -> None:
        while not self.shutdown_event.is_set():
            try:
                log.info("Connecting to MQTT broker %s:%s", self.mqtt_host, self.mqtt_port)
                self.mqtt_connected = False
                self.mqtt.connect(self.mqtt_host, self.mqtt_port, keepalive=self.mqtt_keepalive)
                self.mqtt.loop_start()

                timeout = time.time() + 10
                while time.time() < timeout:
                    if self.mqtt_connected or self.shutdown_event.is_set():
                        return
                    time.sleep(0.2)

                log.error("MQTT connect timed out")
                self.mqtt.loop_stop()
                try:
                    self.mqtt.disconnect()
                except Exception:
                    pass
            except Exception as exc:
                log.error("MQTT connect failed: %s", exc, exc_info=True)
            
            # Sleep with periodic shutdown checks
            for _ in range(self.mqtt_retry_seconds):
                if self.shutdown_event.is_set():
                    break
                time.sleep(1.0)

    def ensure_mqtt_connected(self) -> None:
        if self.mqtt_connected:
            return
        try:
            self.mqtt.loop_stop()
        except Exception:
            pass
        self.mqtt_connect_loop()
        self.publish_bridge_status(True)

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if str(reason_code) == "Success":
            self.mqtt_connected = True
            log.info("Connected to MQTT broker %s:%s", self.mqtt_host, self.mqtt_port)
            client.subscribe(f"{self.base_topic}/+/set_target")
            client.subscribe(f"{self.base_topic}/+/booking/+/set")
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
                self.force_full_republish()
                return

            target_match = re.match(rf"^{re.escape(self.base_topic)}/([^/]+)/set_target$", topic)
            if target_match:
                self.handle_set_target_command(target_match.group(1), payload)
                return

            booking_match = re.match(
                rf"^{re.escape(self.base_topic)}/([^/]+)/booking/(state|on_temp|off_temp)/set$",
                topic,
            )
            if booking_match:
                self.handle_booking_command(
                    booking_match.group(1),
                    booking_match.group(2),
                    payload,
                )
                return
        except Exception as exc:
            log.error("MQTT message handling failed for topic %s: %s", topic, exc)

    def handle_set_target_command(self, zone_key: str, payload: str) -> None:
        if not payload:
            if DEBUG and not self.suppress_empty_command_logs:
                log.debug("Ignoring empty target payload for %s", zone_key)
            return

        zone = self.zones.get(zone_key)
        if not zone:
            if DEBUG:
                log.debug("Ignoring target payload for unknown zone key: %s", zone_key)
            return

        try:
            value = float(payload)
        except ValueError:
            log.warning("Invalid target payload for %s: %s", zone_key, payload)
            return

        requested_target = self.snap_target(value)
        zone.target_temp = requested_target
        controller_target = self.calculate_offset_adjusted_target(zone, requested_target)
        self.enqueue_set_zone_target(zone, controller_target)
        self.publish_state(zone)

    def handle_booking_command(self, zone_key: str, field_name: str, payload: str) -> None:
        if not payload:
            if DEBUG and not self.suppress_empty_command_logs:
                log.debug("Ignoring empty booking payload for %s (%s)", zone_key, field_name)
            return

        zone = self.zones.get(zone_key)
        if not zone:
            if DEBUG:
                log.debug("Ignoring booking payload for unknown zone key: %s", zone_key)
            return

        if field_name == "state":
            parsed_state = self.parse_booking_state(payload)
            if parsed_state is None:
                log.warning("Invalid booking state payload for %s: %s", zone_key, payload)
                return

            zone.booking_state = parsed_state
            self.persist_booking_zone_state()
            self.publish_booking_state(zone)
            self.maybe_apply_booking_target(zone, "manual_state")
            return

        try:
            value = float(payload)
        except ValueError:
            log.warning("Invalid booking %s payload for %s: %s", field_name, zone_key, payload)
            return

        snapped = self.snap_target(value)
        if field_name == "on_temp":
            zone.booking_on_temp = snapped
        else:
            zone.booking_off_temp = snapped

        self.persist_booking_zone_state()
        self.publish_booking_temperatures(zone)
        self.publish_state(zone)
        self.maybe_apply_booking_target(zone, f"{field_name}_update")

    def build_hmpd_cmd(self, controller: Controller, action_args: List[str]) -> List[str]:
        hmpd = self.ensure_hmpd()
        base = [hmpd, "--dev", controller.dev, "--baud", str(controller.baud), *action_args]
        if self.stdbuf_path:
            return [self.stdbuf_path, "-oL", *base]
        return base

    def start_controller_workers(self) -> None:
        for controller in self.controllers:
            if controller.key in self.queue_threads:
                continue
            
            # Check if previous thread died
            if controller.key in self.queue_threads:
                old_thread = self.queue_threads[controller.key]
                if not old_thread.is_alive():
                    log.critical("Previous worker thread for %s died! Restarting...", controller.name)
            
            thread = threading.Thread(
                target=self.controller_worker_loop,
                args=(controller,),
                name=f"hmpd_worker_{controller.key}",
                daemon=False,  # Not daemon - explicit shutdown required
            )
            self.queue_threads[controller.key] = thread
            thread.start()
            log.info("Started queue worker for controller %s", controller.name)

    def pending_set_count(self, controller_key: str) -> int:
        cond = self.queue_conditions[controller_key]
        with cond:
            return sum(1 for job in self.queue_items[controller_key] if job.kind == "set")

    def has_pending_job_kind(self, controller_key: str, kind: str) -> bool:
        cond = self.queue_conditions[controller_key]
        with cond:
            return any(job.kind == kind for job in self.queue_items[controller_key])

    def is_job_kind_active_or_queued(self, controller_key: str, kind: str) -> bool:
        if self.current_job_kind.get(controller_key) == kind:
            return True
        return self.has_pending_job_kind(controller_key, kind)

    def enqueue_controller_job(self, controller: Controller, job: ControllerJob, wait: bool = False) -> Tuple[List[str], bool]:
        cond = self.queue_conditions[controller.key]
        with cond:
            self.queue_items[controller.key].append(job)
            queued_len = len(self.queue_items[controller.key])
            cond.notify()

        if DEBUG:
            log.debug(
                "Enqueued %s for %s reason=%s queue_len=%s",
                job.kind,
                controller.name,
                job.reason or "-",
                queued_len,
            )

        if not wait:
            return [], False

        job.done.wait()
        if job.error is not None:
            error = job.error
            job.error = None  # Clear to prevent memory leak
            raise error
        return job.result_lines, job.result_partial

    def controller_worker_loop(self, controller: Controller) -> None:
        cond = self.queue_conditions[controller.key]
        while not self.shutdown_event.is_set():
            try:
                with cond:
                    while not self.queue_items[controller.key] and not self.shutdown_event.is_set():
                        cond.wait(timeout=1.0)  # Timeout to check shutdown signal periodically
                    if self.shutdown_event.is_set():
                        break
                    if not self.queue_items[controller.key]:
                        continue
                    job = self.queue_items[controller.key].pop(0)

                self.current_job_kind[controller.key] = job.kind
                try:
                    self.execute_job_with_retries(controller, job)
                except Exception as exc:
                    job.error = exc
                    log.error(
                        "Queue job failed permanently kind=%s controller=%s reason=%s err=%s",
                        job.kind,
                        controller.name,
                        job.reason or "-",
                        exc,
                        exc_info=True,  # Log full traceback for debugging
                    )
                finally:
                    self.last_command_finished_at[controller.key] = time.monotonic()
                    self.current_job_kind[controller.key] = None
                    job.done.set()
            except Exception as exc:
                log.critical(
                    "Unhandled exception in controller worker %s: %s",
                    controller.name,
                    exc,
                    exc_info=True,
                )
                time.sleep(5.0)  # Avoid spinning if something goes very wrong
        log.info("Controller worker loop exiting for %s", controller.name)

    def enforce_command_gap(self, controller: Controller) -> None:
        last_finished = self.last_command_finished_at.get(controller.key, 0.0)
        now = time.monotonic()
        remaining = self.command_gap_seconds - (now - last_finished)
        if remaining > 0:
            if DEBUG:
                log.debug("Waiting %.2fs command gap for %s", remaining, controller.name)
            time.sleep(remaining)

    def execute_job_with_retries(self, controller: Controller, job: ControllerJob) -> None:
        last_exc: Optional[Exception] = None

        for attempt in range(1, self.max_command_attempts + 1):
            try:
                self.enforce_command_gap(controller)

                if DEBUG:
                    log.debug(
                        "Running queued job attempt=%s/%s kind=%s controller=%s reason=%s",
                        attempt,
                        self.max_command_attempts,
                        job.kind,
                        controller.name,
                        job.reason or "-",
                    )

                if job.kind == "set":
                    self.execute_set_job(controller, job)
                    job.result_lines = []
                    job.result_partial = False
                    return

                lines, partial = self.run_hmpd_now(controller, job.action_args, job.timeout)

                if job.kind == "temps":
                    self.validate_temps_result(controller, lines)
                    self.apply_temps_result(controller, lines)
                    self.next_temp_sync_at[controller.key] = time.monotonic() + self.current_temp_sync_interval
                elif job.kind == "regs":
                    self.validate_regs_result(controller, lines, partial)
                    self.apply_regs_result(controller, lines, partial)
                    self.next_target_sync_at[controller.key] = time.monotonic() + self.target_sync_interval
                else:
                    raise RuntimeError(f"Unsupported queue job kind: {job.kind}")

                job.result_lines = lines
                job.result_partial = partial
                return

            except Exception as exc:
                last_exc = exc

                if attempt >= self.max_command_attempts:
                    break

                delay = self.retry_delays_seconds[min(attempt - 1, len(self.retry_delays_seconds) - 1)]
                log.warning(
                    "Command failed attempt %s/%s for controller=%s kind=%s reason=%s: %s. Retrying in %ss",
                    attempt,
                    self.max_command_attempts,
                    controller.name,
                    job.kind,
                    job.reason or "-",
                    exc,
                    delay,
                )
                time.sleep(delay)

        log.critical(
            "BIG WARNING: command failed after %s attempts for controller=%s kind=%s reason=%s action=%s last_error=%s",
            self.max_command_attempts,
            controller.name,
            job.kind,
            job.reason or "-",
            job.action_args,
            last_exc,
        )
        raise RuntimeError(
            f"Command failed after {self.max_command_attempts} attempts for {controller.name} "
            f"kind={job.kind} reason={job.reason or '-'}: {last_exc}"
        )

    def run_hmpd_now(self, controller: Controller, action_args: List[str], timeout: int) -> Tuple[List[str], bool]:
        cmd = self.build_hmpd_cmd(controller, action_args)
        if DEBUG:
            log.debug("Running: %s", shlex.join(cmd))

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
            raise RuntimeError(f"Command {cmd!r} timed out after {timeout} seconds")

        if proc.returncode != 0:
            raise RuntimeError(f"hmpd exited {proc.returncode}: {stderr.strip() or stdout.strip()}")

        return lines, False

    def validate_temps_result(self, controller: Controller, lines: List[str]) -> None:
        temps = self.parse_temps(controller, lines)
        if not temps:
            raise RuntimeError(f"No valid temps returned for controller {controller.name}")

    def validate_regs_result(self, controller: Controller, lines: List[str], is_partial: bool) -> None:
        if is_partial:
            raise RuntimeError(f"Partial regs output returned for controller {controller.name}")

        parsed = self.parse_regs(controller, lines)
        if not parsed:
            raise RuntimeError(f"No valid regs returned for controller {controller.name}")

        zone_count = len(parsed)
        expected = max(1, int(controller.expected_regs))

        if zone_count < expected:
            raise RuntimeError(
                f"Incomplete regs output for controller {controller.name}: got {zone_count}, expected at least {expected}"
            )

    def run_sync_job(self, controller: Controller, kind: str, reason: str) -> Tuple[List[str], bool]:
        if kind == "temps":
            job = ControllerJob(
                kind="temps",
                action_args=["temps"],
                timeout=self.temps_timeout,
                reason=reason,
            )
        elif kind == "regs":
            job = ControllerJob(
                kind="regs",
                action_args=["regs"],
                timeout=self.regs_timeout,
                reason=reason,
            )
        else:
            raise ValueError(f"Unsupported sync job kind: {kind}")

        return self.enqueue_controller_job(controller, job, wait=True)

    def enqueue_set_zone_target(self, zone: Zone, target: float, reason: str = "mqtt_set_target") -> None:
        controller = self.controllers_by_key.get(zone.controller_key)
        if controller is None:
            log.error("Unknown controller for zone %s", zone.unique_id)
            return

        job = ControllerJob(
            kind="set",
            timeout=self.set_timeout,
            zone_unique_id=zone.unique_id,
            target=target,
            reason=reason,
        )
        self.enqueue_controller_job(controller, job, wait=False)
        log.info("Queued set for %s (%s) to %.1f", zone.zone_name, zone.unique_id, target)

    def execute_set_job(self, controller: Controller, job: ControllerJob) -> None:
        zone = self.zones.get(job.zone_unique_id or "")
        if zone is None:
            raise RuntimeError(f"Zone not found for queued set: {job.zone_unique_id}")

        controller_target = self.snap_target(float(job.target if job.target is not None else self.temp_min))

        log.info(
            "Processing set for %s (%s) to controller target %.1f",
            zone.zone_name,
            zone.unique_id,
            controller_target,
        )

        self.run_hmpd_now(
            controller,
            ["set", str(zone.zone_index), f"{controller_target:.1f}"],
            self.set_timeout,
        )

        zone.controller_target_temp = controller_target
        if zone.target_temp is None:
            zone.target_temp = self.infer_display_target_from_controller_target(zone) or controller_target
        self.publish_state(zone)
        log.info("Set %s (%s) controller target to %.1f", zone.zone_name, zone.unique_id, controller_target)

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
                    target_temp = self.snap_target(raw_tgt)

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
        state_topic = self.state_topic(zone.unique_id)
        command_topic = self.command_topic(zone.unique_id)
        object_id = f"hmpd_{zone.unique_id}"

        return {
            "name": zone.zone_name,
            "object_id": object_id,
            "unique_id": object_id,
            "availability_topic": f"{self.base_topic}/bridge/status",
            "payload_available": "online",
            "payload_not_available": "offline",
            "current_temperature_topic": state_topic,
            "current_temperature_template": "{{ value_json.current_temp }}",
            "temperature_state_topic": state_topic,
            "temperature_state_template": "{{ value_json.target_temp }}",
            "temperature_command_topic": command_topic,
            "mode_state_topic": state_topic,
            "mode_state_template": "{{ value_json.mode }}",
            "modes": ["off", "heat"],
            "min_temp": self.temp_min,
            "max_temp": self.temp_max,
            "temp_step": self.temp_step,
            "device": {
                "identifiers": [f"hmpd_{zone.unique_id}"],
                "name": zone.zone_name,
                "manufacturer": "HMPD",
                "model": "Thermostat Regulator",
                "via_device": f"hmpd_bridge_{zone.controller_key}",
            },
            "suggested_area": zone.controller_name,
        }

    def booking_discovery_payload(self, zone: Zone) -> dict:
        object_id = f"hmpd_{zone.unique_id}_booking"
        return {
            "name": f"{zone.zone_name} Booked",
            "object_id": object_id,
            "unique_id": object_id,
            "availability_topic": f"{self.base_topic}/bridge/status",
            "payload_available": "online",
            "payload_not_available": "offline",
            "state_topic": self.booking_state_topic(zone.unique_id),
            "command_topic": self.booking_state_command_topic(zone.unique_id),
            "state_on": "on",
            "state_off": "off",
            "payload_on": "on",
            "payload_off": "off",
            "icon": "mdi:calendar-check",
            "device": {
                "identifiers": [f"hmpd_{zone.unique_id}"],
                "name": zone.zone_name,
                "manufacturer": "HMPD",
                "model": "Thermostat Regulator",
                "via_device": f"hmpd_bridge_{zone.controller_key}",
            },
            "suggested_area": zone.controller_name,
        }

    def booking_on_temp_discovery_payload(self, zone: Zone) -> dict:
        object_id = f"hmpd_{zone.unique_id}_booking_on_temp"
        return {
            "name": f"{zone.zone_name} Booking On Temp",
            "object_id": object_id,
            "unique_id": object_id,
            "availability_topic": f"{self.base_topic}/bridge/status",
            "payload_available": "online",
            "payload_not_available": "offline",
            "state_topic": self.booking_on_temp_state_topic(zone.unique_id),
            "command_topic": self.booking_on_temp_command_topic(zone.unique_id),
            "min": self.temp_min,
            "max": self.temp_max,
            "step": self.temp_step,
            "mode": "box",
            "unit_of_measurement": "C",
            "device": {
                "identifiers": [f"hmpd_{zone.unique_id}"],
                "name": zone.zone_name,
                "manufacturer": "HMPD",
                "model": "Thermostat Regulator",
                "via_device": f"hmpd_bridge_{zone.controller_key}",
            },
            "suggested_area": zone.controller_name,
        }

    def booking_off_temp_discovery_payload(self, zone: Zone) -> dict:
        object_id = f"hmpd_{zone.unique_id}_booking_off_temp"
        return {
            "name": f"{zone.zone_name} Booking Off Temp",
            "object_id": object_id,
            "unique_id": object_id,
            "availability_topic": f"{self.base_topic}/bridge/status",
            "payload_available": "online",
            "payload_not_available": "offline",
            "state_topic": self.booking_off_temp_state_topic(zone.unique_id),
            "command_topic": self.booking_off_temp_command_topic(zone.unique_id),
            "min": self.temp_min,
            "max": self.temp_max,
            "step": self.temp_step,
            "mode": "box",
            "unit_of_measurement": "C",
            "device": {
                "identifiers": [f"hmpd_{zone.unique_id}"],
                "name": zone.zone_name,
                "manufacturer": "HMPD",
                "model": "Thermostat Regulator",
                "via_device": f"hmpd_bridge_{zone.controller_key}",
            },
            "suggested_area": zone.controller_name,
        }

    def is_zone_off_mode(self, zone: Zone) -> bool:
        if zone.target_temp is None:
            return False
        return zone.target_temp <= OFF_MODE_TARGET_THRESHOLD

    def publish_bridge_status(self, online: bool) -> None:
        payload = "online" if online else "offline"
        self.mqtt.publish(f"{self.base_topic}/bridge/status", payload, qos=1, retain=True)

    def publish_discovery(self, zone: Zone) -> None:
        topic = self.discovery_topic(zone.unique_id)
        payload = json.dumps(self.discovery_payload(zone), ensure_ascii=False, sort_keys=True)
        if zone.last_discovery_payload == payload and zone.discovered:
            pass
        else:
            self.mqtt.publish(topic, payload, qos=1, retain=self.retain_discovery)
            zone.last_discovery_payload = payload
            zone.discovered = True

        booking_payload = json.dumps(
            self.booking_discovery_payload(zone),
            ensure_ascii=False,
            sort_keys=True,
        )
        if not (zone.booking_discovered and zone.last_booking_discovery_payload == booking_payload):
            self.mqtt.publish(
                self.booking_discovery_topic(zone.unique_id),
                booking_payload,
                qos=1,
                retain=self.retain_discovery,
            )
            zone.last_booking_discovery_payload = booking_payload
            zone.booking_discovered = True

        on_temp_payload = json.dumps(
            self.booking_on_temp_discovery_payload(zone),
            ensure_ascii=False,
            sort_keys=True,
        )
        if not (
            zone.booking_on_temp_discovered
            and zone.last_booking_on_temp_discovery_payload == on_temp_payload
        ):
            self.mqtt.publish(
                self.booking_on_temp_discovery_topic(zone.unique_id),
                on_temp_payload,
                qos=1,
                retain=self.retain_discovery,
            )
            zone.last_booking_on_temp_discovery_payload = on_temp_payload
            zone.booking_on_temp_discovered = True

        off_temp_payload = json.dumps(
            self.booking_off_temp_discovery_payload(zone),
            ensure_ascii=False,
            sort_keys=True,
        )
        if not (
            zone.booking_off_temp_discovered
            and zone.last_booking_off_temp_discovery_payload == off_temp_payload
        ):
            self.mqtt.publish(
                self.booking_off_temp_discovery_topic(zone.unique_id),
                off_temp_payload,
                qos=1,
                retain=self.retain_discovery,
            )
            zone.last_booking_off_temp_discovery_payload = off_temp_payload
            zone.booking_off_temp_discovered = True

    def publish_state(self, zone: Zone) -> None:
        topic = self.state_topic(zone.unique_id)

        current_temp = zone.current_temp if zone.current_temp is not None else self.temp_min
        target_temp = zone.target_temp if zone.target_temp is not None else self.temp_min
        controller_target_temp = zone.controller_target_temp if zone.controller_target_temp is not None else target_temp

        if self.is_zone_off_mode(zone):
            mode = "off"
        else:
            mode = "heat"

        payload_data = {
            "current_temp": current_temp,
            "target_temp": target_temp,
            "mode": mode,
            "booking_state": "on" if zone.booking_state else "off",
            "booking_on_temp": zone.booking_on_temp,
            "booking_off_temp": zone.booking_off_temp,
        }

        if zone.external_sensor_entity_id:
            payload_data["external_sensor_entity_id"] = zone.external_sensor_entity_id
            payload_data["controller_target_temp"] = controller_target_temp
            if zone.built_in_current_temp is not None:
                payload_data["built_in_current_temp"] = zone.built_in_current_temp

        payload = json.dumps(payload_data, sort_keys=True)

        if zone.last_state_payload == payload:
            return

        self.mqtt.publish(topic, payload, qos=1, retain=self.retain_state)
        zone.last_state_payload = payload

    def publish_booking_state(self, zone: Zone) -> None:
        topic = self.booking_state_topic(zone.unique_id)
        payload = "on" if zone.booking_state else "off"

        if zone.last_booking_state_payload == payload:
            return

        self.mqtt.publish(topic, payload, qos=1, retain=self.retain_state)
        zone.last_booking_state_payload = payload

    def publish_booking_temperatures(self, zone: Zone) -> None:
        on_topic = self.booking_on_temp_state_topic(zone.unique_id)
        off_topic = self.booking_off_temp_state_topic(zone.unique_id)
        on_payload = f"{self.snap_target(zone.booking_on_temp):.1f}"
        off_payload = f"{self.snap_target(zone.booking_off_temp):.1f}"

        if zone.last_booking_on_temp_payload != on_payload:
            self.mqtt.publish(on_topic, on_payload, qos=1, retain=self.retain_state)
            zone.last_booking_on_temp_payload = on_payload

        if zone.last_booking_off_temp_payload != off_payload:
            self.mqtt.publish(off_topic, off_payload, qos=1, retain=self.retain_state)
            zone.last_booking_off_temp_payload = off_payload

    def remove_zone(self, unique_id: str) -> None:
        self.cleanup_unique_id(unique_id)
        if unique_id in self.zones:
            del self.zones[unique_id]
            self.persist_booking_zone_state()
        log.info("Removed thermostat %s", unique_id)

    def apply_temps_result(self, controller: Controller, lines: List[str]) -> None:
        temps = self.parse_temps(controller, lines)
        self.latest_temps[controller.key] = temps

        updated = 0
        for idx, current_temp in temps.items():
            unique_id = self.zone_unique_id(controller, idx)
            zone = self.zones.get(unique_id)
            if zone is not None:
                zone.built_in_current_temp = current_temp
                zone.current_temp = current_temp
                self.apply_external_sensor_temperature(zone)
                if zone.target_temp is None:
                    zone.target_temp = self.infer_display_target_from_controller_target(zone)
                self.maybe_enqueue_offset_adjustment(zone, "temp_sync")
                self.publish_state(zone)
                updated += 1

        total_active = len([zone for zone in self.zones.values() if zone.controller_key == controller.key])
        log.info(
            "Cached %s temperatures for controller %s (%s active zones updated, %s total active)",
            len(temps),
            controller.name,
            updated,
            total_active,
        )

    def apply_regs_result(self, controller: Controller, lines: List[str], is_partial: bool) -> None:
        parsed = self.parse_regs(controller, lines)
        self.latest_regs[controller.key] = parsed
        temps = self.latest_temps.get(controller.key, {})

        created = 0
        updated = 0
        skipped_no_temp = 0
        valid_unique_ids: Set[str] = set()
        seen_zone_names: Dict[str, int] = {}

        for idx, data in parsed.items():
            name = data["name"].strip() if data["name"] else ""
            if not name:
                continue

            current_temp = temps.get(idx)
            if current_temp is None:
                current_temp = data.get("current_temp")
            if current_temp is None:
                skipped_no_temp += 1
                continue

            unique_id = self.zone_unique_id(controller, idx)
            valid_unique_ids.add(unique_id)
            zone = self.zones.get(unique_id)
            seen_zone_names[name.casefold()] = seen_zone_names.get(name.casefold(), 0) + 1
            booking_name, booking_entity_ids = self.get_booking_entity_info(controller.name, idx)
            saved_on_temp, saved_off_temp, saved_state = self.get_saved_booking_values(unique_id)

            if zone is None:
                zone = Zone(
                    controller_name=controller.name,
                    controller_key=controller.key,
                    controller_dev=controller.dev,
                    zone_index=idx,
                    zone_name=name,
                    unique_id=unique_id,
                    current_temp=current_temp,
                    built_in_current_temp=current_temp,
                    controller_target_temp=data["target_temp"],
                    target_temp=data["target_temp"],
                    enabled=data["enabled"],
                    external_sensor_entity_id=self.get_external_sensor_entity_id(controller.name, idx),
                    booking_entity_ids=booking_entity_ids,
                    booking_name=booking_name,
                    booking_state=saved_state,
                    booking_on_temp=saved_on_temp,
                    booking_off_temp=saved_off_temp,
                )
                self.zones[unique_id] = zone
                created += 1
            else:
                zone.zone_name = name
                zone.current_temp = current_temp
                zone.built_in_current_temp = current_temp
                if data["target_temp"] is not None:
                    zone.controller_target_temp = data["target_temp"]
                    if zone.target_temp is None or not self.uses_external_sensor(zone):
                        zone.target_temp = data["target_temp"]
                zone.enabled = data["enabled"]
                zone.booking_entity_ids = booking_entity_ids
                zone.booking_name = booking_name
                zone.booking_on_temp = self.snap_target(zone.booking_on_temp)
                zone.booking_off_temp = self.snap_target(zone.booking_off_temp)
                updated += 1

            self.apply_external_sensor_temperature(zone)
            inferred_target = self.infer_display_target_from_controller_target(zone)
            if inferred_target is not None and zone.target_temp is None:
                zone.target_temp = inferred_target

            if zone.booking_entity_ids:
                self.maybe_apply_booking_target(zone, "regs_sync")
            self.maybe_enqueue_offset_adjustment(zone, "regs_sync")
            self.publish_discovery(zone)
            self.publish_booking_state(zone)
            self.publish_booking_temperatures(zone)
            self.publish_state(zone)

        stale_for_controller = [
            unique_id
            for unique_id, zone in self.zones.items()
            if zone.controller_key == controller.key and unique_id not in valid_unique_ids
        ]
        for unique_id in stale_for_controller:
            self.remove_zone(unique_id)

        self.persist_booking_zone_state()

        duplicate_names = sorted(name for name, count in seen_zone_names.items() if count > 1)
        if duplicate_names:
            log.warning(
                "Controller %s returned duplicate zone names for multiple indices: %s",
                controller.name,
                ", ".join(duplicate_names),
            )

        log.info(
            "Synced regs for %s (%s created, %s updated, %s skipped_no_temp)%s",
            controller.name,
            created,
            updated,
            skipped_no_temp,
            " (partial)" if is_partial else "",
        )

    def sync_temps_blocking(self, controller: Controller, reason: str) -> None:
        self.run_sync_job(controller, "temps", reason)

    def sync_regs_blocking(self, controller: Controller, reason: str) -> None:
        self.run_sync_job(controller, "regs", reason)

    def force_full_republish(self) -> None:
        self.scan_and_cleanup_discovery_topics()
        self.cleanup_all_retained_topics()
        self.cleanup_legacy_alias_topics()

        self.zones = {}
        self.latest_temps = {}
        self.latest_regs = {}

        for controller in self.controllers:
            self.sync_temps_blocking(controller, "startup_initial_current_temp_sync")
        for controller in self.controllers:
            self.sync_regs_blocking(controller, "startup_initial_target_sync")

        self.sync_booking_states_from_home_assistant()

    def scheduler_loop(self) -> None:
        last_health_check = time.monotonic()
        
        while not self.shutdown_event.is_set():
            try:
                self.ensure_mqtt_connected()
                now = time.monotonic()
                
                # Check worker thread health every 60 seconds
                if (now - last_health_check) > 60.0:
                    self._health_check_workers()
                    last_health_check = now

                for controller in self.controllers:
                    controller_key = controller.key

                    if now >= self.next_temp_sync_at[controller_key]:
                        if not self.is_job_kind_active_or_queued(controller_key, "temps"):
                            self.enqueue_controller_job(
                                controller,
                                ControllerJob(
                                    kind="temps",
                                    action_args=["temps"],
                                    timeout=self.temps_timeout,
                                    reason="scheduled_current_temp_sync",
                                ),
                                wait=False,
                            )

                    if now >= self.next_target_sync_at[controller_key]:
                        pending_sets = self.pending_set_count(controller_key)
                        set_running = self.current_job_kind.get(controller_key) == "set"

                        if pending_sets > 0 or set_running:
                            if DEBUG:
                                log.debug(
                                    "Delaying target sync for %s because set command(s) are queued or running",
                                    controller.name,
                                )
                        elif not self.is_job_kind_active_or_queued(controller_key, "regs"):
                            self.enqueue_controller_job(
                                controller,
                                ControllerJob(
                                    kind="regs",
                                    action_args=["regs"],
                                    timeout=self.regs_timeout,
                                    reason="scheduled_target_sync",
                                ),
                                wait=False,
                            )

                if now >= self.next_booking_sync_at:
                    self.sync_booking_states_from_home_assistant()
                    self.next_booking_sync_at = now + self.booking_sync_interval

                time.sleep(1.0)
            except Exception as exc:
                log.critical(
                    "Unhandled exception in scheduler loop: %s - recovering...",
                    exc,
                    exc_info=True,
                )
                time.sleep(5.0)  # Brief pause before recovery
                # Try to re-establish MQTT connection on recovery
                self.mqtt_connected = False
        
        log.info("Scheduler loop exiting")

    def _health_check_workers(self) -> None:
        """Check worker threads are alive, restart if dead"""
        for controller in self.controllers:
            thread = self.queue_threads.get(controller.key)
            if thread and not thread.is_alive():
                log.critical(
                    "Worker thread for controller %s is DEAD! Restarting immediately...",
                    controller.name,
                )
                # Restart the worker
                new_thread = threading.Thread(
                    target=self.controller_worker_loop,
                    args=(controller,),
                    name=f"hmpd_worker_{controller.key}",
                    daemon=False,
                )
                self.queue_threads[controller.key] = new_thread
                new_thread.start()

    def start(self):
        try:
            self.wait_for_hmpd()
            log.info("=== HMPD Thermostat Bridge starting ===")
            for controller in self.controllers:
                log.info(
                    "Configured controller %s -> %s @ %s (key=%s, expected_regs=%s)",
                    controller.name,
                    controller.dev,
                    controller.baud,
                    controller.key,
                    controller.expected_regs,
                )

            self.start_controller_workers()
            self.mqtt_connect_loop()
            self.publish_bridge_status(True)

            try:
                self.force_full_republish()
            except Exception as exc:
                log.error(
                    "Initial republish failed (will retry on next sync): %s",
                    exc,
                    exc_info=True,
                )

            now = time.monotonic()
            for controller in self.controllers:
                self.next_temp_sync_at[controller.key] = now + self.current_temp_sync_interval
                self.next_target_sync_at[controller.key] = now + self.target_sync_interval
            self.next_booking_sync_at = now + self.booking_sync_interval

            self.scheduler_loop()
        except Exception as exc:
            log.critical(
                "Fatal error in start: %s",
                exc,
                exc_info=True,
            )
            raise
        finally:
            log.info("Shutting down HMPD Bridge")
            self.shutdown_event.set()
            
            # Signal all worker threads to stop and wait for them
            for controller in self.controllers:
                cond = self.queue_conditions[controller.key]
                with cond:
                    cond.notify_all()  # Wake up any waiting workers
            
            # Wait for worker threads to exit (max 10 seconds)
            deadline = time.time() + 10.0
            for controller in self.controllers:
                thread = self.queue_threads.get(controller.key)
                if thread:
                    remaining = max(0.1, deadline - time.time())
                    thread.join(timeout=remaining)
                    if thread.is_alive():
                        log.warning("Worker thread %s did not exit cleanly", controller.name)
            
            self.publish_bridge_status(False)
            try:
                self.mqtt.loop_stop()
            except Exception:
                pass
            try:
                self.mqtt.disconnect()
            except Exception:
                pass
            log.info("HMPD Bridge shutdown complete")


if __name__ == "__main__":
    try:
        HMPDBridge().start()
    except KeyboardInterrupt:
        log.info("Received SIGINT, shutting down gracefully")
    except Exception as exc:
        log.critical("Fatal error: %s", exc, exc_info=True)
        raise
