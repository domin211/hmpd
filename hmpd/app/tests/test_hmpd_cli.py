import os
import sys

import pytest

from hmpd_bridge import hmpd_cli
from hmpd_bridge.config import TempRange


def test_parse_temps_skips_only_malformed_lines():
    lines = ["0: 21.4", "1: 19.9", "not-a-line", "2: 3027.1", "3: -60.0"]
    parsed = hmpd_cli.parse_temps(lines)
    assert parsed == {0: 21.4, 1: 19.9, 2: 3027.1, 3: -60.0}


def test_parse_regs_extracts_fields_and_enabled_flag():
    lines = [
        "0 | Living Room | cur: 21.3 | tgt: 22.0 | EN",
        "1 | Bedroom | cur: 19.5 | tgt: 18.4 | DIS",
        "not | enough | fields",
        "2 | | cur: 20.0 | tgt: 20.0 | EN",  # blank name is skipped
    ]
    parsed = hmpd_cli.parse_regs(lines, TempRange())

    assert parsed[0] == {"name": "Living Room", "current_temp": 21.3, "target_temp": 22.0, "enabled": True}
    assert parsed[1]["enabled"] is False
    assert 2 not in parsed


def test_parse_regs_snaps_target_and_passes_through_current_temp_unfiltered():
    lines = ["0 | Room | cur: 3027.1 | tgt: 22.4 | EN"]
    parsed = hmpd_cli.parse_regs(lines, TempRange())
    assert parsed[0]["current_temp"] == 3027.1
    assert parsed[0]["target_temp"] == 22.0


def test_hmpd_candidates_dedupes_and_keeps_configured_path_first():
    candidates = hmpd_cli.hmpd_candidates("/custom/hmpd")
    assert candidates[0] == "/custom/hmpd"
    assert len(candidates) == len(set(candidates))


def test_find_hmpd_locates_executable(tmp_path):
    binary = tmp_path / "hmpd"
    binary.write_text("#!/bin/sh\necho hi\n")
    os.chmod(binary, 0o755)

    found = hmpd_cli.find_hmpd(str(binary))
    assert found == str(binary)


def test_find_hmpd_raises_when_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        hmpd_cli.find_hmpd(str(tmp_path / "does-not-exist"))


def test_run_hmpd_returns_stripped_nonempty_lines():
    cmd = [sys.executable, "-c", "print('a'); print(''); print(' b ')"]
    assert hmpd_cli.run_hmpd(cmd, timeout=5) == ["a", "b"]


def test_run_hmpd_raises_on_nonzero_exit():
    cmd = [sys.executable, "-c", "import sys; sys.exit(3)"]
    with pytest.raises(RuntimeError, match="exited 3"):
        hmpd_cli.run_hmpd(cmd, timeout=5)


def test_run_hmpd_raises_on_timeout():
    cmd = [sys.executable, "-c", "import time; time.sleep(5)"]
    with pytest.raises(RuntimeError, match="timed out"):
        hmpd_cli.run_hmpd(cmd, timeout=1)
