"""`make env` must produce a `.env` the stack can actually boot with.

A `change-me` placeholder is right for the committed template and wrong for a
running service: Langfuse, for one, refuses anything that is not 64 hex
characters. The renderer fills the local-only credentials and keeps the derived
URLs in agreement with them, which is the part a human gets wrong by hand.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from scripts.init_dev_env import PLACEHOLDER, render_env

TEMPLATE = Path(__file__).resolve().parents[2] / ".env.example"


def parse(text: str) -> dict[str, str]:
    return {
        line.split("=", 1)[0].strip(): line.split("=", 1)[1].strip()
        for line in text.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }


@pytest.fixture(scope="module")
def rendered() -> dict[str, str]:
    return parse(render_env(TEMPLATE.read_text(encoding="utf-8")))


def test_no_placeholder_survives(rendered: dict[str, str]) -> None:
    leftover = {name for name, value in rendered.items() if PLACEHOLDER in value}
    assert not leftover


def test_the_langfuse_encryption_key_is_64_hex_characters(rendered: dict[str, str]) -> None:
    assert re.fullmatch(r"[0-9a-f]{64}", rendered["LANGFUSE_ENCRYPTION_KEY"])


def test_the_database_url_carries_the_generated_password(rendered: dict[str, str]) -> None:
    password = rendered["POSTGRES_PASSWORD"]
    assert password and PLACEHOLDER not in password
    assert f":{password}@postgres:5432" in rendered["DATABASE_URL"]


def test_langfuse_gets_its_own_credential(rendered: dict[str, str]) -> None:
    """Reusing the bootstrap superuser would make the separate database decorative."""
    password = rendered["LANGFUSE_DB_PASSWORD"]
    assert password and PLACEHOLDER not in password
    assert rendered["LANGFUSE_DATABASE_URL"].startswith(f"postgresql://langfuse:{password}@")
    assert rendered["POSTGRES_PASSWORD"] not in rendered["LANGFUSE_DATABASE_URL"]


def test_the_database_url_keeps_verifying_the_certificate(rendered: dict[str, str]) -> None:
    assert "sslmode=verify-full" in rendered["DATABASE_URL"]
    assert "sslrootcert=/certs/ca.crt" in rendered["DATABASE_URL"]


def test_the_redis_url_carries_the_generated_password_over_tls(rendered: dict[str, str]) -> None:
    assert rendered["REDIS_URL"] == f"rediss://:{rendered['REDIS_PASSWORD']}@redis:6379/0"


def test_generated_credentials_are_url_safe(rendered: dict[str, str]) -> None:
    for name in ("POSTGRES_PASSWORD", "REDIS_PASSWORD"):
        assert re.fullmatch(r"[A-Za-z0-9_-]+", rendered[name]), name


def test_provider_credentials_stay_empty(rendered: dict[str, str]) -> None:
    """Only local infrastructure is generated; a real API key is the operator's."""
    for name in ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "TELEGRAM_BOT_TOKEN"):
        assert rendered[name] == ""


def test_non_secret_settings_are_left_alone(rendered: dict[str, str]) -> None:
    # Empty in the template: listing a channel is a promise its credentials
    # exist, and this sprint ships no Telegram adapter to hold it.
    assert rendered["FITTRACK_CHANNELS"] == ""
    assert rendered["DEBOUNCE_WINDOW_S"] == "10"
    assert rendered["FITTRACK_ACTIVE_KEY_VERSION"] == "1"


def test_two_renders_do_not_share_a_secret() -> None:
    first = parse(render_env(TEMPLATE.read_text(encoding="utf-8")))
    second = parse(render_env(TEMPLATE.read_text(encoding="utf-8")))
    assert first["POSTGRES_PASSWORD"] != second["POSTGRES_PASSWORD"]


def test_comments_and_ordering_survive() -> None:
    template = TEMPLATE.read_text(encoding="utf-8")
    rendered = render_env(template)
    assert [line for line in template.splitlines() if line.startswith("#")] == [
        line for line in rendered.splitlines() if line.startswith("#")
    ]
    assert [line.split("=", 1)[0] for line in template.splitlines() if "=" in line] == [
        line.split("=", 1)[0] for line in rendered.splitlines() if "=" in line
    ]


# --------------------------------------------------------------------------- #
# Rotation and file mode
# --------------------------------------------------------------------------- #


def test_a_volume_bound_credential_is_preserved_on_a_plain_regeneration() -> None:
    """Postgres reads POSTGRES_PASSWORD only when it initialises its data directory.

    Rotating it against a live volume leaves the server on the old password
    while every client starts using the new one — the stack simply stops
    connecting, with an error that looks like anything but a credential change.
    """
    from scripts.init_dev_env import render_env

    template = TEMPLATE.read_text(encoding="utf-8")
    first = render_env(template)
    second = render_env(template, previous=parse(first))

    kept = parse(second)
    for name in ("POSTGRES_PASSWORD", "LANGFUSE_DB_PASSWORD"):
        assert kept[name] == parse(first)[name], name


