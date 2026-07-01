"""Orchestrator: MQTT wiring, the sync scheduler, and zone state.

This only reimplements what is actually reachable in the add-on today: connecting to
MQTT, periodically polling `temps`/`regs` per controller, publishing Home Assistant
`climate` discovery + state, and handling `set_target`/`bridge/resync` commands.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time

import paho.mqtt.client as mqtt

from . import hmpd_cli
from .config import (
    COMMAND_GAP_SECONDS,
    CURRENT_TEMP_SYNC_INTERVAL,
    HMPD_FIND_RETRY_SECONDS,
    MAX_COMMAND_ATTEMPTS,
    MQTT_CLIENT_ID,
    MQTT_KEEPALIVE,
    MQTT_RETRY_SECONDS,
    OFF_MODE_TARGET_THRESHOLD,
    REGS_TIMEOUT,
    RETAIN_DISCOVERY,
    RETAIN_STATE,
    RETRY_DELAYS_SECONDS,
    SET_TIMEOUT,
    TARGET_SYNC_INTERVAL,
    TEMPS_TIMEOUT,
    Controller,
    Options,
)
from .models import ControllerJob, Zone
from .queue import ControllerQueue
from .topics import Topics, zone_unique_id

log = logging.getLogger("hmpd_bridge.bridge")

_SET_TARGET_RE_TEMPLATE = r"^{base}/([^/]+)/set_target$"


class HMPDBridge:
    def __init__(self, options: Options) -> None:
        self.options = options
        self.topics = Topics(options.discovery_prefix, options.base_topic)
        self.temp_range = options.temp_range

        self.shutdown_event = threading.Event()
        self.mqtt_connected = False
        self.hmpd_path = ""

        self.controllers: list[Controller] = list(options.controllers)
        self.controllers_by_key = {c.key: c for c in self.controllers}

        self.zones: dict[str, Zone] = {}
        self.latest_temps: dict[str, dict[int, float]] = {}
        self.next_temp_sync_at: dict[str, float] = {c.key: 0.0 for c in self.controllers}
        self.next_target_sync_at: dict[str, float] = {c.key: 0.0 for c in self.controllers}

        self.queues: dict[str, ControllerQueue] = {
            c.key: ControllerQueue(
                label=c.name,
                execute=self._make_executor(c),
                command_gap_seconds=COMMAND_GAP_SECONDS,
                max_attempts=MAX_COMMAND_ATTEMPTS,
                retry_delays=RETRY_DELAYS_SECONDS,
            )
            for c in self.controllers
        }

        self.mqtt = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=MQTT_CLIENT_ID,
        )
        if options.mqtt_username:
            self.mqtt.username_pw_set(options.mqtt_username, options.mqtt_password)
        self.mqtt.on_connect = self._on_connect
        self.mqtt.on_disconnect = self._on_disconnect
        self.mqtt.on_message = self._on_message
        self.mqtt.reconnect_delay_set(min_delay=1, max_delay=30)

        self._set_target_re = re.compile(_SET_TARGET_RE_TEMPLATE.format(base=re.escape(options.base_topic)))

    # -- hmpd binary -----------------------------------------------------

    def _ensure_hmpd(self) -> str:
        import os

        if self.hmpd_path and os.path.isfile(self.hmpd_path) and os.access(self.hmpd_path, os.X_OK):
            return self.hmpd_path
        self.hmpd_path = hmpd_cli.find_hmpd(self.options.hmpd_path)
        return self.hmpd_path

    # -- controller job execution -----------------------------------------

    def _make_executor(self, controller: Controller):
        def execute(job: ControllerJob) -> None:
            if job.kind == "set":
                self._execute_set_job(controller, job)
                return

            cmd = hmpd_cli.build_hmpd_cmd(self._ensure_hmpd(), controller, job.action_args)
            lines = hmpd_cli.run_hmpd(cmd, job.timeout)

            if job.kind == "temps":
                self._validate_temps(controller, lines)
                self._apply_temps(controller, lines)
                self.next_temp_sync_at[controller.key] = time.monotonic() + CURRENT_TEMP_SYNC_INTERVAL
            elif job.kind == "regs":
                self._validate_regs(controller, lines)
                self._apply_regs(controller, lines)
                self.next_target_sync_at[controller.key] = time.monotonic() + TARGET_SYNC_INTERVAL
            else:
                raise RuntimeError(f"Unsupported queue job kind: {job.kind}")

            job.result_lines = lines

        return execute

    def _validate_temps(self, controller: Controller, lines: list[str]) -> None:
        if not hmpd_cli.parse_temps(lines, self.temp_range):
            raise RuntimeError(f"No valid temps returned for controller {controller.name}")

    def _validate_regs(self, controller: Controller, lines: list[str]) -> None:
        parsed = hmpd_cli.parse_regs(lines, self.temp_range)
        if not parsed:
            raise RuntimeError(f"No valid regs returned for controller {controller.name}")
        expected = max(1, int(controller.expected_regs))
        if len(parsed) < expected:
            raise RuntimeError(
                f"Incomplete regs output for controller {controller.name}: "
                f"got {len(parsed)}, expected at least {expected}"
            )

    def _execute_set_job(self, controller: Controller, job: ControllerJob) -> None:
        zone = self.zones.get(job.zone_unique_id or "")
        if zone is None:
            raise RuntimeError(f"Zone not found for queued set: {job.zone_unique_id}")

        target = self.temp_range.snap(float(job.target if job.target is not None else self.temp_range.minimum))
        cmd = hmpd_cli.build_hmpd_cmd(self._ensure_hmpd(), controller, ["set", str(zone.zone_index), f"{target:.1f}"])
        hmpd_cli.run_hmpd(cmd, job.timeout)

        zone.controller_target_temp = target
        if zone.target_temp is None:
            zone.target_temp = target
        self._publish_state(zone)
        log.info("Set %s (%s) target to %.1f", zone.zone_name, zone.unique_id, target)

    # -- syncing zone state from parsed output -----------------------------

    def _apply_temps(self, controller: Controller, lines: list[str]) -> None:
        temps = hmpd_cli.parse_temps(lines, self.temp_range)
        self.latest_temps[controller.key] = temps

        for idx, current_temp in temps.items():
            zone = self.zones.get(zone_unique_id(controller.key, idx))
            if zone is None:
                continue
            zone.current_temp = current_temp
            if zone.target_temp is None:
                zone.target_temp = zone.controller_target_temp
            self._publish_state(zone)

        log.info("Cached %s temperatures for controller %s", len(temps), controller.name)

    def _apply_regs(self, controller: Controller, lines: list[str]) -> None:
        parsed = hmpd_cli.parse_regs(lines, self.temp_range)
        temps = self.latest_temps.get(controller.key, {})

        created = updated = skipped_no_temp = 0
        valid_unique_ids: set[str] = set()
        skipped_zones: list[str] = []

        for idx, data in parsed.items():
            name = (data["name"] or "").strip()
            if not name:
                continue

            current_temp = temps.get(idx, data.get("current_temp"))
            if current_temp is None:
                skipped_no_temp += 1
                skipped_zones.append(f"{idx}:{name}")
                continue

            unique_id = zone_unique_id(controller.key, idx)
            valid_unique_ids.add(unique_id)
            zone = self.zones.get(unique_id)

            if zone is None:
                zone = Zone(
                    controller_name=controller.name,
                    controller_key=controller.key,
                    controller_dev=controller.dev,
                    zone_index=idx,
                    zone_name=name,
                    unique_id=unique_id,
                    current_temp=current_temp,
                    controller_target_temp=data["target_temp"],
                    target_temp=data["target_temp"],
                    enabled=data["enabled"],
                )
                self.zones[unique_id] = zone
                created += 1
            else:
                zone.zone_name = name
                zone.current_temp = current_temp
                if data["target_temp"] is not None:
                    zone.controller_target_temp = data["target_temp"]
                    if zone.target_temp is None:
                        zone.target_temp = data["target_temp"]
                zone.enabled = data["enabled"]
                updated += 1

            if zone.target_temp is None and zone.controller_target_temp is not None:
                zone.target_temp = zone.controller_target_temp

            self._publish_discovery(zone)
            self._publish_state(zone)

        stale = [
            unique_id
            for unique_id, zone in self.zones.items()
            if zone.controller_key == controller.key and unique_id not in valid_unique_ids
        ]
        for unique_id in stale:
            self._remove_zone(unique_id)

        log.info(
            "Synced regs for %s (%s created, %s updated, %s skipped_no_temp)",
            controller.name,
            created,
            updated,
            skipped_no_temp,
        )
        if skipped_zones:
            log.warning(
                "Controller %s has %s named zone(s) with no valid current temperature (no reading "
                "from temps or regs cur:): %s",
                controller.name,
                len(skipped_zones),
                ", ".join(skipped_zones),
            )

    def _remove_zone(self, unique_id: str) -> None:
        for topic in (self.topics.discovery(unique_id), self.topics.state(unique_id), self.topics.command(unique_id)):
            self.mqtt.publish(topic, "", qos=1, retain=True)
        self.zones.pop(unique_id, None)
        log.info("Removed thermostat %s", unique_id)

    # -- MQTT publishing ----------------------------------------------------

    def _publish_bridge_status(self, online: bool) -> None:
        self.mqtt.publish(self.topics.bridge_status(), "online" if online else "offline", qos=1, retain=True)

    def _publish_discovery(self, zone: Zone) -> None:
        payload = json.dumps(self.topics.discovery_payload(zone, self.temp_range), ensure_ascii=False, sort_keys=True)
        if zone.last_discovery_payload == payload and zone.discovered:
            return
        self.mqtt.publish(self.topics.discovery(zone.unique_id), payload, qos=1, retain=RETAIN_DISCOVERY)
        zone.last_discovery_payload = payload
        zone.discovered = True

    def _publish_state(self, zone: Zone) -> None:
        mode = "off" if zone.is_off_mode(OFF_MODE_TARGET_THRESHOLD) else "heat"
        payload = json.dumps(self.topics.state_payload(zone, self.temp_range, mode), sort_keys=True)
        if zone.last_state_payload == payload:
            return
        self.mqtt.publish(self.topics.state(zone.unique_id), payload, qos=1, retain=RETAIN_STATE)
        zone.last_state_payload = payload

    # -- MQTT callbacks -------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if str(reason_code) == "Success":
            self.mqtt_connected = True
            log.info("Connected to MQTT broker %s:%s", self.options.mqtt_host, self.options.mqtt_port)
            client.subscribe(self.topics.set_target_subscription())
            client.subscribe(self.topics.bridge_resync())
        else:
            self.mqtt_connected = False
            log.error("MQTT authorization/connection failed: %s", reason_code)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties) -> None:
        self.mqtt_connected = False
        log.warning("MQTT disconnected (reason=%s)", reason_code)

    def _on_message(self, client, userdata, msg) -> None:
        topic = msg.topic
        payload = msg.payload.decode("utf-8", errors="ignore").strip()
        log.debug("MQTT message: %s => %s", topic, payload)

        try:
            if topic == self.topics.bridge_resync():
                threading.Thread(target=self.force_full_republish, daemon=True).start()
                return

            match = self._set_target_re.match(topic)
            if match:
                self._handle_set_target(match.group(1), payload)
        except Exception:
            log.error("MQTT message handling failed for topic %s", topic, exc_info=True)

    def _handle_set_target(self, zone_key: str, payload: str) -> None:
        if not payload:
            return
        zone = self.zones.get(zone_key)
        if not zone:
            log.debug("Ignoring target payload for unknown zone key: %s", zone_key)
            return

        try:
            value = float(payload)
        except ValueError:
            log.warning("Invalid target payload for %s: %s", zone_key, payload)
            return

        target = self.temp_range.snap(value)
        zone.target_temp = target
        self._enqueue_set(zone, target)
        self._publish_state(zone)

    def _enqueue_set(self, zone: Zone, target: float, reason: str = "mqtt_set_target") -> None:
        queue = self.queues.get(zone.controller_key)
        if queue is None:
            log.error("Unknown controller for zone %s", zone.unique_id)
            return
        job = ControllerJob(
            kind="set", timeout=SET_TIMEOUT, zone_unique_id=zone.unique_id, target=target, reason=reason
        )
        queue.enqueue(job, wait=False)
        log.info("Queued set for %s (%s) to %.1f", zone.zone_name, zone.unique_id, target)

    # -- sync scheduling --------------------------------------------------

    def _sync_blocking(self, controller: Controller, kind: str, reason: str) -> None:
        timeout = TEMPS_TIMEOUT if kind == "temps" else REGS_TIMEOUT
        job = ControllerJob(kind=kind, action_args=[kind], timeout=timeout, reason=reason)
        self.queues[controller.key].enqueue(job, wait=True)

    def force_full_republish(self) -> None:
        self.zones = {}
        self.latest_temps = {}
        for controller in self.controllers:
            self._sync_blocking(controller, "temps", "initial_current_temp_sync")
        for controller in self.controllers:
            self._sync_blocking(controller, "regs", "initial_target_sync")

    def _mqtt_connect_loop(self) -> None:
        while not self.shutdown_event.is_set():
            try:
                log.info("Connecting to MQTT broker %s:%s", self.options.mqtt_host, self.options.mqtt_port)
                self.mqtt_connected = False
                self.mqtt.connect(self.options.mqtt_host, self.options.mqtt_port, keepalive=MQTT_KEEPALIVE)
                self.mqtt.loop_start()

                deadline = time.time() + 10
                while time.time() < deadline:
                    if self.mqtt_connected or self.shutdown_event.is_set():
                        return
                    time.sleep(0.2)

                log.error("MQTT connect timed out")
                self.mqtt.loop_stop()
                self.mqtt.disconnect()
            except Exception:
                log.error("MQTT connect failed", exc_info=True)

            for _ in range(MQTT_RETRY_SECONDS):
                if self.shutdown_event.is_set():
                    break
                time.sleep(1.0)

    def _ensure_mqtt_connected(self) -> None:
        if self.mqtt_connected:
            return
        try:
            self.mqtt.loop_stop()
        except Exception:
            pass
        self._mqtt_connect_loop()
        self._publish_bridge_status(True)

    def _health_check_workers(self) -> None:
        for controller in self.controllers:
            self.queues[controller.key].restart_if_dead()

    def _scheduler_loop(self) -> None:
        last_health_check = time.monotonic()
        while not self.shutdown_event.is_set():
            try:
                self._ensure_mqtt_connected()
                now = time.monotonic()

                if (now - last_health_check) > 60.0:
                    self._health_check_workers()
                    last_health_check = now

                for controller in self.controllers:
                    key = controller.key
                    queue = self.queues[key]

                    if now >= self.next_temp_sync_at[key] and not queue.is_kind_active_or_queued("temps"):
                        queue.enqueue(
                            ControllerJob(
                                kind="temps",
                                action_args=["temps"],
                                timeout=TEMPS_TIMEOUT,
                                reason="scheduled_current_temp_sync",
                            ),
                            wait=False,
                        )

                    if now >= self.next_target_sync_at[key] and not queue.is_kind_active_or_queued("regs"):
                        queue.enqueue(
                            ControllerJob(
                                kind="regs",
                                action_args=["regs"],
                                timeout=REGS_TIMEOUT,
                                reason="scheduled_target_sync",
                            ),
                            wait=False,
                        )

                time.sleep(1.0)
            except Exception:
                log.critical("Unhandled exception in scheduler loop, recovering...", exc_info=True)
                time.sleep(5.0)
                self.mqtt_connected = False
        log.info("Scheduler loop exiting")

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        try:
            self.hmpd_path = hmpd_cli.wait_for_hmpd(self.options.hmpd_path, HMPD_FIND_RETRY_SECONDS)
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

            for queue in self.queues.values():
                queue.start()

            self._mqtt_connect_loop()
            self._publish_bridge_status(True)

            try:
                self.force_full_republish()
            except Exception:
                log.error("Initial republish failed (will retry on next sync)", exc_info=True)

            now = time.monotonic()
            for controller in self.controllers:
                self.next_temp_sync_at[controller.key] = now + CURRENT_TEMP_SYNC_INTERVAL
                self.next_target_sync_at[controller.key] = now + TARGET_SYNC_INTERVAL

            self._scheduler_loop()
        finally:
            log.info("Shutting down HMPD Bridge")
            self.shutdown_event.set()
            for queue in self.queues.values():
                queue.stop()

            self._publish_bridge_status(False)
            try:
                self.mqtt.loop_stop()
                self.mqtt.disconnect()
            except Exception:
                pass
            log.info("HMPD Bridge shutdown complete")
