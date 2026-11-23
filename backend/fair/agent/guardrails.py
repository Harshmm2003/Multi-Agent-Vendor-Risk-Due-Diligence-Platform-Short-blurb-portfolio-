"""
Guardrails on the agent.

1. Budget: model calls, tool calls and wall time are capped.
2. Sources: an input may only cite evidence or benchmarks that exist.
3. Numbers: the model never supplies a dollar figure the report shows, and
   any dollar figure it writes in its narrative must be one a tool produced
   (compared through format_usd, the same formatting the tools use).
"""

import re
from dataclasses import dataclass

from fair import benchmarks
from fair.agent.session import Session
from fair.format import format_usd
from fair.model.simulation import LossDistribution, SimulationResult, combine
from fair.model.taxonomy import ScenarioInputs


@dataclass(frozen=True)
class Budget:
    max_llm_calls: int = 12
    max_tool_calls: int = 24
    max_seconds: float = 300.0  # free-tier Groq allows ~8K tokens/minute
    max_total_tokens: int = 60_000  # input + output, summed over all calls


class GuardrailError(Exception):
    """A rule was broken; the message goes back to the model to fix it."""


def check_sources(inputs: ScenarioInputs, session: Session) -> None:
    bad = []
    for path, r in inputs.ranges():
        for sid in r.source_ids:
            if sid.startswith("ev_") and not session.evidence.has(sid):
                bad.append(f"{path}: evidence {sid} does not exist")
            elif sid.startswith("benchmark:") and not benchmarks.exists(sid):
                bad.append(f"{path}: {sid} is not a known benchmark")
    if bad:
        raise GuardrailError("unknown sources: " + "; ".join(bad))


def check_not_double_counted(inputs: ScenarioInputs) -> None:
    """
    A frequency benchmark of loss events already includes vulnerability.
    Lowering vulnerability as well counts the same evidence twice.
    """
    tef, vuln = inputs.threat_event_frequency, inputs.vulnerability
    loss_event_ids = [s for s in tef.source_ids if benchmarks.measures_loss_events(s)]
    if loss_event_ids and vuln.min < 1:
        raise GuardrailError(
            f"threat_event_frequency cites {loss_event_ids[0]}, which counts loss events, so "
            "vulnerability must stay fixed at 1.0. Put the effect of the evidence "
            "into threat_event_frequency instead."
        )


