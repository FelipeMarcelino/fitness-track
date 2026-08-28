"""Liveness probes for the three application services.

They exist so compose can order startup by `service_healthy` (S01-T02). They
carry no business behaviour, and the tests here are what stops them growing any.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fittrack.health import (
    DEFAULT_MAX_AGE_S,
    heartbeat_is_fresh,
    main,
    write_heartbeat,
)


def test_a_fresh_heartbeat_is_fresh(tmp_path: Path) -> None:
    beat = tmp_path / "worker.hb"
    write_heartbeat(beat)
    assert heartbeat_is_fresh(beat)


def test_a_missing_heartbeat_is_not_fresh(tmp_path: Path) -> None:
    assert not heartbeat_is_fresh(tmp_path / "absent.hb")


def test_a_stale_heartbeat_is_not_fresh(tmp_path: Path) -> None:
    beat = tmp_path / "worker.hb"
    write_heartbeat(beat)
    assert not heartbeat_is_fresh(beat, now=beat.stat().st_mtime + DEFAULT_MAX_AGE_S + 1)


def test_the_boundary_is_inclusive(tmp_path: Path) -> None:
    beat = tmp_path / "worker.hb"
    write_heartbeat(beat)
    assert heartbeat_is_fresh(beat, now=beat.stat().st_mtime + DEFAULT_MAX_AGE_S)


def test_the_cli_reports_a_fresh_heartbeat(tmp_path: Path) -> None:
    beat = tmp_path / "worker.hb"
    write_heartbeat(beat)
    assert main(["--heartbeat", str(beat)]) == 0


def test_the_cli_fails_on_a_missing_heartbeat(tmp_path: Path) -> None:
    assert main(["--heartbeat", str(tmp_path / "absent.hb")]) == 1


def test_the_cli_needs_exactly_one_probe() -> None:
    with pytest.raises(SystemExit):
        main([])
