"""In-memory state: a thermostat Zone and a queued ControllerJob."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass
class Zone:
    controller_name: str
    controller_key: str
    controller_dev: str
    zone_index: int
    zone_name: str
    unique_id: str
    current_temp: float | None = None
    controller_target_temp: float | None = None
    target_temp: float | None = None
    enabled: bool | None = None
    discovered: bool = False
    last_discovery_payload: str | None = None
    last_state_payload: str | None = None

    def is_off_mode(self, off_threshold: float) -> bool:
        return self.target_temp is not None and self.target_temp <= off_threshold


@dataclass
class ControllerJob:
    kind: str  # "temps" | "regs" | "set"
    action_args: list[str] = field(default_factory=list)
    timeout: int = 0
    zone_unique_id: str | None = None
    target: float | None = None
    reason: str = ""
    done: threading.Event = field(default_factory=threading.Event)
    result_lines: list[str] = field(default_factory=list)
    error: BaseException | None = None
