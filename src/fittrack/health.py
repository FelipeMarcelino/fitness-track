"""Liveness probes for the compose healthchecks (spec 3.1).

Two shapes, because the services have two shapes. The ingress serves HTTP, so
it is probed over HTTP. The worker and the scheduler serve nothing, so they
touch a heartbeat file and the probe asks how old it is.

Nothing here reads the database or the queue: a probe that fails when a
dependency is down turns one outage into three.
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_MAX_AGE_S = 30.0
DEFAULT_HTTP_TIMEOUT_S = 3.0


def write_heartbeat(path: Path) -> None:
    """Record that the process is still going round its loop."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(time.time()), encoding="utf-8")


def heartbeat_is_fresh(
    path: Path, max_age_s: float = DEFAULT_MAX_AGE_S, now: float | None = None
) -> bool:
    try:
        age = (time.time() if now is None else now) - path.stat().st_mtime
    except OSError:
        return False
    return age <= max_age_s


def http_is_healthy(url: str, timeout_s: float = DEFAULT_HTTP_TIMEOUT_S) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:
            status: int = response.status
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return 200 <= status < 300


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FitTrack liveness probe")
    probe = parser.add_mutually_exclusive_group(required=True)
    probe.add_argument("--heartbeat", type=Path, help="heartbeat file of a loop service")
    probe.add_argument("--http", help="health URL of an HTTP service")
    parser.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE_S)
    args = parser.parse_args(argv)

    if args.heartbeat is not None:
        return 0 if heartbeat_is_fresh(args.heartbeat, args.max_age) else 1
    return 0 if http_is_healthy(args.http) else 1


if __name__ == "__main__":
    sys.exit(main())
