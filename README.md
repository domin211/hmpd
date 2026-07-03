# HMPD Home Assistant Add-on

This repository contains a Home Assistant add-on that exposes HMPD thermostat zones
to Home Assistant through MQTT discovery.

## v4.1.0 — Rewrite + diagnostics

The add-on was rewritten from a single 2,400-line script into a small, tested
`hmpd_bridge` package. Behavior is unchanged: the MQTT topics, discovery payloads,
temperature range/step, and the `hmpd` binary invocation are identical to before.
See the [Changelog](#changelog) below for the full list of what changed.

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

## Changelog

### v4.1.1 - Revert Alpine base image (Jul 2026)
- Reverted the Alpine base image from 3.24 back to 3.20. The `hmpd` binary is
  glibc-linked and runs on Alpine through a compatibility shim
  (`libc6-compat`/`gcompat`); the 3.24 bump is suspected of shifting timing for the
  binary's serial-bus polling thread, causing previously-reliable zones (several
  real rooms plus pool/sauna/kitchen/etc.) to intermittently return garbage
  current-temperature readings. Reverting until this is confirmed/ruled out.

### v4.1.0 - Diagnostics (Jul 2026)
- Restored per-register debug logging (dropped during the v4.0.0 rewrite) that shows
  why a named zone has no valid current-temperature reading
- Added a warning that lists the specific zone `index:name` pairs missing a reading,
  not just a count
- Added a log of the complete raw `temps`/`regs` output per controller at INFO level,
  so the exact controller response is always visible without digging through debug noise

### v4.0.0 - Rewrite (Jul 2026)
- Replaced the single 2,400-line `main.py` with a small, tested `hmpd_bridge` package
  (`config`/`models`/`topics`/`hmpd_cli`/`queue`/`bridge` modules)
- Removed ~900 lines of dead code left over from the v3.0.12 "minimal export" release:
  booking/calendar sync, external-sensor offsetting, and the Home Assistant Supervisor
  API client were never wired to any MQTT subscription or scheduler call, so none of it
  ever ran
- Removed the per-restart legacy-topic migration sweep (one-time v3.0.12 transition
  tooling that had already served its purpose)
- Removed the now-unused `homeassistant_api: true` permission
- Bumped the Alpine base image from 3.20 (past end-of-support) to 3.24
- Fixed CRLF line endings that broke `/app/run.sh` inside the container; added
  `.gitattributes` so a Windows checkout can't reintroduce this
- Added pytest unit tests (31), ruff/mypy config, GitHub Actions CI, and Dependabot
- MQTT topic names, discovery payload shape, and the `hmpd` binary CLI contract are
  unchanged from pre-rewrite behavior

### v3.0.12 - Minimal Thermostat Export (Jun 2026)
- Removed calendar/booking and external-sensor syncing from the exposed config options
  (the underlying code wasn't actually deleted until v4.0.0 — see above)
- Clean up legacy retained booking/external topics on startup

### v3.0.11 - Offset Hysteresis Tune-Up (May 2026)
- Added a 0.5°C hysteresis band to the external-sensor offset decision (this logic was
  dead code, never reachable at runtime, and was removed in v4.0.0)

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
