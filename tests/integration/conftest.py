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
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import unquote, urlsplit

import asyncpg
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


# --------------------------------------------------------------------------- #
# Database principals (spec 19.1)
# --------------------------------------------------------------------------- #


def _dsn(user: str, password: str, database: str) -> str:
    return f"postgresql://{user}:{password}@{HOST['postgres']}:5432/{database}"


@pytest.fixture(scope="session")
def owner_dsn(env: dict[str, str], ca_file: Path) -> str:
    """The migration principal. Owns the schema; never what the app connects as.

    Everything is taken from `MIGRATION_DATABASE_URL` when it is set, user and
    database included — a CI environment whose owner or database is not the
    default would otherwise fail to authenticate before running a single test.

    Raises rather than skips, for the same reason as the store credentials: a
    green run that quietly did not migrate proves nothing.
    """
    configured = env.get("MIGRATION_DATABASE_URL", "")
    user, password = _credentials_from(configured)
    database = urlsplit(configured).path.strip("/") if configured else ""
    return _dsn(
        user or env.get("POSTGRES_USER", "fittrack"),
        password or _required(env, name="POSTGRES_PASSWORD", url_variable="MIGRATION_DATABASE_URL"),
        database or env.get("POSTGRES_DB", "fittrack"),
    )


def verified_dsn(dsn: str) -> str:
    """The same DSN with the TLS parameters the migration runner needs.

    The fixtures above pass an `SSLContext` straight to asyncpg; a subprocess
    cannot, so it gets the libpq spelling and `fittrack.db.engine` turns it back
    into a context.
    """
    separator = "&" if "?" in dsn else "?"
    return (
        dsn.replace("postgresql://", "postgresql+asyncpg://")
        + f"{separator}sslmode=verify-full&sslrootcert={CA_FILE}"
    )


@pytest.fixture(scope="session")
def app_dsn(env: dict[str, str], ca_file: Path) -> str:
    """The runtime principal: NOSUPERUSER NOBYPASSRLS, owning nothing."""
    password = _required(env, name="FITTRACK_RUNTIME_PASSWORD", url_variable="DATABASE_URL")
    return _dsn("fittrack_runtime", password, env.get("POSTGRES_DB", "fittrack"))


async def _connect(dsn: str, context: ssl.SSLContext) -> asyncpg.Connection:
    return await asyncpg.connect(dsn, ssl=context)


@pytest.fixture
async def owner(
    owner_dsn: str, trusting_ssl_context: ssl.SSLContext, migrated: None
) -> AsyncIterator[asyncpg.Connection]:
    connection = await _connect(owner_dsn, trusting_ssl_context)
    try:
        yield connection
    finally:
        await connection.close()


@pytest.fixture
async def app(
    app_dsn: str, trusting_ssl_context: ssl.SSLContext, migrated: None
) -> AsyncIterator[asyncpg.Connection]:
    connection = await _connect(app_dsn, trusting_ssl_context)
    try:
        yield connection
    finally:
        await connection.close()


@pytest.fixture(scope="session")
def migrated(owner_dsn: str) -> None:
    """Bring the database to head once per session.

    Idempotent: `alembic upgrade head` on an already-migrated database is a
    no-op, so the suite does not care whether the stack was freshly created.
    """
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "MIGRATION_DATABASE_URL": verified_dsn(owner_dsn),
        },
    )
    if result.returncode != 0:
        pytest.fail(
            f"alembic upgrade head failed (exit {result.returncode}):\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )


@pytest.fixture
async def disposable_database(
    owner_dsn: str, trusting_ssl_context: ssl.SSLContext
) -> AsyncIterator[str]:
    """A database created for one test and dropped after it.

    The migration cycle test drops every table of section 5.2. Run against the
    application database, it would take a developer's local workouts with it.
    """
    import secrets

    name = f"fittrack_cycle_{secrets.token_hex(6)}"
    admin = await asyncpg.connect(owner_dsn, ssl=trusting_ssl_context)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()

    try:
        yield urlsplit(owner_dsn)._replace(path=f"/{name}").geturl()
    finally:
        admin = await asyncpg.connect(owner_dsn, ssl=trusting_ssl_context)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await admin.close()


@pytest.fixture
async def disposable_migrated_database(disposable_database: str) -> str:
    """A disposable database with the schema on it.

    For tests of operations that are *table-wide*. Those cannot run against the
    shared database: other modules leave rows encrypted under their own keys,
    and the operation would correctly refuse them — noise rather than the
    behaviour under test. Emptying the shared table instead would take other
    modules' data with it, through the three foreign keys that cascade from
    `channel_identity`.
    """
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "MIGRATION_DATABASE_URL": verified_dsn(disposable_database)},
    )
    if result.returncode != 0:
        pytest.fail(f"could not migrate the disposable database:\n{result.stderr}")
    return disposable_database