def test_a_credential_that_is_not_volume_bound_is_regenerated() -> None:
    from scripts.init_dev_env import render_env

    template = TEMPLATE.read_text(encoding="utf-8")
    first = render_env(template)
    second = render_env(template, previous=parse(first))
    assert parse(second)["LANGFUSE_NEXTAUTH_SECRET"] != parse(first)["LANGFUSE_NEXTAUTH_SECRET"]


def test_rotation_is_explicit() -> None:
    from scripts.init_dev_env import render_env

    template = TEMPLATE.read_text(encoding="utf-8")
    first = render_env(template)
    rotated = render_env(template, previous=parse(first), rotate=True)
    assert parse(rotated)["POSTGRES_PASSWORD"] != parse(first)["POSTGRES_PASSWORD"]


def test_the_derived_urls_follow_a_preserved_password() -> None:
    from scripts.init_dev_env import render_env

    template = TEMPLATE.read_text(encoding="utf-8")
    first = parse(render_env(template))
    second = parse(render_env(template, previous=first))
    assert f":{second['POSTGRES_PASSWORD']}@postgres:5432" in second["DATABASE_URL"]
    assert second["DATABASE_URL"] == first["DATABASE_URL"]
    assert second["LANGFUSE_DATABASE_URL"] == first["LANGFUSE_DATABASE_URL"]


def test_an_existing_env_file_has_its_mode_restricted(tmp_path: Path) -> None:
    """An operator who copied the template under a 022 umask leaves it 0644.

    Tightened even when the file is then rejected as incomplete: a world-readable
    file full of secrets should not stay that way because it also had another
    problem.
    """
    import stat

    from scripts.init_dev_env import main

    output = tmp_path / ".env"
    output.write_text("FITTRACK_CHANNELS=\n", encoding="utf-8")
    output.chmod(0o644)

    assert main(["--template", str(TEMPLATE), "--output", str(output)]) == 1
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_a_generated_env_file_is_private(tmp_path: Path) -> None:
    import stat

    from scripts.init_dev_env import main

    output = tmp_path / ".env"
    assert main(["--template", str(TEMPLATE), "--output", str(output)]) == 0
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_an_existing_file_still_holding_placeholders_is_refused(tmp_path: Path) -> None:
    """A copied template is the common case, and `make up` used to accept it.

    The stack then fails at Langfuse's key validation, several minutes and one
    confusing error later.
    """
    from scripts.init_dev_env import main

    output = tmp_path / ".env"
    output.write_text("POSTGRES_PASSWORD=change-me\n", encoding="utf-8")
    output.chmod(0o600)

    assert main(["--template", str(TEMPLATE), "--output", str(output)]) == 1


def test_operator_values_survive_a_forced_regeneration(tmp_path: Path) -> None:
    """A filled provider key is the operator's work, not the generator's to discard."""
    from scripts.init_dev_env import main

    output = tmp_path / ".env"
    assert main(["--template", str(TEMPLATE), "--output", str(output)]) == 0

    # Line-precise: `FITTRACK_CHANNELS=` is also a substring of the comment that
    # documents it, and a blanket replace edits both.
    edits = {"ANTHROPIC_API_KEY": "sk-operator-supplied", "FITTRACK_CHANNELS": "telegram"}
    lines = []
    for line in output.read_text(encoding="utf-8").splitlines():
        name = line.split("=", 1)[0]
        lines.append(f"{name}={edits[name]}" if name in edits else line)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert main(["--template", str(TEMPLATE), "--output", str(output), "--force"]) == 0
    kept = parse(output.read_text(encoding="utf-8"))
    assert kept["ANTHROPIC_API_KEY"] == "sk-operator-supplied"
    assert kept["FITTRACK_CHANNELS"] == "telegram"


def test_an_existing_file_missing_a_newer_variable_is_refused(tmp_path: Path) -> None:
    """A `.env` predating a template addition is as broken as one full of placeholders.

    Compose substitutes an empty value for the missing name, and the failure
    lands in an initdb script rather than at the configuration step.
    """
    from scripts.init_dev_env import main

    output = tmp_path / ".env"
    assert main(["--template", str(TEMPLATE), "--output", str(output)]) == 0

    without = [
        line
        for line in output.read_text(encoding="utf-8").splitlines()
        if not line.startswith("LANGFUSE_DB_PASSWORD=")
    ]
    output.write_text("\n".join(without) + "\n", encoding="utf-8")

    assert main(["--template", str(TEMPLATE), "--output", str(output)]) == 1


