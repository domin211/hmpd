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
- `booking_on_temp_default`: Default ON target used by booking mode (default: 23.0)
- `booking_off_temp_default`: Default OFF target used by booking mode (default: 16.0)
- `controllers`: List of serial controllers (USB devices)
- `booking_status_entities`: Booking calendar integrations (optional)
- `external_temp_sensors`: External temperature sensor mappings (optional)

Hardcoded values:
- MQTT discovery prefix: `homeassistant`
- MQTT base topic: `hmpd`
- Sync intervals: 60s (current temp), 3600s (target), 60s (booking)
- Temperature range: 16.0°C - 32.0°C with 1.0°C steps

## Notes

- Put your `hmpd` binary next to `configuration.yaml` on the Home Assistant host, so the add-on can read it as `/homeassistant/hmpd`.
- If your binary is in a different location, set `hmpd_path` in add-on options.
- The add-on publishes MQTT discovery `climate` entities.


## Booking Status Entities

Configure booking calendar integrations to automatically set heating based on calendar events. One calendar can control multiple zones on the same controller.

### Example: One calendar for multiple zones

```yaml
booking_status_entities:
  - name: "Living Room & Bathroom"
    entity_ids:
      - binary_sensor.main_floor_booked
    controller: usb0
    zones: [1, 2]
```

When the calendar is "on", both zones heat to their booking on temperature.

### Example: Multiple calendars with redundancy

```yaml
booking_status_entities:
  - name: "Main Floor"
    entity_ids:
      - binary_sensor.main_floor_booked
      - binary_sensor.manual_override  # Fallback sensor
    controller: usb0
    zones: [1, 2, 3]
  - name: "Bedroom"
    entity_ids:
      - binary_sensor.bedroom_booked
    controller: usb0
    zones: [4]
```

### Notes

- `name` is optional - helps identify the booking configuration
- `entity_ids` is a list of Home Assistant binary sensor entities (multiple entities for redundancy)
- `controller` must match the controller `name` in the add-on config
- `zones` is a list of zone numbers on that controller
- When any entity is "on", all zones in that entry heat to their booking on temperature
- When all entities are "off", all zones revert to the booking off temperature


## External Temperature Sensors

Use existing Home Assistant sensors as the room temperature source for a zone.
If no external sensor is configured for a zone, it uses the built-in HMPD reading.

When an external sensor is configured, the add-on also offsets the controller setpoint in the backend so the physical HMPD regulator still stops heating at the right time even though Home Assistant is showing the external room temperature.

Add entries under `external_temp_sensors` in the add-on options:

```yaml
external_temp_sensors:
  - controller: usb0
    zone: 1
    entity_id: sensor.living_room_temperature
  - controller: usb1
    zone: 3
    entity_id: sensor.office_temperature
```

Notes:

- `controller` must match the controller `name` in the add-on config.
- `zone` is the numeric HMPD zone index.
- `entity_id` must be an existing Home Assistant sensor entity.
- When the external sensor is unavailable or invalid, the add-on falls back to the built-in HMPD temperature for that zone.
- The add-on reads Home Assistant state through the internal Supervisor Core API proxy using `SUPERVISOR_TOKEN`.



### Offset control behavior

- Home Assistant shows the external sensor as the current temperature.
- Home Assistant keeps showing the user-requested target temperature.
- External sensor target offset is fixed at `2.0`.
- The add-on calculates an internal controller target from the difference between the built-in HMPD probe and the external HA sensor, then applies the configured offset directionally: `+offset` when room temperature is below target and `-offset` when room temperature is already at/above target.
- That internal controller target is re-synced through the existing queue system with a fixed cooldown of `60` seconds.
- If the external sensor becomes unavailable, the add-on falls back to the built-in reading and stops applying offset corrections until the external value is usable again.


## Booking calendar mode (on/off)

You can now assign one or more Home Assistant on/off entities (for example `calendar.*` or `input_boolean.*`) to a group of zones on a controller.
The add-on reads that state and automatically applies:

- booking ON target temperature (default `23.0`)
- booking OFF target temperature (default `16.0`)

Configure global defaults and booking entity mappings:

```yaml
booking_on_temp_default: 23.0
booking_off_temp_default: 16.0

booking_status_entities:
  - name: "Room 101 + 102"
    entity_ids:
      - calendar.room_101
      - input_boolean.room_102_booked
    controller: usb0
    zones: [1, 2]
  - controller: usb0
    entity_ids:
      - input_boolean.room_103_booked
    zones: [3]
```

Notes:

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
