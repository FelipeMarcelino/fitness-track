"""The judge obeys the two configuration invariants of CLAUDE.md.

Invariant 4: model names never appear in code — they live in
`config/models.yaml`, resolved by role.
Convention: prompts live in `config/prompts/*.md`, versioned, never embedded in
a Python string.

Both were violated by the first cut of this runner. A hard-coded judge model
survives a model rotation done through the configured path and keeps calling a
retired identifier; an embedded prompt makes a change to the scoring policy
inseparable from provider code.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
MODELS_YAML = ROOT / "config" / "models.yaml"
JUDGE_PROMPT = ROOT / "config" / "prompts" / "judge.md"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

# The role table of spec 7.2.
ROLES = {
    "NORMALIZER",
    "ROUTER",
    "EXTRACTOR",
    "RESOLVER",
    "VOICE",
    "GUARDRAIL",
    "SUMMARY",
    "ANALYST",
    "COACH",
    "JUDGE",
}


@pytest.fixture(scope="module")
def models() -> dict[str, Any]:
    parsed: dict[str, Any] = yaml.safe_load(MODELS_YAML.read_text(encoding="utf-8"))
    return parsed


def ci_workflow() -> str:
    """Read the repository workflow when tests run from a source checkout.

    The production image intentionally does not copy `.github`; the Quality job
    proves this contract before Integration builds that image.
    """
    source_checkout = CI_WORKFLOW.is_file()
    skip_reason = "CI workflow is not included in the production image"
    pytest.skip(skip_reason) if not source_checkout else None
    return CI_WORKFLOW.read_text(encoding="utf-8")


def test_every_role_of_the_spec_is_configured(models: dict[str, Any]) -> None:
    assert set(models["roles"]) == ROLES


def test_the_judge_role_has_no_primary(models: dict[str, Any]) -> None:
    """Spec 7.2: a judge running on the model that produced the answer is not a judge."""
    judge = models["roles"]["JUDGE"]
    assert judge.get("primary") is None
    assert judge["fallback"]["provider"] == "openai"
    assert judge["fallback"]["reasoning_effort"] == "high"


def test_no_model_name_appears_in_python() -> None:
    """The invariant, enforced rather than asserted in a comment."""
    pattern = re.compile(r"claude-[a-z0-9.-]+|gpt-oss-[0-9]+b|openai/gpt", re.IGNORECASE)
    offenders: list[str] = []
    for directory in ("src", "evals", "scripts"):
        for path in (ROOT / directory).rglob("*.py"):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    assert not offenders, "model names belong in config/models.yaml:\n" + "\n".join(offenders)


def test_the_judge_model_is_resolved_from_configuration() -> None:
    from evals.judge.backends import judge_model

    configured = yaml.safe_load(MODELS_YAML.read_text(encoding="utf-8"))
    assert judge_model() == configured["roles"]["JUDGE"]["fallback"]["model"]


def test_an_absent_judge_role_is_an_error(tmp_path: Path) -> None:
    from evals.judge.backends import judge_model

    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump({"roles": {"ANALYST": {}}}), encoding="utf-8")
    with pytest.raises(KeyError, match="JUDGE"):
        judge_model(path)


def test_the_judge_prompt_is_a_versioned_file() -> None:
    assert JUDGE_PROMPT.is_file()
    assert JUDGE_PROMPT.read_text(encoding="utf-8").strip()


def test_the_prompt_file_carries_the_nonce_placeholder() -> None:
    assert "{nonce}" in JUDGE_PROMPT.read_text(encoding="utf-8")


def test_no_prompt_is_embedded_in_the_backend() -> None:
    """Instruction text, not string formatting: the backend still renders the case block."""
    source = (ROOT / "evals" / "judge" / "backends.py").read_text(encoding="utf-8")
    instruction_shaped = ("Você é", "Pontue a resposta", "Regra de leitura", "Sobre números")
    found = [phrase for phrase in instruction_shaped if phrase in source]
    assert not found, f"prompt text in backends.py: {found} — it belongs in config/prompts/judge.md"


def test_the_backend_reads_the_prompt_from_the_configured_file() -> None:
    from evals.judge.backends import PROMPT_FILE, build_system_prompt

    assert PROMPT_FILE == JUDGE_PROMPT
    assert "Regra de leitura" in build_system_prompt("ABC123")


def test_ci_runs_the_openai_judge_as_a_real_gate() -> None:
    workflow = ci_workflow()
    assert "OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}" in workflow
    assert "python -m evals.run_judge --backend openai" in workflow
    assert "continue-on-error: true" not in workflow


def test_a_judge_model_change_triggers_the_ci_gate() -> None:
    workflow = ci_workflow()
    assert "config/models.yaml" in workflow