def test_a_complete_existing_file_is_accepted(tmp_path: Path) -> None:
    from scripts.init_dev_env import main

    output = tmp_path / ".env"
    assert main(["--template", str(TEMPLATE), "--output", str(output)]) == 0
    assert main(["--template", str(TEMPLATE), "--output", str(output)]) == 0


def test_a_derived_url_that_disagrees_with_its_password_is_refused(tmp_path: Path) -> None:
    """An edited password without a rebuilt URL: no placeholder, still broken."""
    from scripts.init_dev_env import main

    output = tmp_path / ".env"
    assert main(["--template", str(TEMPLATE), "--output", str(output)]) == 0

    lines = [
        "POSTGRES_PASSWORD=edited-by-hand" if line.startswith("POSTGRES_PASSWORD=") else line
        for line in output.read_text(encoding="utf-8").splitlines()
    ]
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert main(["--template", str(TEMPLATE), "--output", str(output)]) == 1


def test_a_url_still_carrying_a_placeholder_is_refused(tmp_path: Path) -> None:
    """`change-me` sits *inside* the URL, where an equality check never sees it."""
    from scripts.init_dev_env import main

    output = tmp_path / ".env"
    output.write_text(TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    output.chmod(0o600)

    assert main(["--template", str(TEMPLATE), "--output", str(output)]) == 1


# The cryptographic contract (spec 22.2) has to be satisfiable locally
# --------------------------------------------------------------------------- #


def test_the_generated_keyring_is_a_valid_json_map(rendered: dict[str, str]) -> None:
    import base64
    import json

    keyring = json.loads(rendered["FITTRACK_ENCRYPTION_KEYS"])
    assert set(keyring) == {"1"}
    assert len(base64.b64decode(keyring["1"], validate=True)) == 32


def test_the_active_version_points_into_the_generated_keyring(
    rendered: dict[str, str],
) -> None:
    import json

    assert rendered["FITTRACK_ACTIVE_KEY_VERSION"] in json.loads(
        rendered["FITTRACK_ENCRYPTION_KEYS"]
    )


def test_the_pepper_is_independent_of_the_keyring(rendered: dict[str, str]) -> None:
    """They rotate by separate procedures; shared material would couple them."""
    pepper = rendered["FITTRACK_IDENTITY_PEPPER"]
    assert pepper
    assert pepper not in rendered["FITTRACK_ENCRYPTION_KEYS"]


def test_the_rendered_env_satisfies_the_settings_contract(rendered: dict[str, str]) -> None:
    """The end-to-end point of the generator: `make env` produces a bootable process."""
    import os

    from fittrack.settings import Settings

    with pytest.MonkeyPatch.context() as patch:
        for name in list(os.environ):
            patch.delenv(name, raising=False)
        for name, value in rendered.items():
            patch.setenv(name, value)
        settings = Settings(_env_file=None)

    assert settings.active_key_version in settings.encryption_keys
    assert settings.channels == ()


def test_the_encryption_material_survives_a_plain_regeneration() -> None:
    """A new keyring cannot read old ciphertext; a new pepper strands every lookup."""
    template = TEMPLATE.read_text(encoding="utf-8")
    first = parse(render_env(template))
    second = parse(render_env(template, previous=first))
    assert second["FITTRACK_ENCRYPTION_KEYS"] == first["FITTRACK_ENCRYPTION_KEYS"]
    assert second["FITTRACK_IDENTITY_PEPPER"] == first["FITTRACK_IDENTITY_PEPPER"]


def test_a_copied_placeholder_is_not_preserved_as_key_material(tmp_path: Path) -> None:
    """The common starting state: `.env.example` copied verbatim.

    Preserving `change-me` under a volume-bound name would look like careful key
    handling and leave the keyring unparseable, so every service fails startup.
    """
    from scripts.init_dev_env import main

    output = tmp_path / ".env"
    output.write_text(TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")

    assert main(["--template", str(TEMPLATE), "--output", str(output), "--force"]) == 0
    kept = parse(output.read_text(encoding="utf-8"))
    assert PLACEHOLDER not in kept["FITTRACK_ENCRYPTION_KEYS"]
    assert PLACEHOLDER not in kept["POSTGRES_PASSWORD"]


def test_an_empty_required_value_is_refused(tmp_path: Path) -> None:
    """A `.env` from an older revision carries the name with no value.

    Neither missing nor a placeholder — and it still fails startup.
    """
    from scripts.init_dev_env import main

    output = tmp_path / ".env"
    assert main(["--template", str(TEMPLATE), "--output", str(output)]) == 0

    lines = [
        "FITTRACK_ENCRYPTION_KEYS=" if line.startswith("FITTRACK_ENCRYPTION_KEYS=") else line
        for line in output.read_text(encoding="utf-8").splitlines()
    ]
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert main(["--template", str(TEMPLATE), "--output", str(output)]) == 1
