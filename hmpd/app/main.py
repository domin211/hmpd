import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import paho.mqtt.client as mqtt


OPTIONS_PATH = "/data/options.json"

MQTT_HOST = os.getenv("MQTT_HOST", "core-mosquitto")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USERNAME = os.getenv("MQTT_USERNAME", "ufandy")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "Fanda18067")
MQTT_DISCOVERY_PREFIX = os.getenv("MQTT_DISCOVERY_PREFIX", "homeassistant")
MQTT_BASE_TOPIC = os.getenv("MQTT_BASE_TOPIC", "hmpd")
MQTT_CLIENT_ID = os.getenv("MQTT_CLIENT_ID", "hmpd_bridge")
MQTT_KEEPALIVE = 60
MQTT_RETRY_SECONDS = 10

CONTROLLERS = [
    {"name": "usb0", "dev": "/dev/ttyUSB0", "baud": 4800},
    {"name": "usb1", "dev": "/dev/ttyUSB1", "baud": 4800},
]

CURRENT_TEMP_SYNC_INTERVAL = 60
TARGET_SYNC_INTERVAL = 3600

TEMPS_TIMEOUT = 15
REGS_TIMEOUT = 20
SET_TIMEOUT = 20

COMMAND_GAP_SECONDS = 3.0
MAX_COMMAND_ATTEMPTS = 5
RETRY_DELAYS_SECONDS = [5, 10, 60, 60]

TEMP_MIN = 16.0
TEMP_MAX = 32.0
TEMP_STEP = 0.5

CURRENT_TEMP_MIN = 5.0
CURRENT_TEMP_MAX = 50.0

HMPD_PATH = "/homeassistant/hmpd"

RETAIN_DISCOVERY = True
RETAIN_STATE = True

DEBUG_LOG_FILE = "/config/hmpd_bridge.log"
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


