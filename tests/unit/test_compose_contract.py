"""The compose topology is a contract (spec sections 3.1 and 22.1).

These checks are cheap and run in the unit job, before anything starts a
container: a database that publishes a port in the production file is a
regression worth catching in seconds, not in an incident.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "docker-compose.yml"
DEV = ROOT / "docker-compose.dev.yml"
ENV_EXAMPLE = ROOT / ".env.example"
CADDYFILE = ROOT / "Caddyfile"
DOCKERFILE = ROOT / "Dockerfile"

APP_SERVICES = {"ingress", "worker", "scheduler"}
INFRA_SERVICES = {"postgres", "redis", "qdrant", "langfuse", "caddy"}
ALL_SERVICES = APP_SERVICES | INFRA_SERVICES
# Section 3.1: only Caddy publishes. Everything else lives on the internal network.
PRIVATE_SERVICES = ALL_SERVICES - {"caddy"}
TLS_SERVICES = {"postgres", "redis", "qdrant"}

DIGEST = re.compile(r"^[\w./-]+:[\w.-]+@sha256:[0-9a-f]{64}$")
ENV_REF = re.compile(r"\$\{([A-Z0-9_]+)(?::-[^}]*)?\}")


def load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        parsed = yaml.safe_load(handle)
    assert isinstance(parsed, dict), f"{path.name} is not a mapping"
    return parsed


@pytest.fixture(scope="module")
def base() -> dict[str, Any]:
    return load(BASE)


@pytest.fixture(scope="module")
def dev() -> dict[str, Any]:
    return load(DEV)


def test_the_files_of_the_topology_exist() -> None:
    for path in (BASE, DEV, ENV_EXAMPLE, CADDYFILE, DOCKERFILE):
        assert path.is_file(), f"{path.name} is missing"


def test_every_service_of_section_3_1_is_declared(base: dict[str, Any]) -> None:
    assert set(base["services"]) == ALL_SERVICES


@pytest.mark.parametrize("service", sorted(INFRA_SERVICES))
def test_infrastructure_images_are_pinned_by_digest(base: dict[str, Any], service: str) -> None:
    image = base["services"][service].get("image", "")
    assert DIGEST.match(image), f"{service}: {image!r} is not pinned as tag@sha256:..."


def test_application_services_share_one_build(base: dict[str, Any]) -> None:
    builds = {name: base["services"][name].get("build") for name in APP_SERVICES}
    assert all(builds.values()), f"application services must build an image: {builds}"
    assert len(set(map(str, builds.values()))) == 1, "ingress, worker and scheduler share an image"


@pytest.mark.parametrize("service", sorted(PRIVATE_SERVICES))
def test_production_compose_publishes_no_port_but_caddy(base: dict[str, Any], service: str) -> None:
    assert "ports" not in base["services"][service], f"{service} must not publish a port"


def test_caddy_is_the_only_published_service(base: dict[str, Any]) -> None:
    published = {n for n, s in base["services"].items() if s.get("ports")}
    assert published == {"caddy"}
    assert sorted(base["services"]["caddy"]["ports"]) == ["443:443", "80:80"]


@pytest.mark.parametrize("service", sorted(ALL_SERVICES))
def test_every_service_declares_a_healthcheck(base: dict[str, Any], service: str) -> None:
    assert "healthcheck" in base["services"][service], f"{service} has no healthcheck"


@pytest.mark.parametrize("service", sorted(APP_SERVICES))
def test_application_services_wait_for_healthy_dependencies(
    base: dict[str, Any], service: str
) -> None:
    depends = base["services"][service].get("depends_on", {})
    assert depends, f"{service} declares no dependency"
    assert all(spec["condition"] == "service_healthy" for spec in depends.values())


def mount_sources(service: dict[str, Any]) -> set[str]:
    """Volume sources, from either compose syntax.

    The certificate mounts use the long form so they can carry
    `create_host_path: false`; the data volumes stay short.
    """
    sources = set()
    for volume in service.get("volumes", []):
        if not isinstance(volume, dict):
            sources.add(volume.split(":")[0])
        elif "source" in volume:
            sources.add(volume["source"])
        # A tmpfs mount has a target and no source: there is no host path to
        # name, which is the whole point of it.
    return sources


def test_stateful_services_keep_a_named_volume(base: dict[str, Any]) -> None:
    declared = set(base.get("volumes") or {})
    for service in ("postgres", "redis", "qdrant"):
        assert mount_sources(base["services"][service]) & declared, f"{service} has no named volume"


# A path, a flag or a number under a *_KEY name is configuration, not a secret:
# `QDRANT__TLS__KEY: /certs/server.key` names a file, it does not embed one.
NOT_A_SECRET = re.compile(r"^(/|\./|true$|false$|\d+$)", re.IGNORECASE)
SECRET_NAME = re.compile(r"^-?\s*[A-Z0-9_]*(PASSWORD|SECRET|KEY|TOKEN|PEPPER)[A-Z0-9_]*[:=]")


def scratch_mount(service: dict[str, Any]) -> dict[str, Any] | None:
    """The tmpfs mount for `/tmp`, if the service declares one."""
    for mount in service.get("volumes") or []:
        if (
            isinstance(mount, dict)
            and mount.get("type") == "tmpfs"
            and mount.get("target") == "/tmp"
        ):
            return mount
    return None


@pytest.mark.parametrize("service", sorted(APP_SERVICES))
def test_the_application_writes_media_to_memory_and_not_to_disk(
    base: dict[str, Any], service: str
) -> None:
    """Section 11.1: a voice recording lands in tmpfs, never on a volume.

    Without the mount, `/tmp` is the container's writable layer — which is disk,
    survives a restart, and holds the recordings 11.3 deliberately keeps for six
    hours when transcription fails. The adapter cannot check this from inside
    the container, so the topology is where it has to be true.
    """
    assert scratch_mount(base["services"][service]) is not None, (
        f"{service} has no tmpfs for /tmp: downloaded audio would persist on host storage"
    )


@pytest.mark.parametrize("service", sorted(APP_SERVICES))
def test_the_memory_backed_scratch_space_is_bounded(base: dict[str, Any], service: str) -> None:
    """An unbounded tmpfs is host memory a malformed `file_id` can spend."""
    mount = scratch_mount(base["services"][service])
    assert mount is not None
    assert (mount.get("tmpfs") or {}).get("size"), f"{service} mounts /tmp without a size"


def test_no_literal_secret_lives_in_the_compose_file() -> None:
    """Every credential must come from the environment (section 22, Segredos)."""
    for line in BASE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not SECRET_NAME.match(stripped):
            continue
        _, _, value = stripped.partition(":" if ":" in stripped else "=")
        value = value.strip().strip("\"'")
        if not value or NOT_A_SECRET.match(value):
            continue
        assert ENV_REF.search(value), f"literal credential in docker-compose.yml: {stripped}"


@pytest.mark.parametrize("service", sorted(TLS_SERVICES))
def test_the_data_stores_are_configured_for_tls(base: dict[str, Any], service: str) -> None:
    """Section 22.1, transit layer: no plaintext hop, even inside the compose network."""
    blob = yaml.safe_dump(base["services"][service])
    assert "tls" in blob.lower() or "ssl" in blob.lower(), f"{service} has no TLS configuration"


def test_the_application_verifies_the_postgres_certificate() -> None:
    assert "sslmode=verify-full" in ENV_EXAMPLE.read_text(encoding="utf-8")


def test_the_dev_override_only_adds_local_ergonomics(
    base: dict[str, Any], dev: dict[str, Any]
) -> None:
    assert set(dev["services"]) <= set(base["services"])
    allowed = {"ports", "environment", "volumes", "command", "build", "profiles", "deploy"}
    for name, service in dev["services"].items():
        extra = set(service) - allowed
        assert not extra, f"dev override adds {sorted(extra)} to {name}"


def test_the_dev_override_is_what_publishes_the_database_ports(dev: dict[str, Any]) -> None:
    published = {n for n, s in dev["services"].items() if s.get("ports")}
    assert {"postgres", "redis", "qdrant"} <= published


def test_env_example_documents_every_variable_the_compose_files_reference(
    base: dict[str, Any], dev: dict[str, Any]
) -> None:
    referenced: set[str] = set()
    for path in (BASE, DEV):
        referenced |= set(ENV_REF.findall(path.read_text(encoding="utf-8")))
    documented = {
        line.split("=", 1)[0].strip()
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    assert referenced <= documented, f"undocumented: {sorted(referenced - documented)}"


def test_env_example_carries_no_real_looking_secret() -> None:
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        name, _, value = line.partition("=")
        if not re.search(r"PASSWORD|SECRET|KEY|TOKEN|PEPPER", name):
            continue
        if NOT_A_SECRET.match(value.strip()):
            # `FITTRACK_ACTIVE_KEY_VERSION=1` names a key, it is not one.
            continue
        assert not value.strip() or value.strip().startswith("change-me"), (
            f"{name.strip()} in .env.example must be empty or a change-me placeholder"
        )


# --------------------------------------------------------------------------- #
# Every hop verifies, and the edge exposes only what it must
# --------------------------------------------------------------------------- #


def test_the_caddy_admin_api_is_bound_to_loopback() -> None:
    """It is unauthenticated, and it can rewrite the live edge configuration.

    On `:2019` it is reachable from every container on the compose network, so
    one compromised service — or an SSRF capable enough — could point the public
    edge at anything private. The healthcheck already probes 127.0.0.1.
    """
    text = CADDYFILE.read_text(encoding="utf-8")
    assert "admin 127.0.0.1:2019" in text
    assert "\tadmin :2019" not in text


def test_caddy_receives_the_configured_acme_email(base: dict[str, Any]) -> None:
    """Otherwise every deployment silently uses the fallback contact address."""
    assert "FITTRACK_ACME_EMAIL" in base["services"]["caddy"]["environment"]


def test_langfuse_verifies_its_postgres_certificate(base: dict[str, Any]) -> None:
    """`hostssl` forces encryption, not authentication.

    Without a CA this hop would accept any certificate, which is precisely the
    gap the internal CA exists to close (spec 22.1).
    """
    assert "/certs" in yaml.safe_dump(base["services"]["langfuse"].get("volumes", []))
    documented = ENV_EXAMPLE.read_text(encoding="utf-8")
    dsn = next(
        line for line in documented.splitlines() if line.startswith("LANGFUSE_DATABASE_URL=")
    )
    assert "sslmode=require" in dsn
    assert "sslaccept=strict" in dsn
    assert "sslcert=" in dsn


# --------------------------------------------------------------------------- #
# Blast radius: what a compromised container can reach
# --------------------------------------------------------------------------- #

APP_ENV_ALLOWLIST = {
    "DATABASE_URL",
    "REDIS_URL",
    "QDRANT_URL",
    "QDRANT_API_KEY",
    "FITTRACK_TLS_CA_FILE",
    "FITTRACK_CONFIG_DIR",
    # Required by `fittrack.startup`, which every service runs before serving.
    "FITTRACK_CHANNELS",
    "FITTRACK_ENCRYPTION_KEYS",
    "FITTRACK_ACTIVE_KEY_VERSION",
    "FITTRACK_IDENTITY_PEPPER",
    # Listing a channel is a promise startup checks, so what backs the promise
    # has to arrive with it.
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_MODE",
    "TELEGRAM_WEBHOOK_SECRET",
    "TELEGRAM_WEBHOOK_URL",
    "WABA_PHONE_NUMBER_ID",
    "WABA_TOKEN",
    "WABA_APP_SECRET",
    "WABA_VERIFY_TOKEN",
    # Behaviour settings the services validate; unreachable means unenforced.
    "SESSION_IDLE_TIMEOUT_MIN",
    "SESSION_MAX_DURATION_MIN",
    "DEBOUNCE_WINDOW_S",
    "INTERRUPT_TTL_MIN",
    "ACK_CONFIDENCE_THRESHOLD",
    "CHANNEL_LINK_TTL_MIN",
    "GRAPH_RECURSION_LIMIT",
    "CHECKPOINT_RETENTION_DAYS",
}

# What must stay out: a compromise of the public ingress should not hand over
# credentials that belong to other services.
APP_ENV_FORBIDDEN = {
    "ANTHROPIC_API_KEY",
    "GROQ_API_KEY",
    "OPENAI_API_KEY",
    "POSTGRES_PASSWORD",
    "LANGFUSE_ENCRYPTION_KEY",
    "LANGFUSE_NEXTAUTH_SECRET",
    "MERCADOPAGO_ACCESS_TOKEN",
    "MERCADOPAGO_WEBHOOK_SECRET",
}


@pytest.mark.parametrize("service", sorted(APP_SERVICES))
def test_no_application_service_takes_the_whole_env_file(
    base: dict[str, Any], service: str
) -> None:
    """A catch-all `env_file` hands the public ingress every secret in `.env`.

    Provider keys, payment credentials, the Langfuse encryption key — none of
    which the ingress reads, all of which it would leak if compromised.
    """
    assert "env_file" not in base["services"][service], (
        f"{service} inherits the whole .env; list what it reads instead"
    )


@pytest.mark.parametrize("service", sorted(APP_SERVICES))
def test_application_services_declare_only_what_they_read(
    base: dict[str, Any], service: str
) -> None:
    declared = set(base["services"][service].get("environment", {}))
    extra = declared - APP_ENV_ALLOWLIST
    assert not extra, f"{service} receives {sorted(extra)}, which nothing in it reads yet"


def test_an_enabled_channel_has_its_credentials_in_the_topology(
    base: dict[str, Any],
) -> None:
    """Otherwise every service refuses to boot the moment a channel is enabled."""
    declared = set(base["services"]["ingress"]["environment"])
    assert {"FITTRACK_CHANNELS", "TELEGRAM_BOT_TOKEN", "TELEGRAM_MODE"} <= declared


@pytest.mark.parametrize("service", sorted(APP_SERVICES))
def test_no_application_service_receives_another_service_secret(
    base: dict[str, Any], service: str
) -> None:
    declared = set(base["services"][service].get("environment", {}))
    leaked = declared & APP_ENV_FORBIDDEN
    assert not leaked, f"{service} receives {sorted(leaked)}"


@pytest.mark.parametrize("service", sorted(ALL_SERVICES))
def test_no_container_receives_the_ca_signing_key(base: dict[str, Any], service: str) -> None:
    """`certs/ca/` holds `ca.key`. A container with it can mint a certificate
    that Postgres, Redis and Qdrant all trust, which is the entire boundary.
    """
    # Through `mount_sources`, which is the one place that knows a mount may be
    # a bind, a named volume or a tmpfs with no host path at all.
    for source in mount_sources(base["services"][service]):
        assert source.rstrip("/") != "./certs/ca", (
            f"{service} mounts the CA directory, including its private key"
        )


def test_langfuse_does_not_reuse_the_postgres_superuser() -> None:
    """The bootstrap user the image creates is a cluster superuser.

    Putting tracing tables in a separate database buys nothing if the role that
    reaches them can read every other one.
    """
    documented = ENV_EXAMPLE.read_text(encoding="utf-8")
    dsn = next(
        line for line in documented.splitlines() if line.startswith("LANGFUSE_DATABASE_URL=")
    )
    assert "//langfuse:" in dsn, "Langfuse must use its own restricted role"
    assert "LANGFUSE_DB_PASSWORD=" in documented


def test_the_secure_edge_profile_requires_tls_13() -> None:
    """Spec 22.1 names TLS 1.3 at the edge; Caddy's default still allows 1.2.

    It is a profile rather than a flag because Caddy refuses a TLS policy on a
    listener that terminates no TLS — the local `:80` run would not start.
    """
    text = CADDYFILE.read_text(encoding="utf-8")
    assert "(secure) {" in text
    assert "protocols tls1.3" in text
    assert "import {$FITTRACK_EDGE_PROFILE:secure}" in text, (
        "the default must be the safe profile: changing only the site address "
        "would otherwise leave the edge on Caddy's own TLS policy"
    )


def test_caddy_receives_the_edge_profile(base: dict[str, Any]) -> None:
    assert "FITTRACK_EDGE_PROFILE" in base["services"]["caddy"]["environment"]


@pytest.mark.parametrize(("service", "replicas"), [("ingress", 2), ("worker", 4)])
def test_the_production_topology_declares_its_replica_counts(
    base: dict[str, Any], service: str, replicas: int
) -> None:
    """Spec 3.1 names them; without the setting a deployment starts one of each."""
    assert base["services"][service]["deploy"]["replicas"] == replicas


@pytest.mark.parametrize("service", ["ingress", "worker"])
def test_the_dev_override_runs_one_of_each(dev: dict[str, Any], service: str) -> None:
    """A published host port cannot be shared, and a second worker only adds noise."""
    assert dev["services"][service]["deploy"]["replicas"] == 1
