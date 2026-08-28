"""The versioned YAML configuration is typed and cross-validated at boot.

Spec sections 7.2 and 7.2.1 (model tiering by role, optional per-agent
override), 19.3 (cost ceiling per plan) and 15.3 (RAG).

These files are the only place model names live (CLAUDE.md, invariant 4), which
makes an unnoticed typo in one of them a production incident rather than a
config wart — so the loader refuses rather than guesses.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from fittrack.config import (
    ConfigError,
    ModelsConfig,
    QuotaConfig,
    RagConfig,
    load_models,
    load_quota,
    load_rag,
)
from fittrack.llm.roles import KNOWN_AGENTS, LLMRole

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def write(tmp_path: Path, name: str, payload: object) -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# models.yaml
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def models() -> ModelsConfig:
    return load_models(CONFIG_DIR / "models.yaml")


def test_every_role_is_configured(models: ModelsConfig) -> None:
    assert set(models.roles) == set(LLMRole)


def test_the_judge_has_no_primary(models: ModelsConfig) -> None:
    """Spec 7.2: a judge on the model that produced the answer is not a judge."""
    assert models.roles[LLMRole.JUDGE].primary is None


def test_every_other_role_has_a_primary_and_a_fallback(models: ModelsConfig) -> None:
    for role, config in models.roles.items():
        assert config.fallback is not None, role
        if role is not LLMRole.JUDGE:
            assert config.primary is not None, role


def test_phase_one_declares_no_agent_override(models: ModelsConfig) -> None:
    """Spec 7.2.1 recommends none: ten roles, zero exceptions, then measure."""
    assert models.agents == {}


def test_a_missing_role_fails_to_load(tmp_path: Path) -> None:
    payload = {"roles": {"EXTRACTOR": {"fallback": {"provider": "anthropic", "model": "x"}}}}
    with pytest.raises(ConfigError, match="ANALYST"):
        load_models(write(tmp_path, "models.yaml", payload))


def test_an_unknown_role_fails_to_load(tmp_path: Path) -> None:
    payload = {"roles": {"WIZARD": {"fallback": {"provider": "anthropic", "model": "x"}}}}
    with pytest.raises(ConfigError, match="WIZARD"):
        load_models(write(tmp_path, "models.yaml", payload))


def full_roles() -> dict[str, object]:
    return {
        role.value: {
            "primary": None
            if role is LLMRole.JUDGE
            else {"provider": "groq", "model": "m", "reasoning_effort": "low"},
            "fallback": {"provider": "anthropic", "model": "m"},
            "timeout_s": 30,
        }
        for role in LLMRole
    }


def test_an_override_must_declare_the_role_it_inherits(tmp_path: Path) -> None:
    payload = {"roles": full_roles(), "agents": {"proactive": {"primary": {"temperature": 0.1}}}}
    with pytest.raises(ConfigError, match="role"):
        load_models(write(tmp_path, "models.yaml", payload))


def test_an_override_for_an_unregistered_agent_fails(tmp_path: Path) -> None:
    """A renamed agent must not leave dead, silent configuration behind."""
    payload = {"roles": full_roles(), "agents": {"typo_agent": {"role": "COACH"}}}
    with pytest.raises(ConfigError, match="typo_agent"):
        load_models(write(tmp_path, "models.yaml", payload))


def test_an_override_is_partial_and_inherits_the_rest(tmp_path: Path) -> None:
    payload = {
        "roles": full_roles(),
        "agents": {"proactive": {"role": "COACH", "primary": {"reasoning_effort": "high"}}},
    }
    config = load_models(write(tmp_path, "models.yaml", payload))
    resolved = config.resolve(agent="proactive", role=LLMRole.COACH)
    assert resolved.primary is not None
    assert resolved.primary.reasoning_effort == "high"
    assert resolved.primary.provider == "groq"  # inherited
    assert resolved.timeout_s == 30  # inherited


def test_resolving_without_an_override_returns_the_role(tmp_path: Path) -> None:
    config = load_models(write(tmp_path, "models.yaml", {"roles": full_roles()}))
    assert config.resolve(agent="analysis", role=LLMRole.ANALYST) is config.roles[LLMRole.ANALYST]


def test_the_declared_role_must_match_the_role_the_agent_asks_for(tmp_path: Path) -> None:
    """Stops the YAML and the code disagreeing about an agent's cost class."""
    payload = {"roles": full_roles(), "agents": {"proactive": {"role": "COACH"}}}
    config = load_models(write(tmp_path, "models.yaml", payload))
    with pytest.raises(ConfigError, match="ANALYST"):
        config.resolve(agent="proactive", role=LLMRole.ANALYST)


