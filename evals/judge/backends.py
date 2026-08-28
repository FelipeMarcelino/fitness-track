"""Where a verdict comes from: a live provider, or a recorded file."""

from __future__ import annotations

import json
import os
import secrets
import textwrap
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

import yaml
from pydantic import ValidationError as PydanticValidationError

from evals.judge.models import CaseVerdict, JudgeCase, RubricScore
from evals.judge.rubrics import Rubric

if TYPE_CHECKING:
    from openai.types.responses import ResponseTextConfigParam
    from openai.types.shared_params import Reasoning

CREDENTIAL_ENV = "OPENAI_API_KEY"

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
MODELS_FILE = CONFIG_DIR / "models.yaml"
PROMPT_FILE = CONFIG_DIR / "prompts" / "judge.md"

NONCE_BYTES = 8
# Responses counts hidden reasoning and visible JSON against the same ceiling.
# The judge uses high effort, so the old 2,048 visible-token budget could end
# before a complete structured verdict was emitted.
MAX_OUTPUT_TOKENS = 16_384
ReasoningEffort = Literal["low", "medium", "high"]


class MissingCredentialsError(RuntimeError):
    """No provider credential is available for a live judge run."""


@dataclass(frozen=True, slots=True)
class JudgeModelConfig:
    """The provider-specific settings of the configured judge role."""

    provider: str
    model: str
    reasoning_effort: ReasoningEffort


def judge_config(models_file: Path | None = None) -> JudgeModelConfig:
    """Resolve the live judge settings from `config/models.yaml`."""
    path = models_file or MODELS_FILE
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    try:
        fallback = config["roles"]["JUDGE"]["fallback"]
        reasoning_effort = fallback["reasoning_effort"]
        if reasoning_effort not in ("low", "medium", "high"):
            raise ValueError(f"invalid JUDGE reasoning_effort in {path}: {reasoning_effort!r}")
        return JudgeModelConfig(
            provider=str(fallback["provider"]),
            model=str(fallback["model"]),
            reasoning_effort=cast("ReasoningEffort", reasoning_effort),
        )
    except (KeyError, TypeError) as error:
        raise KeyError(f"no complete JUDGE role configured in {path}") from error


def judge_model(models_file: Path | None = None) -> str:
    """The model of the `JUDGE` role, from `config/models.yaml`.

    Model names never appear in code (CLAUDE.md, invariant 4). One hard-coded
    here would survive a rotation performed through the configured path and keep
    calling a retired identifier — or, worse, evaluate with a model the
    configuration says is no longer the judge.

    The role has no primary by design: a judge sharing a failure mode with the
    model under evaluation is not a judge (spec 7.2), so the fallback *is* it.
    """
    return judge_config(models_file).model


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
# OpenAI
# --------------------------------------------------------------------------- #


class OpenAIBackend:
    """Live judge (spec 21.2). Requires OPENAI_API_KEY."""

    name = "openai"

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        reasoning_effort: ReasoningEffort | None = None,
    ) -> None:
        key = api_key or os.environ.get(CREDENTIAL_ENV)
        if not key:
            raise MissingCredentialsError(CREDENTIAL_ENV)
        import openai  # imported lazily so the offline path needs no SDK call

        configured = judge_config()
        if configured.provider != self.name:
            raise ValueError(
                f"JUDGE is configured for provider {configured.provider!r}, not {self.name!r}"
            )
        self.model = model or configured.model
        self.reasoning_effort = reasoning_effort or configured.reasoning_effort
        self._client = openai.OpenAI(api_key=key)

    def _response_format(self, rubrics: Mapping[str, Rubric]) -> dict[str, Any]:
        properties = {
            name: {
                "type": "object",
                "properties": {
                    "score": {"type": "integer", "minimum": 1, "maximum": 5},
                    "justification": {"type": "string"},
                },
                "required": ["score", "justification"],
                "additionalProperties": False,
            }
            for name in rubrics
        }
        return {
            "name": "record_scores",
            "type": "json_schema",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": properties,
                "required": list(rubrics),
                "additionalProperties": False,
            },
        }

    def score(self, case: JudgeCase, rubrics: dict[str, Rubric]) -> CaseVerdict:
        block, nonce = build_case_block(case)
        response = self._client.responses.create(
            model=self.model,
            instructions=build_system_prompt(nonce),
            input=f"Rubricas:\n\n{_render_rubrics(rubrics)}\n\n{block}",
            max_output_tokens=MAX_OUTPUT_TOKENS,
            reasoning=cast("Reasoning", {"effort": self.reasoning_effort}),
            text=cast(
                "ResponseTextConfigParam",
                {"format": self._response_format(rubrics)},
            ),
            store=False,
        )
        try:
            raw = json.loads(response.output_text)
        except (json.JSONDecodeError, TypeError) as error:
            raise ValueError(f"case {case.id!r}: judge did not return valid JSON") from error
        if not isinstance(raw, dict):
            raise ValueError(f"case {case.id!r}: judge JSON is not an object")
        return verdict_from_payload(case.id, raw, rubrics)
