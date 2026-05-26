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

- For local development you can run the main module directly from the workspace root:

  python -u hmpd/app/main.py

Where to look (important files)
-------------------------------
- **Add-on metadata & options:** [hmpd/config.yaml](hmpd/config.yaml)
- **Container build:** [hmpd/Dockerfile](hmpd/Dockerfile)
- **App entrypoint / runner:** [hmpd/app/run.sh](hmpd/app/run.sh)
- **Application logic:** [hmpd/app/main.py](hmpd/app/main.py)
- **Python deps:** [hmpd/app/requirements.txt](hmpd/app/requirements.txt)
- **Repository README:** [README.md](README.md)

Key conventions & notes for agents
--------------------------------
- Runtime options are supplied via the add-on `options` (see `hmpd/config.yaml`) and are read at `/data/options.json` inside the container.
- The add-on expects an `hmpd` binary to live at `/app/hmpd` in the container by default; the `hmpd_path` option overrides this.
- Logging: enable debug via `debug: true` in the add-on options to get rotating file logs at `/config/hmpd_bridge.log`.
- MQTT: default broker host is `core-mosquitto`; config lives in `options` (see `config.yaml`).
- The Dockerfile creates a Python venv and installs `-r /app/requirements.txt`.

What agents should do first
---------------------------
1. Inspect `hmpd/config.yaml` for configuration schema and default values.
2. Use the Dockerfile if you need a reproducible runtime for testing.
3. For quick checks, run `python -u hmpd/app/main.py` locally in a virtualenv.

Where to ask for help
---------------------
If behavior or configuration is unclear, check the repository `README.md` or open an issue with reproduction steps and current add-on `options` used.

Minimalness rule
-----------------
Only include material here that an AI agent cannot discover quickly by reading the referenced files. Link to other documentation rather than copying it.
