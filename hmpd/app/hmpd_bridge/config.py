"""Add-on configuration: options.json loading, controller definitions, temperature range."""
from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

OPTIONS_PATH = "/data/options.json"

DEFAULT_MQTT_HOST = os.getenv("MQTT_HOST", "core-mosquitto")
DEFAULT_MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
DEFAULT_MQTT_USERNAME = os.getenv("MQTT_USERNAME", "")
DEFAULT_MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")
MQTT_DISCOVERY_PREFIX = os.getenv("MQTT_DISCOVERY_PREFIX", "homeassistant")
MQTT_BASE_TOPIC = os.getenv("MQTT_BASE_TOPIC", "hmpd")
MQTT_CLIENT_ID = "hmpd_bridge"
MQTT_KEEPALIVE = 60
MQTT_RETRY_SECONDS = 10

DEFAULT_HMPD_PATH = os.getenv("HMPD_PATH", "/app/hmpd")
HMPD_FIND_RETRY_SECONDS = 30

DEFAULT_CONTROLLERS: list[dict[str, Any]] = [
    {"name": "usb0", "dev": "/dev/ttyUSB0", "baud": 4800, "expected_regs": 64},
    {"name": "usb1", "dev": "/dev/ttyUSB1", "baud": 4800, "expected_regs": 39},
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
TEMP_STEP = 1.0
OFF_MODE_TARGET_THRESHOLD = 18.0

RETAIN_DISCOVERY = True
RETAIN_STATE = True

DEBUG_LOG_FILE = "/config/hmpd_bridge.log"
DEBUG_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10MB per file
DEBUG_LOG_BACKUP_COUNT = 5  # 5 rotated backups = 60MB max


@dataclass(frozen=True)
class TempRange:
    """Controller-facing temperature bounds and step used to snap requested targets."""

    minimum: float = TEMP_MIN
    maximum: float = TEMP_MAX
    step: float = TEMP_STEP

    def snap(self, value: float) -> float:
        clamped = max(self.minimum, min(self.maximum, value))
        steps = round((clamped - self.minimum) / self.step)
        snapped = self.minimum + steps * self.step
        return round(max(self.minimum, min(self.maximum, snapped)), 1)


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


@dataclass(frozen=True)
class Options:
    debug: bool = False
    mqtt_host: str = DEFAULT_MQTT_HOST
    mqtt_port: int = DEFAULT_MQTT_PORT
    mqtt_username: str = DEFAULT_MQTT_USERNAME
    mqtt_password: str = DEFAULT_MQTT_PASSWORD
    hmpd_path: str = DEFAULT_HMPD_PATH
    controllers: tuple[Controller, ...] = field(default_factory=tuple)
    discovery_prefix: str = MQTT_DISCOVERY_PREFIX
    base_topic: str = MQTT_BASE_TOPIC
    temp_range: TempRange = field(default_factory=TempRange)


def load_raw_options(path: str = OPTIONS_PATH) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _build_controllers(raw: Any) -> tuple[Controller, ...]:
    items = raw if isinstance(raw, list) and raw else DEFAULT_CONTROLLERS
    controllers = []
    for item in items:
        controllers.append(
            Controller(
                name=str(item["name"]),
                dev=str(item["dev"]),
                baud=int(item["baud"]),
                expected_regs=int(item.get("expected_regs", 64)),
            )
        )
    return tuple(controllers)


def build_options(raw: dict[str, Any]) -> Options:
    return Options(
        debug=bool(raw.get("debug", False)),
        mqtt_host=str(raw.get("mqtt_host", DEFAULT_MQTT_HOST)),
        mqtt_port=int(raw.get("mqtt_port", DEFAULT_MQTT_PORT)),
        mqtt_username=str(raw.get("mqtt_username", DEFAULT_MQTT_USERNAME)),
        mqtt_password=str(raw.get("mqtt_password", DEFAULT_MQTT_PASSWORD)),
        hmpd_path=str(raw.get("hmpd_path", DEFAULT_HMPD_PATH)),
        controllers=_build_controllers(raw.get("controllers")),
    )


def load_options(path: str = OPTIONS_PATH) -> Options:
    return build_options(load_raw_options(path))
