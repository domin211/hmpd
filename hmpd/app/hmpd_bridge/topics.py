"""MQTT topic naming and Home Assistant MQTT-discovery payloads.

Topic names and payload shape are a hard external contract: Home Assistant already
has `climate` entities registered under these names for real rooms. Do not rename.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import Controller, TempRange
from .models import Zone


def zone_unique_id(controller_key: str, zone_index: int) -> str:
    return f"{controller_key}_{int(zone_index)}"


@dataclass(frozen=True)
class Topics:
    discovery_prefix: str
    base_topic: str

    def state(self, unique_id: str) -> str:
        return f"{self.base_topic}/{unique_id}/state"

    def command(self, unique_id: str) -> str:
        return f"{self.base_topic}/{unique_id}/set_target"

    def discovery(self, unique_id: str) -> str:
        return f"{self.discovery_prefix}/climate/hmpd_{unique_id}/config"

    def bridge_status(self) -> str:
        return f"{self.base_topic}/bridge/status"

    def bridge_resync(self) -> str:
        return f"{self.base_topic}/bridge/resync"

    def set_target_subscription(self) -> str:
        return f"{self.base_topic}/+/set_target"

    def discovery_payload(self, zone: Zone, temp_range: TempRange) -> dict[str, Any]:
        state_topic = self.state(zone.unique_id)
        object_id = f"hmpd_{zone.unique_id}"
        return {
            "name": zone.zone_name,
            "object_id": object_id,
            "unique_id": object_id,
            "availability_topic": self.bridge_status(),
            "payload_available": "online",
            "payload_not_available": "offline",
            "current_temperature_topic": state_topic,
            "current_temperature_template": "{{ value_json.current_temp }}",
            "temperature_state_topic": state_topic,
            "temperature_state_template": "{{ value_json.target_temp }}",
            "temperature_command_topic": self.command(zone.unique_id),
            "mode_state_topic": state_topic,
            "mode_state_template": "{{ value_json.mode }}",
            "modes": ["off", "heat"],
            "min_temp": temp_range.minimum,
            "max_temp": temp_range.maximum,
            "temp_step": temp_range.step,
            "device": {
                "identifiers": [f"hmpd_{zone.unique_id}"],
                "name": zone.zone_name,
                "manufacturer": "HMPD",
                "model": "Thermostat Regulator",
                "via_device": f"hmpd_bridge_{zone.controller_key}",
            },
            "suggested_area": zone.controller_name,
        }

    def state_payload(self, zone: Zone, temp_range: TempRange, mode: str) -> dict[str, Any]:
        current_temp = zone.current_temp if zone.current_temp is not None else temp_range.minimum
        target_temp = zone.target_temp if zone.target_temp is not None else temp_range.minimum
        return {
            "current_temp": current_temp,
            "target_temp": target_temp,
            "mode": mode,
        }


def controller_log_label(controller: Controller) -> str:
    return f"{controller.name} ({controller.dev} @ {controller.baud}, key={controller.key})"
