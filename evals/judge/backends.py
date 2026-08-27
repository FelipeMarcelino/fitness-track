"""Where a verdict comes from: a live provider, or a recorded file."""

from __future__ import annotations

import json
import os
import secrets
import textwrap
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

import yaml
from pydantic import ValidationError as PydanticValidationError

from evals.judge.models import CaseVerdict, JudgeCase, RubricScore
from evals.judge.rubrics import Rubric

if TYPE_CHECKING:
    from anthropic.types import ToolChoiceToolParam, ToolParam

CREDENTIAL_ENV = "ANTHROPIC_API_KEY"

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
MODELS_FILE = CONFIG_DIR / "models.yaml"
PROMPT_FILE = CONFIG_DIR / "prompts" / "judge.md"

NONCE_BYTES = 8


class MissingCredentialsError(RuntimeError):
    """No provider credential is available for a live judge run."""


def judge_model(models_file: Path | None = None) -> str:
    """The model of the `JUDGE` role, from `config/models.yaml`.

    Model names never appear in code (CLAUDE.md, invariant 4). One hard-coded
    here would survive a rotation performed through the configured path and keep
    calling a retired identifier — or, worse, evaluate with a model the
    configuration says is no longer the judge.

    The role has no primary by design: a judge sharing a failure mode with the
    model under evaluation is not a judge (spec 7.2), so the fallback *is* it.
    """
    path = models_file or MODELS_FILE
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    try:
        return str(config["roles"]["JUDGE"]["fallback"]["model"])
    except (KeyError, TypeError) as error:
        raise KeyError(f"no JUDGE role configured in {path}") from error


class JudgeBackend(Protocol):
    """Scores one case against the rubrics it is gated on."""

    name: str

    def score(self, case: JudgeCase, rubrics: dict[str, Rubric]) -> CaseVerdict: ...


# --------------------------------------------------------------------------- #
# Prompt and case rendering
# --------------------------------------------------------------------------- #


def build_system_prompt(nonce: str) -> str:
    """The judge's instructions, naming the delimiter this call will actually use.

    Loaded from `config/prompts/judge.md`: prompts are versioned content, not
    Python strings (CLAUDE.md, Convenções). Keeping the scoring policy inside
    the backend would make a change to it inseparable from provider code.
    """
    return PROMPT_FILE.read_text(encoding="utf-8").replace("{nonce}", nonce)


def _render_rubrics(rubrics: Mapping[str, Rubric]) -> str:
    blocks = []
    for rubric in rubrics.values():
        scale = "\n".join(f"  {level}. {text}" for level, text in sorted(rubric.scale.items()))
        blocks.append(f"### {rubric.id} — {rubric.title}\n{rubric.criterion}\n\nEscala:\n{scale}")
    return "\n\n".join(blocks)


def _neutralise(text: str, nonce: str) -> str:
    """Make untrusted text unable to close the block that contains it.

    The nonce is unguessable, so the only way a field could carry the closing
    marker is by chance or by having been shown one. Stripping it costs nothing:
    the text stays readable, which is what the judge has to grade.
    """
    return text.replace(f"</case-{nonce}>", "[marca removida]").replace(
        f"<case-{nonce}>", "[marca removida]"
    )


def build_case_block(case: JudgeCase, nonce: str | None = None) -> tuple[str, str]:
    """Render one case as a delimited data block, and the nonce that delimits it."""
    nonce = nonce or secrets.token_hex(NONCE_BYTES).upper()
    tools = json.dumps([t.model_dump() for t in case.tool_results], ensure_ascii=False, indent=2)
    retrieved = "\n".join(f"- {chunk}" for chunk in case.retrieved) or "(nada recuperado)"
    body = textwrap.dedent(
        f"""\
        Perfil: {json.dumps(case.profile, ensure_ascii=False)}
        Contexto: {json.dumps(case.context, ensure_ascii=False)}

        Mensagem do usuário:
        {case.user_message}

        Resultados de tool disponíveis (única origem legítima de medida):
        {tools}

        Trechos recuperados do RAG:
        {retrieved}

        Resposta do assistente a ser avaliada:
        {case.response}"""
    )
    return f"<case-{nonce}>\n{_neutralise(body, nonce)}\n</case-{nonce}>", nonce


