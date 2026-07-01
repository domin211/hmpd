# HMPD Home Assistant Add-on

This repository contains a Home Assistant add-on that exposes HMPD thermostat zones
to Home Assistant through MQTT discovery.

## v4.0.0 — Rewrite

The add-on was rewritten from a single 2,400-line script into a small, tested
`hmpd_bridge` package. Behavior is unchanged: the MQTT topics, discovery payloads,
temperature range/step, and the `hmpd` binary invocation are identical to before.

The rewrite also removed a large amount of dead code left over from the v3.0.12
"minimal export" release: booking/calendar sync, external-sensor offsetting, and the
Home Assistant Supervisor API client were never actually wired up to anything (no
MQTT subscription routed to them, and their config options had already been removed
from `config.yaml`), so none of it ever ran. It has now been deleted along with the
per-restart legacy-topic migration sweep, which was one-time tooling for the v3.0.12
transition and has already served its purpose.

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
- Publishing `bridge/resync` on the base MQTT topic forces a full re-sync (clears cached
  zones and re-polls `temps`/`regs` on every controller).

## Code layout

```
hmpd/app/
  main.py            # entrypoint: logging setup, load options, run the bridge
  hmpd_bridge/
    config.py         # options.json loading, Controller/TempRange
    models.py          # Zone / ControllerJob
    topics.py          # MQTT topic names + discovery/state payloads
    hmpd_cli.py        # locate + invoke the hmpd binary, parse its output
    queue.py            # per-controller job queue, retry/backoff
    bridge.py            # MQTT wiring, sync scheduler, zone state
  tests/                 # pytest unit tests for everything above
```

## Development

```
cd hmpd/app
pip install -r requirements-dev.txt
ruff check .
mypy
pytest
```

None of the above can exercise the real serial hardware or a live MQTT broker — those
are integration-tested by running the add-on for real. After any change, watch the
add-on log (`debug: true` → `/config/hmpd_bridge.log`) through one full sync cycle
before trusting it.

## Support

- Debug logs: enable `debug: true` in add-on options → `/config/hmpd_bridge.log`
- MQTT: verify the broker host/port and credentials are correct
- HMPD binary: ensure `hmpd_path` points to an executable
