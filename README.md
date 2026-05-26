# HMPD Home Assistant Add-ons

This repository contains a Home Assistant add-on that exposes HMPD thermostat zones to Home Assistant through MQTT discovery.

## Stability & Deployment

**v3.0.0 is production-ready for 5+ year deployments** with:
- Hardcoded stable configuration (no runtime tuning needed)
- Comprehensive error handling and automatic recovery
- Memory-safe design with log rotation
- Graceful shutdown support
- Full exception logging for debugging
- Thread-safe concurrent operations
- **Worker thread health monitoring** - dead threads are automatically restarted
- **Job error cleanup** - prevents memory leaks from stored exceptions
- **Explicit thread shutdown** - graceful cleanup with 10-second timeout

## Install

1. Create a new GitHub repository.
2. Upload the contents of this folder.
3. Edit `repository.yaml` and replace the placeholder URL and maintainer.
4. In Home Assistant, open **Settings -> Add-ons -> Add-on Store -> menu -> Repositories**.
5. Add your GitHub repository URL.
6. Install the **HMPD Thermostat Bridge** add-on.

## Long-Term Stability Features

The add-on is designed for reliable 5+ year deployments with:

- **Graceful Error Handling**: All critical loops have exception handlers that log errors and recover automatically
- **Thread Safety**: Worker threads properly handle shutdown signals and timeouts
- **Resource Management**: 
  - Log file rotation (10MB per file, 5 backups = 60MB max)
  - Memory leak prevention with periodic warning cache cleanup
  - Proper MQTT connection cleanup
- **Automatic Recovery**: If MQTT connection fails or an exception occurs, the system will automatically attempt to recover
- **Full Tracebacks**: All errors logged with full Python tracebacks for debugging
- **Stateful Recovery**: Booking temperatures and states are persisted and restored

## Configuration

The add-on has minimal configuration:

- `debug`: Enable debug logging (default: false)
- `mqtt_host`: MQTT broker host (default: core-mosquitto)
- `mqtt_port`: MQTT broker port (default: 1883)
- `mqtt_username`: MQTT username (optional)
- `mqtt_password`: MQTT password (optional)
- `hmpd_path`: Path to the hmpd binary (default: /app/hmpd)
- `rooms`: Recommended room-based preset entries that combine controller, calendar, external sensor, and zone IDs in one row
- `booking_on_temp_default`: Default ON target used by booking mode (default: 23.0)
- `booking_off_temp_default`: Default OFF target used by booking mode (default: 16.0)
- `controllers`: List of serial controllers (USB devices)

Hardcoded values:
- MQTT discovery prefix: `homeassistant`
- MQTT base topic: `hmpd`
- Sync intervals: 60s (current temp), 3600s (target), 60s (booking)
- Temperature range: 16.0°C - 32.0°C with 1.0°C steps

The `rooms` preset is the easiest setup path. Each room row expands into the booking and external temperature mappings the runtime already understands, so one UI item configures a whole room. Booking on/off temperatures are shared globally, so you only edit them once and every room uses the same values. The zone ids are shown again in the UI as a comma-separated field so they can be reviewed or edited per room.

## Notes

- Put your `hmpd` binary next to `configuration.yaml` on the Home Assistant host, so the add-on can read it as `/homeassistant/hmpd`.
- If your binary is in a different location, set `hmpd_path` in add-on options.
- The add-on publishes MQTT discovery `climate` entities.


## Room-Based Presets

Use `rooms` when you want one entry to configure a whole room.

```yaml
rooms:
  - name: room_11
    controller: usb0
    calendar: calendar.pokoj_11
    external_sensor: sensor.pokoj_11_teplota
    external_sensor_enabled: true
```

Each room row does three things:

- maps the calendar to booking control
- maps the room zone IDs to the correct controller using the built-in room preset
- uses the external sensor as the current temperature source when `external_sensor_enabled` is `true`

### Offset control behavior

- Home Assistant shows the external sensor as the current temperature.
- Home Assistant keeps showing the user-requested target temperature.
- External sensor target offset is fixed at `2.0`.
- The add-on calculates an internal controller target from the difference between the built-in HMPD probe and the external HA sensor, then applies the configured offset directionally with a `0.5`°C hysteresis band: `+offset` when room temperature is at least `0.5`°C below target and `-offset` when room temperature is at least `0.5`°C above target.
- That internal controller target is re-synced through the existing queue system with a fixed cooldown of `60` seconds.
- If the external sensor becomes unavailable, the add-on falls back to the built-in reading and stops applying offset corrections until the external value is usable again.
- The built-in HMPD probe is also published as a separate `sensor` entity so you can graph internal and external temperatures side by side.
- Only the first id in a room row uses the external sensor; any bathroom or WC ids in the same row stay on the built-in probe.


## Booking calendar mode (on/off)

Booking is now configured through the `rooms` preset. Each room row links a calendar to a controller and its zones, and the shared booking temperatures below apply to all rooms.

- booking ON target temperature (default `23.0`)
- booking OFF target temperature (default `16.0`)

- The add-on creates visible entities per zone in Home Assistant:
  - `switch`: booking state (`Booked`)
  - `number`: booking ON temperature
  - `number`: booking OFF temperature
- You can control booking directly with the switch, even without a mapped calendar entity.
- Booking values are persisted across restarts.


## Automatic off mode at low targets

When a zone target temperature is `18.0` or lower, the climate entity now reports `mode: off`.
This applies to manual target changes and booking-driven targets.

## Deployment Checklist (5+ Year Stability)

