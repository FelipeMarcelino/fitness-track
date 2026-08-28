"""Nothing secret is tracked by Git.

S01-T01 asks for it in one line — "nenhum artefato de ambiente ou secret entra
no Git" — and it is the kind of criterion that holds until the first `git add -A`
runs in a tree that has generated material lying in it. That is exactly how the
development CA private key reached a commit in this repository, so the criterion
now has a test instead of a sentence.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# Extensions and names that carry key material or a filled-in environment.
FORBIDDEN_NAMES = re.compile(
    r"(^|/)("
    r"\.env"  # the filled template; .env.example is the tracked one
    r"|.*\.(key|pem|p12|pfx|jks|keystore)"
    r"|id_(rsa|dsa|ecdsa|ed25519)"
    r"|.*\.srl"  # openssl serial files travel with a CA
    r")$"
)
ALLOWED = {".env.example"}

PRIVATE_KEY_HEADER = re.compile(rb"-----BEGIN (RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----")


def tracked_files() -> list[str] | None:
    """Tracked paths, or None where Git cannot answer.

    The application image ships the tree without `.git` and without a Git
    binary, so this check belongs to the host and to CI. Returning None rather
    than an empty list keeps "nothing is tracked" distinguishable from
    "cannot tell" — the first would be a silent pass.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    return [name for name in result.stdout.decode().split("\0") if name]


@pytest.fixture(scope="module")
def tracked() -> list[str]:
    files = tracked_files()
    if not files:
        pytest.skip("not a git checkout, or Git is unavailable")
    return files


def test_no_key_material_or_filled_env_is_tracked(tracked: list[str]) -> None:
    offenders = [name for name in tracked if FORBIDDEN_NAMES.search(name) and name not in ALLOWED]
    assert not offenders, f"secret-bearing files are tracked: {offenders}"


def test_the_certificate_directory_is_never_tracked(tracked: list[str]) -> None:
    """`make certs` writes here; the CA private key must not follow it into Git."""
    assert not [name for name in tracked if name.startswith("certs/")]


def test_no_tracked_file_contains_a_private_key(tracked: list[str]) -> None:
    """Catches the same material under a name the pattern above does not know."""
    offenders = []
    for name in tracked:
        path = ROOT / name
        if not path.is_file() or path.stat().st_size > 1_000_000:
            continue
        if PRIVATE_KEY_HEADER.search(path.read_bytes()):
            offenders.append(name)
    assert not offenders, f"tracked files contain a private key: {offenders}"
