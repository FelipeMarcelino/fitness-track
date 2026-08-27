"""Human-readable rendering of a judge round."""

from __future__ import annotations

from evals.judge.gates import RunReport

RULE = "-" * 78


def render(report: RunReport) -> str:
    lines = [RULE, f"LLM-as-judge — fase {report.phase}, {report.scored_cases} casos", RULE]

    calibration = report.calibration
    if calibration is not None:
        errors = len(calibration.mismatches)
        status = "calibrado" if calibration.calibrated else "NAO CALIBRADO"
        lines.append(
            f"Calibracao: {calibration.total - errors}/{calibration.total} de acordo "
            f"com o rotulo humano ({errors} erro(s), teto {calibration.max_errors}) — {status}"
        )
        if calibration.mismatches:
            lines.append(f"  divergencias: {', '.join(sorted(calibration.mismatches))}")

    if report.trends:
        lines.append("")
        lines.append("Tendencia (nao bloqueia; queda >0.5 em 3 rodadas abre issue):")
        for name, value in sorted(report.trends.items()):
            lines.append(f"  {name:<20} {value:.2f}")

    lines.append("")
    if report.blocking_failures:
        verb = "ignoradas (rodada descartada)" if report.discarded else "BLOQUEIAM o merge"
        lines.append(f"Falhas em rubrica bloqueante — {verb}:")
        lines.extend(f"  {failure}" for failure in report.blocking_failures)
    else:
        lines.append("Nenhuma falha em rubrica bloqueante.")

    lines.append(RULE)
    if report.discarded:
        lines.append(
            "RESULTADO: judge nao calibrado — rodada descartada, PR nao reprovada (§21.2)."
        )
    elif report.blocking_failures:
        lines.append("RESULTADO: reprovado.")
    else:
        lines.append("RESULTADO: aprovado.")
    return "\n".join(lines)
