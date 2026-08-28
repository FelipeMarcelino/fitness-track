"""Everything under `tests/integration/` needs real services.

Marking by directory instead of by decorator keeps `make test` honest: a new
integration test cannot forget its marker and end up running in the cheap job
(spec section 21.4 orders the cheap gates first).

The fixtures below make one suite work from two places — inside the `worker`
container, where the services answer by their compose name, and from the host,
where the dev override publishes them on loopback. The certificates carry both
names in their SAN precisely so that `verify-full` holds either way.
"""

from __future__ import annotations

import os
import ssl
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]

# The application image mounts the CA at /certs; a checkout has it under ./certs.
IN_CONTAINER = Path("/certs/ca.crt").exists()
CA_FILE = Path("/certs/ca.crt") if IN_CONTAINER else ROOT / "certs" / "ca" / "ca.crt"
HOST = {
    "postgres": "postgres" if IN_CONTAINER else "localhost",
    "redis": "redis" if IN_CONTAINER else "localhost",
    "qdrant": "qdrant" if IN_CONTAINER else "localhost",
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # The hook receives the whole session, not just this directory's items.
    here = Path(__file__).parent
    for item in items:
        if item.path is not None and item.path.is_relative_to(here):
            item.add_marker(pytest.mark.integration)


def _dotenv() -> dict[str, str]:
    path = ROOT / ".env"
    if not path.is_file():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            name, _, value = line.partition("=")
            values[name.strip()] = value.strip()
    return values


@pytest.fixture(scope="session")
def env() -> dict[str, str]:
    """Settings from the process environment, falling back to the local `.env`."""
    return {**_dotenv(), **os.environ}


@pytest.fixture(scope="session")
def ca_file() -> Path:
    if not CA_FILE.is_file():
        pytest.skip(f"no development CA at {CA_FILE}; run `make certs`")
    return CA_FILE


def _verifying_context(ca: Path) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=str(ca))
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


@pytest.fixture(scope="session")
def trusting_ssl_context(ca_file: Path) -> ssl.SSLContext:
    """What the application uses: verify the chain *and* the hostname."""
    return _verifying_context(ca_file)


@pytest.fixture(scope="session")
def foreign_ca_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A CA that signed nothing in this stack.

    This is what gives the TLS tests their teeth: a client that merely *offers*
    TLS proves nothing, because the handshake succeeds against any certificate.
    Verification is what has to fail.
    """
    directory = tmp_path_factory.mktemp("foreign-ca")
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-days",
            "1",
            "-nodes",
            "-keyout",
            str(directory / "ca.key"),
            "-out",
            str(directory / "ca.crt"),
            "-subj",
            "/CN=Not The FitTrack CA",
        ],
        check=True,
        capture_output=True,
    )
    return directory / "ca.crt"


@pytest.fixture(scope="session")
def foreign_ssl_context(foreign_ca_file: Path) -> ssl.SSLContext:
    return _verifying_context(foreign_ca_file)


def _credentials_from(url: str) -> tuple[str, str]:
    """The user and password a connection URL already carries."""
    parsed = urlsplit(url)
    return unquote(parsed.username or ""), unquote(parsed.password or "")


def _required(env: dict[str, str], *, name: str, url_variable: str) -> str:
    """A credential, from the environment or from the URL that already holds it.

    Deliberately not a skip. Inside the compose worker these tests are the only
    thing standing between the TLS boundary and nobody having checked it, and a
    green run that quietly tested one store out of three is worse than a red
    one. The suite skips only where there is no configured stack at all — which
    the CA fixture decides.
    """
    direct = env.get(name, "")
    if direct:
        return direct
    _, password = _credentials_from(env.get(url_variable, ""))
    if password:
        return password
    raise RuntimeError(
        f"neither {name} nor a password in {url_variable} is available; "
        "run `make env`, or inject the test credentials into the container"
    )


@pytest.fixture(scope="session")
def postgres_dsn(env: dict[str, str], ca_file: Path) -> str:
    user = env.get("POSTGRES_USER", "fittrack")
    database = env.get("POSTGRES_DB", "fittrack")
    password = _required(env, name="POSTGRES_PASSWORD", url_variable="DATABASE_URL")
    return f"postgresql://{user}:{password}@{HOST['postgres']}:5432/{database}"


@pytest.fixture(scope="session")
def redis_password(env: dict[str, str], ca_file: Path) -> str:
    return _required(env, name="REDIS_PASSWORD", url_variable="REDIS_URL")
