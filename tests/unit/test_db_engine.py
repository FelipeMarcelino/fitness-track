"""Translating a libpq DSN into what asyncpg actually accepts.

`sslmode=verify-full&sslrootcert=...` is how everyone writes a verified Postgres
URL, and it is what `.env.example` documents. asyncpg takes neither: SQLAlchemy
hands unknown query parameters straight to `asyncpg.connect()`, which rejects
them. The intent stays in the URL; the translation happens here, in one place,
so the two cannot drift.
"""

from __future__ import annotations

import ssl
from pathlib import Path

import pytest

from fittrack.db.engine import split_ssl_arguments

CA = Path(__file__).resolve().parents[2] / "certs" / "ca" / "ca.crt"
BASE = "postgresql+asyncpg://u:p@postgres:5432/fittrack"


def dsn(**params: str) -> str:
    query = "&".join(f"{name}={value}" for name, value in params.items())
    return f"{BASE}?{query}" if query else BASE


@pytest.fixture(scope="module")
def ca_file() -> Path:
    if not CA.is_file():
        pytest.skip("no development CA; run `make certs`")
    return CA


def test_the_ssl_parameters_are_removed_from_the_url(ca_file: Path) -> None:
    """They would reach `asyncpg.connect()` as unexpected keyword arguments."""
    url, _ = split_ssl_arguments(dsn(sslmode="verify-full", sslrootcert=str(ca_file)))
    assert "sslmode" not in url
    assert "sslrootcert" not in url
    assert url.startswith("postgresql+asyncpg://u:p@postgres:5432/fittrack")


def test_verify_full_produces_a_context_that_checks_the_hostname(ca_file: Path) -> None:
    _, args = split_ssl_arguments(dsn(sslmode="verify-full", sslrootcert=str(ca_file)))
    context = args["ssl"]
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname
    assert context.verify_mode is ssl.CERT_REQUIRED


def test_verify_ca_checks_the_chain_but_not_the_hostname(ca_file: Path) -> None:
    """Recognised so the difference is explicit, not so it is recommended.

    `verify-ca` accepts any certificate this CA signed, which in the compose
    topology is every service — settings reject it for that reason.
    """
    _, args = split_ssl_arguments(dsn(sslmode="verify-ca", sslrootcert=str(ca_file)))
    assert args["ssl"].verify_mode is ssl.CERT_REQUIRED
    assert not args["ssl"].check_hostname


def test_the_root_certificate_is_loaded(ca_file: Path) -> None:
    _, args = split_ssl_arguments(dsn(sslmode="verify-full", sslrootcert=str(ca_file)))
    assert args["ssl"].get_ca_certs(), "the CA was not loaded into the context"


def test_verify_full_without_a_root_certificate_is_refused() -> None:
    """Falling back to the system trust store would verify nothing useful here."""
    with pytest.raises(ValueError, match="sslrootcert"):
        split_ssl_arguments(dsn(sslmode="verify-full"))


def test_a_missing_root_certificate_file_says_so() -> None:
    with pytest.raises(ValueError, match="nowhere"):
        split_ssl_arguments(dsn(sslmode="verify-full", sslrootcert="/nowhere/ca.crt"))


def test_a_url_without_ssl_parameters_is_left_alone() -> None:
    url, args = split_ssl_arguments(dsn())
    assert url == BASE
    assert args == {}


def test_other_query_parameters_survive(ca_file: Path) -> None:
    url, _ = split_ssl_arguments(
        dsn(sslmode="verify-full", sslrootcert=str(ca_file), application_name="fittrack")
    )
    assert "application_name=fittrack" in url


@pytest.mark.parametrize("mode", ["disable", "allow", "prefer", "require"])
def test_a_mode_that_does_not_verify_is_refused(mode: str) -> None:
    """Spec 22.1 has no unverified hop; accepting one here would create it quietly."""
    with pytest.raises(ValueError, match=mode):
        split_ssl_arguments(dsn(sslmode=mode))