def verdict_from_payload(
    case_id: str, payload: Mapping[str, Any], rubrics: Mapping[str, Rubric]
) -> CaseVerdict:
    """Validate the provider's answer against the rubrics that were requested.

    A tool schema guides a model; it does not guarantee its output. Silently
    dropping a rubric the provider omitted would let a round report approval
    while `persona` or `grounding` quietly vanished from the trend — and, for a
    blocking rubric, while the gate stopped being a gate.
    """
    missing = sorted(set(rubrics) - set(payload))
    if missing:
        raise ValueError(f"case {case_id!r}: judge omitted rubric(s) {', '.join(missing)}")
    try:
        return CaseVerdict(
            case_id=case_id,
            scores={
                name: RubricScore.model_validate(
                    {"rubric": name, **dict(payload[name])}  # raw score, strictly validated
                )
                for name in rubrics
            },
        )
    except PydanticValidationError as error:
        raise ValueError(f"case {case_id!r}: malformed judge scores: {error}") from error


def require_complete(verdict: CaseVerdict, rubrics: Mapping[str, Rubric]) -> CaseVerdict:
    """A verdict must carry every rubric it was asked for, whatever its source.

    The replay path needs this as much as the live one: a recording made before
    a rubric existed would otherwise pass while that rubric silently vanished
    from the trend, and a blocking one would stop gating.
    """
    missing = sorted(set(rubrics) - set(verdict.scores))
    if missing:
        raise ValueError(
            f"case {verdict.case_id!r}: recorded verdict omits rubric(s) {', '.join(missing)}"
        )
    return verdict


# --------------------------------------------------------------------------- #
# Replay
# --------------------------------------------------------------------------- #


class ReplayBackend:
    """Serves verdicts recorded by an earlier run.

    This is what makes the CI policy testable without a network: the gates see
    exactly the shape they would see from a live judge.
    """

    name = "replay"

    def __init__(self, verdicts: Iterable[CaseVerdict]) -> None:
        self._by_case = {verdict.case_id: verdict for verdict in verdicts}

    @classmethod
    def from_file(cls, path: Path) -> ReplayBackend:
        verdicts = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                verdicts.append(CaseVerdict.model_validate(json.loads(line)))
        return cls(verdicts)

    def score(self, case: JudgeCase, rubrics: dict[str, Rubric]) -> CaseVerdict:
        try:
            recorded = self._by_case[case.id]
        except KeyError as error:
            raise KeyError(f"no recorded verdict for case {case.id!r}") from error
        return require_complete(recorded, rubrics)


# --------------------------------------------------------------------------- #
# Anthropic
# --------------------------------------------------------------------------- #


class AnthropicBackend:
    """Live judge (spec 21.2). Requires ANTHROPIC_API_KEY."""

    name = "anthropic"

    def __init__(self, model: str | None = None, api_key: str | None = None) -> None:
        key = api_key or os.environ.get(CREDENTIAL_ENV)
        if not key:
            raise MissingCredentialsError(CREDENTIAL_ENV)
        import anthropic  # imported lazily so the offline path needs no SDK call

        self.model = model or judge_model()
        self._client = anthropic.Anthropic(api_key=key)

    def _tool_schema(self, rubrics: Mapping[str, Rubric]) -> dict[str, Any]:
        properties = {
            name: {
                "type": "object",
                "properties": {
                    "score": {"type": "integer", "minimum": 1, "maximum": 5},
                    "justification": {"type": "string"},
                },
                "required": ["score", "justification"],
            }
            for name in rubrics
        }
        return {
            "name": "record_scores",
            "description": "Registra a nota e a justificativa de cada rubrica.",
            "input_schema": {
                "type": "object",
                "properties": properties,
                "required": list(rubrics),
            },
        }

    def score(self, case: JudgeCase, rubrics: dict[str, Rubric]) -> CaseVerdict:
        tool = self._tool_schema(rubrics)
        block, nonce = build_case_block(case)
        message = self._client.messages.create(
            model=self.model,
            max_tokens=2048,
            system=build_system_prompt(nonce),
            tools=[cast("ToolParam", tool)],
            tool_choice=cast("ToolChoiceToolParam", {"type": "tool", "name": tool["name"]}),
            messages=[
                {
                    "role": "user",
                    "content": f"Rubricas:\n\n{_render_rubrics(rubrics)}\n\n{block}",
                }
            ],
        )
        raw = next((part.input for part in message.content if part.type == "tool_use"), None)
        if not isinstance(raw, dict):
            raise ValueError(f"judge returned no structured scores for case {case.id!r}")
        return verdict_from_payload(case.id, raw, rubrics)