# $950, $1,200, $12.3K, $1.25M, -$50K ...
_DOLLAR = re.compile(r"-?\$\s?\d[\d,]*(?:\.\d+)?\s?[KMBT]?(?![A-Za-z])")
_SUFFIX = {"": 1.0, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


def parse_usd(text: str) -> float:
    t = text.replace("$", "").replace(",", "").replace(" ", "")
    negative = t.startswith("-")
    t = t.lstrip("-")
    suffix = t[-1] if t and t[-1] in "KMBT" else ""
    value = float(t[: len(t) - len(suffix)]) * _SUFFIX[suffix]
    return -value if negative else value


def _distribution_figures(d: LossDistribution) -> list[float]:
    return [d.ale_mean, d.p10, d.p50, d.p90, d.p95, d.p99]


def allowed_values(session: Session, final: list[SimulationResult]) -> list[float]:
    """Every dollar amount a tool has shown: the only amounts the narrative may use."""
    values: list[float] = []
    for r in session.ledger.results():
        values += _distribution_figures(r.distribution)
        values += r.breakdown.values()
        values += [
            v for _, rng in r.inputs.ranges() for v in (rng.min, rng.mode, rng.max)
        ]
    if final:
        values += _distribution_figures(combine(final).distribution)
    tol = session.context.tolerance()
    if tol is not None:
        values.append(tol.value_usd)
    ctx = session.context
    for v in (
        ctx.annual_contract_value,
        ctx.exposure.liability_cap_usd,
        ctx.exposure.cyber_insurance_usd,
        ctx.exposure.revenue_at_risk_per_hour,
    ):
        if v is not None:
            values.append(v)
    values += [
        b.value
        for b in benchmarks.all_benchmarks().values()
        if b.unit.startswith("usd")
    ]
    return [v for v in values if v > 0]


def _digits_and_half_unit(text: str) -> tuple[int, float]:
    """Significant digits written, and half the value of the last digit, in dollars."""
    t = text.replace("$", "").replace(",", "").replace(" ", "").lstrip("-")
    suffix = t[-1] if t and t[-1] in "KMBT" else ""
    number = t[: len(t) - len(suffix)]
    decimals = len(number.split(".")[1]) if "." in number else 0
    digits = len(number.replace(".", "").lstrip("0"))
    return digits, 0.5 * 10.0**-decimals * _SUFFIX[suffix]


def figure_matches(text: str, values: list[float]) -> bool:
    """
    True when text is a correct rounding of a real value to at least two
    significant figures ("$4.452M", "$1.4M" from 1.36M), or exactly a value's
    standard rendering ("$950", "$2M" from 2,000,000). "$2K" for $1.5K is not:
    one significant figure can move a number by a third.
    """
    x = abs(parse_usd(text))
    if any(format_usd(v) == format_usd(x) for v in values):
        return True
    digits, half_unit = _digits_and_half_unit(text)
    if digits < 2:
        return False
    return any(abs(v - x) <= half_unit * (1 + 1e-9) for v in values)


def nearest(text: str, values: list[float]) -> str:
    x = abs(parse_usd(text))
    return format_usd(min(values, key=lambda v: abs(v - x))) if values else "none"


def check_submission(
    session: Session,
    final_run_ids: list[str],
    texts: list[str],
    recommended_control_runs: list[str] | None = None,
) -> list[SimulationResult]:
    """The final runs, if the submission passes; otherwise GuardrailError."""
    problems = []
    bad_controls = [
        rid
        for rid in recommended_control_runs or []
        if not session.ledger.has(rid)
        or session.ledger.meta(rid).kind != "control"
        or session.ledger.meta(rid).base_run_id not in final_run_ids
    ]
    if bad_controls:
        problems.append(
            f"recommended_control_runs {', '.join(bad_controls)} are not test_control "
            "runs of the final scenario runs"
        )
    if not final_run_ids:
        problems.append("final_run_ids is empty: give one run per scenario")
    unknown = [rid for rid in final_run_ids if not session.ledger.has(rid)]
    if unknown:
        problems.append(f"unknown run ids: {', '.join(unknown)}")
    not_scenario = [
        rid
        for rid in final_run_ids
        if session.ledger.has(rid) and session.ledger.meta(rid).kind != "scenario"
    ]
    if not_scenario:
        problems.append(
            f"{', '.join(not_scenario)} are control what-ifs, not the scenario as it "
            "stands; name the scenario runs as final and controls in "
            "recommended_control_runs"
        )
    final = [
        session.ledger.get(rid) for rid in final_run_ids if session.ledger.has(rid)
    ]

    scenarios = [r.inputs.scenario for r in final]
    dupes = sorted({str(s) for s in scenarios if scenarios.count(s) > 1})
    if dupes:
        problems.append(
            f"more than one final run for {', '.join(dupes)}: pick one per scenario, "
            "or their losses would be counted twice"
        )

    values = allowed_values(session, final)
    invented = sorted(
        {
            m.group().strip()
            for text in texts
            for m in _DOLLAR.finditer(text)
            if not figure_matches(m.group(), values)
        }
    )
    if invented:
        hints = ", ".join(
            f"{f} (nearest tool figure: {nearest(f, values)})" for f in invented
        )
        problems.append(
            f"dollar figures not produced by any tool: {hints}. Use figures as tools "
            "show them (rounding to 2+ significant figures is fine), or leave them out."
        )
    if problems:
        raise GuardrailError("; ".join(problems))
    return final
