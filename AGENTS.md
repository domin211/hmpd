AGENTS.md — AI agent instructions for this repository
===============================================

Purpose
-------
This file gives AI coding agents the minimal, high-value information needed to be productive in this repository: how to run the app, where key files live, and a few project conventions.

Quick start
-----------
- Build the add-on Docker image (recommended):

  docker build -t hmpd-add-on hmpd

- The container runs `/app/run.sh`, which launches the Python process and restarts it on exit. The runtime command is:

  python3 -u /app/main.py

- For local development, run the app directly from `hmpd/app`:

  python -u main.py

- Lint, type-check, and test (from `hmpd/app`):

  pip install -r requirements-dev.txt
  ruff check .
  mypy
  pytest

Where to look (important files)
-------------------------------
- **Add-on metadata & options:** [hmpd/config.yaml](hmpd/config.yaml)
- **Container build:** [hmpd/Dockerfile](hmpd/Dockerfile)
- **App entrypoint / runner:** [hmpd/app/run.sh](hmpd/app/run.sh)
- **Entrypoint:** [hmpd/app/main.py](hmpd/app/main.py)
- **Application package:** `hmpd/app/hmpd_bridge/` — `config.py` (options), `models.py` (Zone/ControllerJob), `topics.py` (MQTT topic names + discovery payloads), `hmpd_cli.py` (binary discovery + output parsing), `queue.py` (per-controller retry/backoff queue), `bridge.py` (orchestrator)
- **Tests:** `hmpd/app/tests/`
- **Python deps:** [hmpd/app/requirements.txt](hmpd/app/requirements.txt) (runtime), [hmpd/app/requirements-dev.txt](hmpd/app/requirements-dev.txt) (lint/type/test tools, not shipped in the image)
- **Repository README:** [README.md](README.md)

Key conventions & notes for agents
--------------------------------
- Runtime options are supplied via the add-on `options` (see `hmpd/config.yaml`) and are read at `/data/options.json` inside the container.
- The add-on expects an `hmpd` binary to live at `/app/hmpd` in the container by default; the `hmpd_path` option overrides this.
- Logging: enable debug via `debug: true` in the add-on options to get rotating file logs at `/config/hmpd_bridge.log`.
- MQTT: default broker host is `core-mosquitto`; config lives in `options` (see `config.yaml`).
- MQTT topic names, discovery payload shape, and the `hmpd` binary's CLI/output format are fixed external contracts — Home Assistant already has entities registered under the existing topic names, and the binary's CLI isn't owned by this repo. Don't rename/reshape them without a real migration plan.
- No tests can exercise real serial hardware or a live MQTT broker; `pytest` only covers pure/deterministic logic. Treat a green test run as necessary, not sufficient — verify against the real add-on for anything touching hardware I/O or MQTT wiring.

What agents should do first
---------------------------
1. Inspect `hmpd/config.yaml` for configuration schema and default values.
2. Run `ruff check .`, `mypy`, and `pytest` from `hmpd/app` before and after changes.
3. Use the Dockerfile if you need a reproducible runtime for testing.

Where to ask for help
---------------------
If behavior or configuration is unclear, check the repository `README.md` or open an issue with reproduction steps and current add-on `options` used.

Minimalness rule
-----------------
Only include material here that an AI agent cannot discover quickly by reading the referenced files. Link to other documentation rather than copying it.
