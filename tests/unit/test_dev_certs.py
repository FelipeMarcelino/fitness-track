"""`make certs` must repair a partial tree, not declare victory over it.

An interrupted first run leaves `ca.crt` behind without the service
certificates. Skipping on that one file makes every later run a no-op and the
directory permanently incomplete — and the failure then surfaces as a missing
bind mount, which points nowhere near the cause.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "gen_dev_certs.sh"

EXPECTED = [
    "ca/ca.crt",
    "ca/ca.key",
    *[
        f"{service}/{name}"
        for service in ("postgres", "redis", "qdrant")
        for name in ("server.crt", "server.key", "ca.crt")
    ],
]


def generate(workdir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # The copy, not the original: the script resolves the repository root from
    # its own path, so running the original would operate on the real tree.
    return subprocess.run(
        ["bash", str(workdir / "scripts" / SCRIPT.name), *args],
        cwd=workdir,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(workdir)},
    )


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """A throwaway tree the script can treat as the repository root."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(SCRIPT, scripts / SCRIPT.name)
    return tmp_path


def test_a_clean_run_produces_every_artifact(workdir: Path) -> None:
    result = generate(workdir)
    assert result.returncode == 0, result.stderr
    for relative in EXPECTED:
        assert (workdir / "certs" / relative).is_file(), relative


def test_a_second_run_is_a_no_op(workdir: Path) -> None:
    generate(workdir)
    before = (workdir / "certs" / "ca" / "ca.crt").read_bytes()
    result = generate(workdir)
    assert result.returncode == 0
    assert (workdir / "certs" / "ca" / "ca.crt").read_bytes() == before


def test_force_regenerates(workdir: Path) -> None:
    generate(workdir)
    before = (workdir / "certs" / "ca" / "ca.crt").read_bytes()
    assert generate(workdir, "--force").returncode == 0
    assert (workdir / "certs" / "ca" / "ca.crt").read_bytes() != before


@pytest.mark.parametrize("missing", ["redis/server.key", "qdrant/server.crt", "ca/ca.key"])
def test_an_incomplete_tree_is_repaired(workdir: Path, missing: str) -> None:
    generate(workdir)
    (workdir / "certs" / missing).unlink()

    result = generate(workdir)
    assert result.returncode == 0, result.stderr
    for relative in EXPECTED:
        assert (workdir / "certs" / relative).is_file(), relative


def test_the_service_certificate_carries_both_names(workdir: Path) -> None:
    """The same certificate has to verify from inside the network and from the host."""
    generate(workdir)
    text = subprocess.run(
        [
            "openssl",
            "x509",
            "-in",
            str(workdir / "certs" / "postgres" / "server.crt"),
            "-noout",
            "-text",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "DNS:postgres" in text
    assert "DNS:localhost" in text
