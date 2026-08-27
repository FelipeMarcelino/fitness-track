"""End-to-end behaviour of `evals/run_judge.py`, with no provider credentials.

The replay backend reads verdicts from a file, so the whole CI contract —
exit codes, calibration discard, missing credentials — is exercised offline.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.judge.datasets import BASELINE, CALIBRATION, load_cases
from evals.run_judge import main

GOOD = {"safety": 5, "numeric_fidelity": 5, "profile_adherence": 5, "grounding": 5, "persona": 5}


def write_verdicts(path: Path, overrides: dict[str, dict[str, int]] | None = None) -> Path:
    """A verdict file where the judge agrees with every human label."""
    overrides = overrides or {}
    lines = []
    for case in load_cases(CALIBRATION) + load_cases(BASELINE):
        scores = dict(GOOD)
        if case.label == "bad":
            scores |= {name: score for name, score in case.human_scores.items() if score < 5}
        scores |= overrides.get(case.id, {})
        lines.append(
            json.dumps(
                {
                    "case_id": case.id,
                    "scores": {
                        n: {"rubric": n, "score": s, "justification": "replay"}
                        for n, s in scores.items()
                    },
                }
            )
        )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def run(path: Path, *extra: str) -> int:
    return main(["--backend", "replay", "--verdicts", str(path), *extra])


def test_a_calibrated_clean_round_passes(tmp_path: Path) -> None:
    assert run(write_verdicts(tmp_path / "v.jsonl")) == 0


def test_a_safety_failure_in_the_sample_fails_the_round(tmp_path: Path) -> None:
    path = write_verdicts(tmp_path / "v.jsonl", {"base-001": {"safety": 1}})
    assert run(path) == 1


def test_a_numeric_fidelity_failure_fails_the_round(tmp_path: Path) -> None:
    path = write_verdicts(tmp_path / "v.jsonl", {"base-014": {"numeric_fidelity": 3}})
    assert run(path) == 1


def test_a_trend_rubric_failure_does_not_fail_the_round(tmp_path: Path) -> None:
    path = write_verdicts(tmp_path / "v.jsonl", {"base-001": {"persona": 1}})
    assert run(path) == 0


def test_an_uncalibrated_judge_reports_without_failing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Three disagreements on the calibration set discards the round even though
    # the sample carries a real safety failure.
    overrides = {f"cal-g0{i}": {"safety": 1} for i in (1, 2, 3)}
    overrides["base-001"] = {"safety": 1}
    path = write_verdicts(tmp_path / "v.jsonl", overrides)
    assert run(path) == 0
    assert "judge nao calibrado" in capsys.readouterr().out.lower()


def test_a_verdict_missing_from_the_replay_file_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "v.jsonl"
    path.write_text("", encoding="utf-8")
    with pytest.raises(KeyError):
        run(path)


def test_missing_credentials_report_instead_of_passing_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert main(["--backend", "auto"]) == 0
    out = capsys.readouterr().out
    assert "nao executado" in out.lower()
    assert "ANTHROPIC_API_KEY" in out


def test_verdicts_can_be_written_back_for_replay(tmp_path: Path) -> None:
    source = write_verdicts(tmp_path / "v.jsonl")
    out = tmp_path / "out.jsonl"
    assert run(source, "--out", str(out)) == 0
    written = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line]
    assert len(written) == len(load_cases(BASELINE)) + len(load_cases(CALIBRATION))


# --------------------------------------------------------------------------- #
# Credentials: a check that did not run must not read as a check that passed
# --------------------------------------------------------------------------- #


def test_the_strict_backend_fails_when_the_credential_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What CI uses. A green required check would claim the judge scored the diff."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert main(["--backend", "anthropic"]) == 1


def test_the_strict_backend_says_which_variable_is_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    main(["--backend", "anthropic"])
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_the_runner_gates_each_case_on_its_declared_rubrics(tmp_path: Path) -> None:
    """A baseline case that declares no `grounding` must not be gated or trended on it."""
    baseline = load_cases(BASELINE)
    narrow = next(c for c in baseline if "grounding" not in c.rubrics)
    path = write_verdicts(tmp_path / "v.jsonl", {narrow.id: {"grounding": 1}})
    assert run(path) == 0


def test_a_replayed_verdict_missing_a_rubric_is_rejected(tmp_path: Path) -> None:
    """The replay path validates like the live one, or the two disagree.

    An older recording that predates a rubric would otherwise pass while that
    rubric silently vanished from the trend — and, for a blocking rubric, while
    the gate stopped gating.
    """
    path = write_verdicts(tmp_path / "v.jsonl")
    lines = path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    del first["scores"]["persona"]
    lines[0] = json.dumps(first)
    path.write_text("\n".join(lines), encoding="utf-8")
    with pytest.raises(ValueError, match="persona"):
        run(path)


def test_an_undersized_calibration_set_is_refused(tmp_path: Path) -> None:
    """Two of two agreeing is not calibration; it is a coincidence."""
    import shutil

    small = tmp_path / "calibration.jsonl"
    lines = CALIBRATION.read_text(encoding="utf-8").splitlines()[:2]
    small.write_text("\n".join(lines) + "\n", encoding="utf-8")
    shutil.copy(BASELINE, tmp_path / "baseline.jsonl")

    verdicts = write_verdicts(tmp_path / "v.jsonl")
    with pytest.raises(ValueError, match="20"):
        main(
            [
                "--backend",
                "replay",
                "--verdicts",
                str(verdicts),
                "--calibration",
                str(small),
            ]
        )


def test_an_empty_calibration_set_is_refused(tmp_path: Path) -> None:
    empty = tmp_path / "calibration.jsonl"
    empty.write_text("", encoding="utf-8")
    verdicts = write_verdicts(tmp_path / "v.jsonl")
    with pytest.raises(ValueError, match="20"):
        main(["--backend", "replay", "--verdicts", str(verdicts), "--calibration", str(empty)])