@dataclass(frozen=True)
class Controller:
    name: str
    dev: str
    baud: int

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
    target_temp: Optional[float] = None
    enabled: Optional[bool] = None
    discovered: bool = False
    last_discovery_payload: Optional[str] = None
    last_state_payload: Optional[str] = None


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
        self.discovery_prefix = MQTT_DISCOVERY_PREFIX
        self.base_topic = MQTT_BASE_TOPIC
        self.mqtt_host = MQTT_HOST
        self.mqtt_port = MQTT_PORT
        self.mqtt_username = MQTT_USERNAME
        self.mqtt_password = MQTT_PASSWORD
        self.mqtt_client_id = MQTT_CLIENT_ID
        self.mqtt_keepalive = MQTT_KEEPALIVE
        self.mqtt_retry_seconds = MQTT_RETRY_SECONDS

        self.current_temp_sync_interval = CURRENT_TEMP_SYNC_INTERVAL
        self.target_sync_interval = TARGET_SYNC_INTERVAL

        self.temps_timeout = TEMPS_TIMEOUT
        self.regs_timeout = REGS_TIMEOUT
        self.set_timeout = SET_TIMEOUT

        self.command_gap_seconds = COMMAND_GAP_SECONDS
        self.max_command_attempts = MAX_COMMAND_ATTEMPTS
        self.retry_delays_seconds = list(RETRY_DELAYS_SECONDS)

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

        self.next_temp_sync_at: Dict[str, float] = {
            controller.key: 0.0 for controller in self.controllers
        }
        self.next_target_sync_at: Dict[str, float] = {
            controller.key: 0.0 for controller in self.controllers
        }

        self.mqtt = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=self.mqtt_client_id,
        )
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

    def zone_unique_id(self, controller: Controller, zone_index: int) -> str:
        return f"{controller.key}_{int(zone_index)}"

    def state_topic(self, unique_id: str) -> str:
        return f"{self.base_topic}/{unique_id}/state"

    def command_topic(self, unique_id: str) -> str:
        return f"{self.base_topic}/{unique_id}/set_target"

    def discovery_topic(self, unique_id: str) -> str:
        return f"{self.discovery_prefix}/climate/hmpd_{unique_id}/config"

    def cleanup_unique_id(self, unique_id: str) -> None:
        self.mqtt.publish(self.discovery_topic(unique_id), "", qos=1, retain=True)
        self.mqtt.publish(self.state_topic(unique_id), "", qos=1, retain=True)
        self.mqtt.publish(self.command_topic(unique_id), "", qos=1, retain=True)

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
        raise FileNotFoundError("Could not find executable hmpd. Checked: " + ", ".join(checked))

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

            m = re.match(rf"^{re.escape(self.base_topic)}/([^/]+)/set_target$", topic)
            if not m:
                return

            zone_key = m.group(1)
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

            self.enqueue_set_zone_target(zone, self.snap_target(value))
        except Exception as exc:
            log.error("MQTT message handling failed for topic %s: %s", topic, exc)

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
            thread = threading.Thread(
                target=self.controller_worker_loop,
                args=(controller,),
                name=f"hmpd_worker_{controller.key}",
                daemon=True,
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
            raise job.error
        return job.result_lines, job.result_partial

    def controller_worker_loop(self, controller: Controller) -> None:
        cond = self.queue_conditions[controller.key]
        while True:
            with cond:
                while not self.queue_items[controller.key]:
                    cond.wait()
                job = self.queue_items[controller.key].pop(0)

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
                )
            finally:
                self.last_command_finished_at[controller.key] = time.monotonic()
                job.done.set()

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
                elif job.kind == "regs":
                    self.validate_regs_result(controller, lines, partial)
                    self.apply_regs_result(controller, lines, partial)
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
        if zone_count < MAX_ZONE_INDEX:
            raise RuntimeError(
                f"Incomplete regs output for controller {controller.name}: got {zone_count}, expected at least {MAX_ZONE_INDEX}"
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

    def enqueue_set_zone_target(self, zone: Zone, target: float) -> None:
        controller = self.controllers_by_key.get(zone.controller_key)
        if controller is None:
            log.error("Unknown controller for zone %s", zone.unique_id)
            return

        job = ControllerJob(
            kind="set",
            timeout=self.set_timeout,
            zone_unique_id=zone.unique_id,
            target=target,
            reason="mqtt_set_target",
        )
        self.enqueue_controller_job(controller, job, wait=False)
        log.info("Queued set for %s (%s) to %.1f", zone.zone_name, zone.unique_id, target)

    def execute_set_job(self, controller: Controller, job: ControllerJob) -> None:
        zone = self.zones.get(job.zone_unique_id or "")
        if zone is None:
            raise RuntimeError(f"Zone not found for queued set: {job.zone_unique_id}")

        target = self.snap_target(float(job.target if job.target is not None else self.temp_min))

        log.info(
            "Processing set for %s (%s) to %.1f",
            zone.zone_name,
            zone.unique_id,
            target,
        )

        self.run_hmpd_now(
            controller,
            ["set", str(zone.zone_index), f"{target:.1f}"],
            self.set_timeout,
        )

        zone.target_temp = target
        self.publish_state(zone)
        log.info("Set %s (%s) to %.1f", zone.zone_name, zone.unique_id, target)

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
            "modes": ["heat"],
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

    def publish_bridge_status(self, online: bool) -> None:
        payload = "online" if online else "offline"
        self.mqtt.publish(f"{self.base_topic}/bridge/status", payload, qos=1, retain=True)

    def publish_discovery(self, zone: Zone) -> None:
        topic = self.discovery_topic(zone.unique_id)
        payload = json.dumps(self.discovery_payload(zone), ensure_ascii=False, sort_keys=True)
        if zone.last_discovery_payload == payload and zone.discovered:
            return
        self.mqtt.publish(topic, payload, qos=1, retain=self.retain_discovery)
        zone.last_discovery_payload = payload
        zone.discovered = True

    def publish_state(self, zone: Zone) -> None:
        topic = self.state_topic(zone.unique_id)
        payload = json.dumps(
            {
                "current_temp": zone.current_temp if zone.current_temp is not None else self.temp_min,
                "target_temp": zone.target_temp if zone.target_temp is not None else self.temp_min,
                "mode": "heat",
            },
            sort_keys=True,
        )
        if zone.last_state_payload == payload:
            return
        self.mqtt.publish(topic, payload, qos=1, retain=self.retain_state)
        zone.last_state_payload = payload

    def remove_zone(self, unique_id: str) -> None:
        self.cleanup_unique_id(unique_id)
        if unique_id in self.zones:
            del self.zones[unique_id]
        log.info("Removed thermostat %s", unique_id)

    def apply_temps_result(self, controller: Controller, lines: List[str]) -> None:
        temps = self.parse_temps(controller, lines)
        self.latest_temps[controller.key] = temps

        updated = 0
        for idx, current_temp in temps.items():
            unique_id = self.zone_unique_id(controller, idx)
            zone = self.zones.get(unique_id)
            if zone is not None:
                zone.current_temp = current_temp
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

            if zone is None:
                zone = Zone(
                    controller_name=controller.name,
                    controller_key=controller.key,
                    controller_dev=controller.dev,
                    zone_index=idx,
                    zone_name=name,
                    unique_id=unique_id,
                    current_temp=current_temp,
                    target_temp=data["target_temp"],
                    enabled=data["enabled"],
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

            self.publish_discovery(zone)
            self.publish_state(zone)

        stale_for_controller = [
            unique_id
            for unique_id, zone in self.zones.items()
            if zone.controller_key == controller.key and unique_id not in valid_unique_ids
        ]
        for unique_id in stale_for_controller:
            self.remove_zone(unique_id)

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

    def scheduler_loop(self) -> None:
        while True:
            self.ensure_mqtt_connected()
            now = time.monotonic()

            for controller in self.controllers:
                controller_key = controller.key

                if now >= self.next_temp_sync_at[controller_key]:
                    if not self.has_pending_job_kind(controller_key, "temps"):
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
                        self.next_temp_sync_at[controller_key] = now + self.current_temp_sync_interval

                if now >= self.next_target_sync_at[controller_key]:
                    pending_sets = self.pending_set_count(controller_key)
                    if pending_sets > 0:
                        if DEBUG:
                            log.debug(
                                "Delaying target sync for %s because %s set command(s) are still queued",
                                controller.name,
                                pending_sets,
                            )
                    elif not self.has_pending_job_kind(controller_key, "regs"):
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
                        self.next_target_sync_at[controller_key] = now + self.target_sync_interval

            time.sleep(1.0)

    def start(self):
        self.find_hmpd()
        log.info("=== HMPD Thermostat Bridge starting ===")
        for controller in self.controllers:
            log.info(
                "Configured controller %s -> %s @ %s (key=%s)",
                controller.name,
                controller.dev,
                controller.baud,
                controller.key,
            )

        self.start_controller_workers()
        self.mqtt_connect_loop()
        self.publish_bridge_status(True)

        self.force_full_republish()

        now = time.monotonic()
        for controller in self.controllers:
            self.next_temp_sync_at[controller.key] = now + self.current_temp_sync_interval
            self.next_target_sync_at[controller.key] = now + self.target_sync_interval

        self.scheduler_loop()


if __name__ == "__main__":
    HMPDBridge().start()