def test_every_registered_agent_is_a_valid_override_target(tmp_path: Path) -> None:
    """Each declared under the role it actually invokes."""
    from fittrack.llm.roles import AGENT_ROLES

    payload = {
        "roles": full_roles(),
        "agents": {name: {"role": role.value} for name, role in AGENT_ROLES.items()},
    }
    assert len(load_models(write(tmp_path, "models.yaml", payload)).agents) == len(AGENT_ROLES)


def test_an_override_declaring_the_wrong_role_fails_to_load(tmp_path: Path) -> None:
    """`proactive` invokes COACH; declaring ANALYST used to validate against itself."""
    payload = {
        "roles": full_roles(),
        "agents": {"proactive": {"role": "ANALYST"}},
    }
    with pytest.raises(ConfigError, match="proactive"):
        load_models(write(tmp_path, "models.yaml", payload))


# --------------------------------------------------------------------------- #
# quota.yaml
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def quota() -> QuotaConfig:
    return load_quota(CONFIG_DIR / "quota.yaml")


def test_every_plan_has_a_ceiling(quota: QuotaConfig) -> None:
    assert set(quota.plans) == {"free", "pro", "trial"}


def test_pro_is_more_generous_than_free(quota: QuotaConfig) -> None:
    assert quota.plans["pro"].llm_usd_month > quota.plans["free"].llm_usd_month
    assert quota.plans["pro"].analysis_calls_month > quota.plans["free"].analysis_calls_month


def test_a_missing_plan_fails_to_load(tmp_path: Path) -> None:
    payload = {"quota": {"free": {"llm_usd_month": 0.5, "analysis_calls_month": 20}}}
    with pytest.raises(ConfigError, match="pro"):
        load_quota(write(tmp_path, "quota.yaml", payload))


def test_a_negative_ceiling_fails_to_load(tmp_path: Path) -> None:
    payload = {
        "quota": {
            "free": {"llm_usd_month": -1, "analysis_calls_month": 20},
            "pro": {"llm_usd_month": 6.0, "analysis_calls_month": 400},
            "trial": {"llm_usd_month": 6.0, "analysis_calls_month": 400},
        }
    }
    with pytest.raises(ConfigError):
        load_quota(write(tmp_path, "quota.yaml", payload))


# --------------------------------------------------------------------------- #
# rag.yaml
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def rag() -> RagConfig:
    return load_rag(CONFIG_DIR / "rag.yaml")


def test_the_rag_configuration_matches_the_spec(rag: RagConfig) -> None:
    assert rag.embeddings.dimensions == 1024
    assert rag.qdrant.distance == "Cosine"
    assert rag.retrieval.top_k == 8
    assert rag.retrieval.score_threshold == pytest.approx(0.62)
    assert rag.retrieval.rerank is False  # phase 1.3 adds the cross-encoder


def test_an_out_of_range_threshold_fails_to_load(tmp_path: Path) -> None:
    payload = {
        "embeddings": {"provider": "openai", "model": "m", "dimensions": 1024},
        "qdrant": {"distance": "Cosine"},
        "retrieval": {"top_k": 8, "score_threshold": 1.4, "rerank": False},
    }
    with pytest.raises(ConfigError):
        load_rag(write(tmp_path, "rag.yaml", payload))


def test_a_zero_top_k_fails_to_load(tmp_path: Path) -> None:
    payload = {
        "embeddings": {"provider": "openai", "model": "m", "dimensions": 1024},
        "qdrant": {"distance": "Cosine"},
        "retrieval": {"top_k": 0, "score_threshold": 0.62, "rerank": False},
    }
    with pytest.raises(ConfigError):
        load_rag(write(tmp_path, "rag.yaml", payload))