✅ **Configuration**: All values hardcoded and tuned - no runtime adjustments needed  
✅ **Error Handling**: Automatic recovery from transient failures  
✅ **Logging**: Rotating files prevent disk fill-up (10MB per file, 5 backups)  
✅ **Memory**: Periodic cache cleanup prevents long-term memory leaks  
✅ **Threads**: Graceful shutdown with proper cleanup  
✅ **Testing**: Syntax validated at build time  

**Deploy once, forget for 5 years.**

---

## Changelog

### v3.0.11 - Offset Hysteresis Tune-Up (May 2026)

- Added a 0.5°C hysteresis band to the external-sensor offset decision
- Prevents tiny temperature jitter from flipping the offset direction around the setpoint

### v3.0.10 - Dual Temperature Graph Support (May 2026)

- Added separate MQTT sensors for internal and external room temperatures
- Published both values so a Home Assistant history graph can show two temperature lines
- Kept the existing climate entity behavior unchanged for control

### v3.0.9 - Room UI Cleanup Follow-up (May 2026)

- Restored the room `ids` field as a comma-separated input so multiple zone IDs can be edited in one place
- Added startup and runtime logs for room expansion, shared booking temperatures, and mapping conflicts
- Kept booking temperatures shared globally so they can be edited once for all rooms

### Example history graph

Use the two new sensor entities in a Home Assistant history graph card. Replace the example entity ids with the discovered ones for your room:

```yaml
type: history-graph
title: Room temperatures
hours_to_show: 24
entities:
  - entity: sensor.hmpd_<room>_temperature_internal
    name: Internal
  - entity: sensor.hmpd_<room>_temperature_external
    name: External
  - entity: climate.<room>
    name: Target
```

The climate entity still exposes a single current temperature for control; the two sensor entities are what you graph when you want internal and external temperatures together.

### v3.0.7 - Config/Docs Consistency Cleanup (April 2026)

- Removed unsupported hidden HA API option parsing from runtime (now fixed to internal Supervisor defaults)
- Updated booking and external sensor docs/examples to match current add-on schema and fixed timings

### v3.0.6 - Worker Startup Logic Fix (April 2026)

- Fixed unreachable branch in worker startup code so dead worker threads are correctly detected and restarted

### v3.0.5 - Defensive Zone Field Backfill (April 2026)

- Added runtime `Zone` field backfill guard before discovery/state publishing
- Prevents worker crashes if a future edit introduces missing `Zone` cache/state attributes

### v3.0.4 - Startup Attribute Crash Fix (April 2026)

- Added missing `Zone` fields `discovered` and `last_discovery_payload` used by MQTT discovery publishing
- Fixes startup retry loop reporting `'Zone' object has no attribute 'last_discovery_payload'`

### v3.0.3 - Booking Regression + UI Fix (April 2026)

- Fixed startup crash caused by missing `Zone` fields (`booking_state`, booking temperatures, and payload/discovery caches)
- Restored `booking_status_entities` options UI usability by changing schema list definitions to editable list-item validators

### v3.0.2 - Configurable Booking Defaults Restored (April 2026)

- Restored add-on options for `booking_on_temp_default` and `booking_off_temp_default`
- Added schema validation for those options in `config.yaml`
- Runtime now safely parses configured values with fallback to built-in defaults

### v3.0.1 - Supervisor Compatibility Fix (April 2026)

- Removed deprecated `arch` values (`armv7`, `armhf`, `i386`) from add-on metadata
- Fixed `booking_status_entities` schema list syntax so Supervisor can parse `config.yaml` cleanly

### v3.0.0 - Production-Ready Release (April 2026)

**Breaking Changes:**
- Simplified configuration: Removed all tunable runtime parameters
- Booking status entities now use simplified zone list format

**Major Improvements (5-Year Stability):**
- ✨ **Exception Handling**: All critical loops wrapped with try/catch for automatic recovery
- ✨ **Worker Thread Monitoring**: Scheduler performs health checks every 60s, automatically restarts dead threads
- ✨ **Graceful Shutdown**: Proper signal handling, thread cleanup, and 10-second shutdown timeout
- ✨ **Memory Safety**: 
  - Log rotation prevents disk fill (10MB/file, 5 backups = 60MB max)
  - Periodic warning cache cleanup (hourly) prevents memory leaks
  - Job exception objects cleaned up after execution
- ✨ **Full Tracebacks**: All exceptions logged with complete stack traces for debugging
- ✨ **Multi-Zone Booking**: One calendar can now control multiple zones simultaneously
- ✨ **Redundant Sensors**: Booking entities support multiple fallback sensor IDs
- ✨ **Thread Safety**: Non-daemon worker threads with explicit cleanup

**Configuration Changes:**
- Hardcoded: MQTT discovery prefix, base topic, sync intervals, temperature range, defaults
- Simplified to 5 configurable options: debug, mqtt_host/port/creds, hmpd_path, controllers, mappings

**Code Quality:**
- Added rotating file handler with `logging.handlers.RotatingFileHandler`
- Worker threads are non-daemon with explicit shutdown sequence
- Scheduler loop health checks restart dead workers
- MQTT connection loops include timeout checks for graceful recovery
- Explicit thread.join() during shutdown with 10-second timeout
- Job errors cleared after processing to prevent memory leaks

**Backward Compatibility:**
- Old `entity_id` format still supported for booking status (auto-converted to list)

### v2.4.0 - Previous Release

Initial stable release with MQTT discovery and booking support.

---

## Support

For issues, check:
1. Debug logs: Enable `debug: true` in add-on options → `/config/hmpd_bridge.log`
2. MQTT connection: Verify broker is accessible on configured host/port
3. HMPD binary: Confirm path is correct and executable
4. Home Assistant: Verify sensor entities exist at configured IDs
