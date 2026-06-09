# HMPD Home Assistant Add-on

This repository contains a Home Assistant add-on that exposes HMPD thermostat zones to Home Assistant through MQTT discovery.

## v3.0.12 — Minimal Thermostat Export

This release trims advanced features and focuses on a single responsibility: reliably exporting HMPD thermostats as MQTT-discovered Home Assistant `climate` entities.

Removed features:
- Calendar/booking sync and booking-related MQTT entities
- External-sensor offsetting and external temperature sensor discovery

The add-on still includes startup cleanup logic to remove legacy retained discovery/state topics for removed entities so old retained topics don't linger in MQTT.

## Install

1. Create a new GitHub repository and upload this folder.
2. Edit `repository.yaml` and replace placeholder URL/maintainer.
3. In Home Assistant: Settings → Add-ons → Add-on Store → menu → Repositories → add your repo URL → install the `HMPD Thermostat Bridge` add-on.

## Configuration

Exposed add-on options (in the UI):
- `debug`: Enable debug logging (default: false)
- `mqtt_host`: MQTT broker host (default: `core-mosquitto`)
- `mqtt_port`: MQTT broker port (default: `1883`)
- `mqtt_username`: MQTT username (optional)
- `mqtt_password`: MQTT password (optional)
- `hmpd_path`: Path to the `hmpd` binary (default: `/app/hmpd`)
- `controllers`: List of serial controllers (USB devices) with `name`, `dev`, `baud`, `expected_regs`

Hardcoded stability choices:
- MQTT discovery prefix: `homeassistant`
- MQTT base topic: `hmpd`
- Temperature range and steps: 16.0°C–32.0°C, step 1.0°C

## Behavior

- The add-on publishes Home Assistant MQTT discovery `climate` entities for each HMPD zone.
- State payloads include `current_temp`, `target_temp`, and `mode` only.
- Users set temperature via the climate entity; the add-on issues HMPD `set` commands to the controller.
- Legacy retained topics for booking/external sensors are cleaned at startup.

## Deployment Checklist

- Syntax validated: `python3 -m py_compile hmpd/app/main.py`
- Add-on metadata version bumped to `3.0.12`
- README updated to describe current behavior

## Changelog

### v3.0.12 - Minimal Thermostat Export (Jun 2026)
- Removed calendar/booking and external-sensor syncing — export thermostats only
- Clean up legacy retained booking/external topics on startup
- Bumped add-on metadata version for release

### v3.0.11 - Offset Hysteresis Tune-Up (May 2026)
- Added a 0.5°C hysteresis band to the external-sensor offset decision
- Prevents tiny temperature jitter from flipping the offset direction around the setpoint


## Support

- Debug logs: enable `debug: true` in add-on options → `/config/hmpd_bridge.log`
- MQTT: verify the broker host/port and credentials are correct
- HMPD binary: ensure `hmpd_path` points to an executable
