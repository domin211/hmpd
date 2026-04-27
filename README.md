# HMPD Home Assistant Add-ons

This repository contains a Home Assistant add-on that exposes HMPD thermostat zones to Home Assistant through MQTT discovery.

## Install

1. Create a new GitHub repository.
2. Upload the contents of this folder.
3. Edit `repository.yaml` and replace the placeholder URL and maintainer.
4. In Home Assistant, open **Settings -> Add-ons -> Add-on Store -> menu -> Repositories**.
5. Add your GitHub repository URL.
6. Install the **HMPD Thermostat Bridge** add-on.

## Notes

- Put your `hmpd` binary next to `configuration.yaml` on the Home Assistant host, so the add-on can read it as `/homeassistant/hmpd`.
- The add-on publishes MQTT discovery `climate` entities.


## External temperature sensors

The add-on can now use an existing Home Assistant sensor as the room temperature source for a zone.
If no external sensor is configured for a zone, it keeps using the built-in HMPD reading.

When an external sensor is configured, the add-on also offsets the controller setpoint in the backend so the physical HMPD regulator still stops heating at the right time even though Home Assistant is showing the external room temperature.

Add entries under `external_temp_sensors` in the add-on options:

```yaml
external_sensor_target_offset: 2.0

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
- `external_sensor_target_offset` is configurable in add-on options (default `2.0`).
- The add-on calculates an internal controller target from the difference between the built-in HMPD probe and the external HA sensor, then applies the configured offset directionally: `+offset` when room temperature is below target and `-offset` when room temperature is already at/above target.
- That internal controller target is re-synced through the existing queue system with a cooldown of `auto_offset_cooldown_seconds` (default `60`).
- If the external sensor becomes unavailable, the add-on falls back to the built-in reading and stops applying offset corrections until the external value is usable again.


## Booking calendar mode (on/off)

You can now assign a Home Assistant on/off entity (for example a `calendar.*` or `input_boolean.*`) to each zone.
The add-on reads that state and automatically applies:

- booking ON target temperature (default `23.0`)
- booking OFF target temperature (default `16.0`)

Configure global defaults and optional per-zone booking entity mappings:

```yaml
booking_sync_interval: 60
booking_on_temp_default: 23.0
booking_off_temp_default: 16.0

booking_status_entities:
  - controller: usb0
    zone: 1
    entity_id: calendar.room_101
  - controller: usb0
    zone: 2
    entity_id: input_boolean.room_102_booked
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
