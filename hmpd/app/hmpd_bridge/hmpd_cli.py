"""Locating and invoking the external `hmpd` binary, and parsing its output.

The binary's CLI (--dev, --baud, `temps`, `regs`, `set <idx> <val>`) and its output
line formats (`idx: temp`, `idx | name | cur: X | tgt: Y | EN`) are a fixed external
contract owned by that separate compiled program, not by this codebase.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time

from .config import Controller, TempRange

log = logging.getLogger("hmpd_bridge.hmpd_cli")


def hmpd_candidates(configured_path: str) -> list[str]:
    candidates = [
        configured_path,
        "/homeassistant/hmpd",
        "/homeassistant/hmpd/hmpd",
        "/homeassistant/config/hmpd",
        "/homeassistant/config/hmpd/hmpd",
        "/config/hmpd",
        "/config/hmpd/hmpd",
        "/config/bin/hmpd",
        "/ha_config/hmpd",
        "/ha_config/hmpd/hmpd",
        "/share/hmpd",
        "/share/hmpd/hmpd",
        "/app/hmpd",
        "/app/hmpd/hmpd",
        "./hmpd",
    ]
    seen: set[str] = set()
    ordered: list[str] = []
    for path in candidates:
        if path and path not in seen:
            ordered.append(path)
            seen.add(path)
    return ordered


def find_hmpd(configured_path: str) -> str:
    checked: list[str] = []
    for candidate in hmpd_candidates(configured_path):
        candidate_paths = [candidate]
        if os.path.isdir(candidate):
            candidate_paths.append(os.path.join(candidate, "hmpd"))

        for path in candidate_paths:
            checked.append(path)
            if os.path.isfile(path):
                if not os.access(path, os.X_OK):
                    try:
                        os.chmod(path, 0o755)
                    except OSError:
                        pass
                if os.access(path, os.X_OK):
                    return path
    raise FileNotFoundError("Could not find executable hmpd. Checked: " + ", ".join(checked))


def wait_for_hmpd(configured_path: str, retry_seconds: int) -> str:
    while True:
        try:
            return find_hmpd(configured_path)
        except FileNotFoundError as exc:
            log.error(
                "%s. Set hmpd_path in add-on options or place the binary in one of the checked "
                "paths. Retrying in %ss",
                exc,
                retry_seconds,
            )
            time.sleep(retry_seconds)


def build_hmpd_cmd(hmpd_path: str, controller: Controller, action_args: list[str]) -> list[str]:
    base = [hmpd_path, "--dev", controller.dev, "--baud", str(controller.baud), *action_args]
    stdbuf_path = shutil.which("stdbuf")
    if stdbuf_path:
        return [stdbuf_path, "-oL", *base]
    return base


def run_hmpd(cmd: list[str], timeout: int) -> list[str]:
    """Run an hmpd command to completion and return its non-empty output lines.

    Raises RuntimeError on timeout or non-zero exit.
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise RuntimeError(f"Command {cmd!r} timed out after {timeout} seconds") from None

    stdout = (stdout or "").replace("\r", "")
    stderr = (stderr or "").replace("\r", "")
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]

    if proc.returncode != 0:
        raise RuntimeError(f"hmpd exited {proc.returncode}: {stderr.strip() or stdout.strip()}")

    return lines


def parse_temps(lines: list[str]) -> dict[int, float]:
    parsed: dict[int, float] = {}
    for line in lines:
        try:
            idx_str, val_str = line.split(":", 1)
            idx = int(idx_str.strip())
            val = float(val_str.strip())
            parsed[idx] = round(val, 1)
        except (ValueError, IndexError) as exc:
            log.error("TEMP parse error: %s | raw=%s", exc, line)
    log.debug("Parsed %s temps from %s lines", len(parsed), len(lines))
    return parsed


_REG_CUR_RE = re.compile(r"cur:\s*(-?\d+(?:\.\d+)?)")
_REG_TGT_RE = re.compile(r"tgt:\s*(-?\d+(?:\.\d+)?)")


def parse_regs(lines: list[str], temp_range: TempRange) -> dict[int, dict]:
    parsed: dict[int, dict] = {}
    for line in lines:
        try:
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 5:
                continue

            idx = int(parts[0].strip())
            name = parts[1].strip()
            if not name:
                continue

            current_temp: float | None = None
            m_cur = _REG_CUR_RE.search(parts[2])
            if m_cur:
                current_temp = round(float(m_cur.group(1)), 1)
            else:
                log.debug("REG idx=%s name=%s has no cur: field | raw=%s", idx, name, line)

            target_temp: float | None = None
            m_tgt = _REG_TGT_RE.search(parts[3])
            if m_tgt:
                target_temp = temp_range.snap(float(m_tgt.group(1)))

            parsed[idx] = {
                "name": name,
                "current_temp": current_temp,
                "target_temp": target_temp,
                "enabled": "EN" in parts[4],
            }
        except (ValueError, IndexError) as exc:
            log.error("REG parse error: %s | raw=%s", exc, line)
    log.debug("Parsed %s named registers from %s lines", len(parsed), len(lines))
    return parsed