# --------------------------------------------------------------------------- #
# Shared
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("loader", [load_models, load_quota, load_rag])
def test_a_missing_file_names_itself(tmp_path: Path, loader: object) -> None:
    with pytest.raises(ConfigError, match=r"absent\.yaml"):
        loader(tmp_path / "absent.yaml")  # type: ignore[operator]


@pytest.mark.parametrize("loader", [load_models, load_quota, load_rag])
def test_malformed_yaml_names_the_file(tmp_path: Path, loader: object) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("roles: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"broken\.yaml"):
        loader(path)  # type: ignore[operator]


# --------------------------------------------------------------------------- #
# The bundle: loaded from the configured directory, separately from secrets
# --------------------------------------------------------------------------- #


def test_the_bundle_loads_from_the_configured_directory() -> None:
    from fittrack.config import load_config

    bundle = load_config(CONFIG_DIR)
    assert set(bundle.models.roles) == set(LLMRole)
    assert bundle.quota.plans["free"].llm_usd_month > 0
    assert bundle.rag.retrieval.top_k > 0


def test_the_bundle_names_the_file_that_is_missing(tmp_path: Path) -> None:
    from fittrack.config import load_config

    write(tmp_path, "models.yaml", {"roles": full_roles()})
    with pytest.raises(ConfigError, match=r"quota\.yaml"):
        load_config(tmp_path)


def test_the_bundle_carries_no_secret() -> None:
    """Versioned configuration and credentials are separate on purpose.

    These files are committed and hot-reloadable (spec 7.2); a credential in one
    would be neither.
    """
    from fittrack.config import load_config

    blob = load_config(CONFIG_DIR).model_dump_json()
    for marker in ("api_key", "apikey", "token", "password", "secret"):
        assert marker not in blob.lower(), f"{marker!r} appears in versioned configuration"


# --------------------------------------------------------------------------- #
# An invalid override must fail at boot, not on the first request that hits it
# --------------------------------------------------------------------------- #


def test_an_override_with_an_invalid_field_fails_to_load(tmp_path: Path) -> None:
    payload = {
        "roles": full_roles(),
        "agents": {"proactive": {"role": "COACH", "primary": {"temperature": 99}}},
    }
    with pytest.raises(ConfigError, match="proactive"):
        load_models(write(tmp_path, "models.yaml", payload))


def test_an_override_with_an_unknown_provider_fails_to_load(tmp_path: Path) -> None:
    payload = {
        "roles": full_roles(),
        "agents": {"proactive": {"role": "COACH", "fallback": {"provider": "typo"}}},
    }
    with pytest.raises(ConfigError, match="proactive"):
        load_models(write(tmp_path, "models.yaml", payload))


def test_an_override_with_an_unknown_key_fails_to_load(tmp_path: Path) -> None:
    payload = {
        "roles": full_roles(),
        "agents": {"proactive": {"role": "COACH", "primary": {"temperatur": 0.1}}},
    }
    with pytest.raises(ConfigError, match="proactive"):
        load_models(write(tmp_path, "models.yaml", payload))


def test_a_deterministic_node_is_not_an_override_target() -> None:
    """`gamification` is pure SQL (spec 9.2) and invokes no LLM role.

    Accepting it would let `agents.gamification` pass boot validation while
    being dead configuration — the exact condition this registry rejects.
    """
    assert "gamification" not in KNOWN_AGENTS


def test_an_unknown_top_level_quota_key_fails_to_load(tmp_path: Path) -> None:
    """A misspelled section leaves the ceiling silently at its old value."""
    payload = {
        "quota": {
            "free": {"llm_usd_month": 0.5, "analysis_calls_month": 20},
            "pro": {"llm_usd_month": 6.0, "analysis_calls_month": 400},
            "trial": {"llm_usd_month": 6.0, "analysis_calls_month": 400},
        },
        "quotas": {"free": {"llm_usd_month": 99.0}},
    }
    with pytest.raises(ConfigError, match="quotas"):
        load_quota(write(tmp_path, "quota.yaml", payload))


# --------------------------------------------------------------------------- #
# Provider credentials
# --------------------------------------------------------------------------- #


def test_the_providers_a_configuration_needs_are_reported() -> None:
    from fittrack.config import configured_providers, load_config

    providers = configured_providers(load_config(CONFIG_DIR))
    assert providers == {"groq", "anthropic", "openai"}


def test_a_missing_provider_credential_is_named(tmp_path: Path) -> None:
    from fittrack.config import load_config, missing_provider_credentials

    config = load_config(CONFIG_DIR)
    assert missing_provider_credentials(config, {"groq": "key"}) == ["anthropic", "openai"]
    assert missing_provider_credentials(config, dict.fromkeys(configured, "k")) == []


configured = ("groq", "anthropic", "openai")


def test_a_duplicate_yaml_key_fails_to_load(tmp_path: Path) -> None:
    """`yaml.safe_load` keeps the last one and says nothing."""
    path = tmp_path / "quota.yaml"
    path.write_text(
        "quota:\n"
        "  free: {llm_usd_month: 0.5, analysis_calls_month: 20}\n"
        "  free: {llm_usd_month: 99.0, analysis_calls_month: 999}\n"
        "  pro: {llm_usd_month: 6.0, analysis_calls_month: 400}\n"
        "  trial: {llm_usd_month: 6.0, analysis_calls_month: 400}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="duplicate"):
        load_quota(path)


def test_an_infinite_ceiling_is_not_a_ceiling(tmp_path: Path) -> None:
    payload = {
        "quota": {
            "free": {"llm_usd_month": float("inf"), "analysis_calls_month": 20},
            "pro": {"llm_usd_month": 6.0, "analysis_calls_month": 400},
            "trial": {"llm_usd_month": 6.0, "analysis_calls_month": 400},
        }
    }
    with pytest.raises(ConfigError):
        load_quota(write(tmp_path, "quota.yaml", payload))


def test_a_blank_model_identifier_fails_to_load(tmp_path: Path) -> None:
    roles = full_roles()
    roles["ANALYST"]["fallback"]["model"] = "   "  # type: ignore[index]
    with pytest.raises(ConfigError):
        load_models(write(tmp_path, "models.yaml", {"roles": roles}))


def test_a_fallback_sharing_the_primary_provider_fails_to_load(tmp_path: Path) -> None:
    """Spec 7.3: a retry that shares the outage is not a failover."""
    roles = full_roles()
    roles["ANALYST"]["fallback"]["provider"] = "groq"  # type: ignore[index]
    with pytest.raises(ConfigError, match="ANALYST"):
        load_models(write(tmp_path, "models.yaml", {"roles": roles}))


def test_the_committed_configuration_has_distinct_providers(models: ModelsConfig) -> None:
    for role, config in models.roles.items():
        if config.primary is not None:
            assert config.primary.provider != config.fallback.provider, role


def test_an_override_cannot_collapse_the_failover(tmp_path: Path) -> None:
    """The rule has to hold after the merge, not only before it."""
    payload = {
        "roles": full_roles(),
        "agents": {"proactive": {"role": "COACH", "fallback": {"provider": "groq"}}},
    }
    with pytest.raises(ConfigError, match="proactive"):
        load_models(write(tmp_path, "models.yaml", payload))


def test_an_override_provider_needs_its_own_credential(tmp_path: Path) -> None:
    """Otherwise the deployment reads as fully credentialed and fails on first use."""
    from fittrack.config import Config, configured_providers, load_quota, load_rag

    payload = {
        "roles": full_roles(),
        "agents": {"proactive": {"role": "COACH", "primary": {"provider": "xai"}}},
    }
    config = Config(
        models=load_models(write(tmp_path, "models.yaml", payload)),
        quota=load_quota(CONFIG_DIR / "quota.yaml"),
        rag=load_rag(CONFIG_DIR / "rag.yaml"),
    )
    assert "xai" in configured_providers(config)


def test_a_blank_embedding_model_fails_to_load(tmp_path: Path) -> None:
    payload = {
        "embeddings": {"provider": "openai", "model": "  ", "dimensions": 1024},
        "qdrant": {"distance": "Cosine"},
        "retrieval": {"top_k": 8, "score_threshold": 0.62, "rerank": False},
    }
    with pytest.raises(ConfigError):
        load_rag(write(tmp_path, "rag.yaml", payload))
