"""
The FAIR report. Every number in it is copied from the run ledger by code;
the model contributes only which runs are final and the words around them.
"""

from typing import Literal

from pydantic import BaseModel

from fair.agent.session import RunMeta, Session, TraceStep
from fair.context import AnalysisContext, Tolerance
from fair.format import format_percent, format_usd
from fair.model.controls import CONTROLS, apply_control
from fair.model.scenarios import TEMPLATES
from fair.model.sensitivity import Driver, drivers
from fair.model.simulation import (
    LossDistribution,
    LossExceedancePoint,
    SimulationResult,
    combine,
)
from fair.ports import Evidence


class InputSummary(BaseModel):
    path: str
    min: float
    mode: float
    max: float
    source: str
    rationale: str


class LossSummary(BaseModel):
    ale_mean: float
    p10: float
    p50: float
    p90: float
    p95: float
    p99: float
    prob_any_loss: float
    prob_exceeds_tolerance: float | None
    loss_exceedance_curve: list[LossExceedancePoint]


class ScenarioSummary(BaseModel):
    run_id: str
    scenario: str
    title: str
    label: str
    loss_event_frequency_mean: float
    loss: LossSummary
    breakdown: dict[str, float]
    inputs: list[InputSummary]
    assumption_count: int
    drivers: list[Driver]  # inputs ranked by how much they move the expected loss


class ControlResult(BaseModel):
    """A what-if: one control applied to one scenario's final run."""

    run_id: str
    base_run_id: str
    scenario: str
    control_id: str
    title: str
    parameter: float | None
    tested_by: Literal["agent", "code"]
    recommended: bool
    ale_before: float
    ale_after: float
    p99_before: float
    p99_after: float
    combined_after: LossSummary  # all scenarios, with this control in place


class FairReport(BaseModel):
    subject: str
    context: AnalysisContext
    status: Literal["complete", "partial"]
    generated_by: Literal["agent", "fallback"]
    scenarios: list[ScenarioSummary]
    combined: LossSummary
    tolerance: Tolerance | None
    input_count: int
    assumption_count: int
    assumption_share: float
    missing_inputs: list[str]
    sharpen: list[str]  # facts that would most narrow the estimate, most useful first
    controls: list[ControlResult]
    narrative: str
    key_findings: list[str]
    recommendations: list[str]
    notes: list[str]
    evidence: list[Evidence]
    trace: list[TraceStep]
    llm_calls: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    paced_seconds: float


def loss_summary(dist: LossDistribution, tolerance: Tolerance | None) -> LossSummary:
    return LossSummary(
        ale_mean=dist.ale_mean,
        p10=dist.p10,
        p50=dist.p50,
        p90=dist.p90,
        p95=dist.p95,
        p99=dist.p99,
        prob_any_loss=dist.prob_any_loss,
        prob_exceeds_tolerance=(
            dist.prob_exceeds(tolerance.value_usd) if tolerance is not None else None
        ),
        loss_exceedance_curve=dist.loss_exceedance_curve,
    )


def assemble(
    session: Session,
    final: list[SimulationResult],
    *,
    status: Literal["complete", "partial"],
    generated_by: Literal["agent", "fallback"],
    narrative: str,
    key_findings: list[str] | None = None,
    recommendations: list[str] | None = None,
    notes: list[str] | None = None,
    recommended_control_runs: list[str] | None = None,
) -> FairReport:
    ctx = session.context
    tolerance = ctx.tolerance()
    scenarios = [_scenario_summary(r, tolerance) for r in final]
    controls = _controls(session, final, tolerance, set(recommended_control_runs or []))
    n_inputs = sum(len(s.inputs) for s in scenarios)
    n_assumed = sum(s.assumption_count for s in scenarios)
    combined = loss_summary(combine(final).distribution, tolerance)
    tail = tail_note(combined, tolerance)
    return FairReport(
        subject=ctx.subject,
        context=ctx,
        status=status,
        generated_by=generated_by,
        scenarios=scenarios,
        combined=combined,
        tolerance=tolerance,
        input_count=n_inputs,
        assumption_count=n_assumed,
        assumption_share=n_assumed / n_inputs if n_inputs else 1.0,
        missing_inputs=ctx.missing_inputs(),
        sharpen=_sharpen(ctx, scenarios),
        controls=controls,
        narrative=narrative,
        key_findings=key_findings or [],
        recommendations=recommendations or [],
        notes=[*(notes or []), *([tail] if tail else [])],
        evidence=session.evidence.items(),
        trace=session.trace,
        llm_calls=session.llm_calls,
        tool_calls=session.tool_calls,
        input_tokens=session.input_tokens,
        output_tokens=session.output_tokens,
        paced_seconds=session.paced_seconds,
    )


def tail_note(combined: LossSummary, tolerance: Tolerance | None) -> str | None:
    """
    Written in code, not by the model: when the expected loss sits within the
    tolerance but the 1-in-100-year loss does not, the reader must still see
    the tail, whatever the narrative says.
    """
    if (
        tolerance is None
        or tolerance.value_usd <= 0
        or combined.p99 <= tolerance.value_usd
    ):
        return None
    kind = "your" if tolerance.source == "client" else "the derived"
    multiple = combined.p99 / tolerance.value_usd
    within = combined.ale_mean < tolerance.value_usd
    return (
        f"Tail risk: a 1-in-100-year loss of {format_usd(combined.p99)} is "
        f"{multiple:.0f}× {kind} tolerance of {format_usd(tolerance.value_usd)}"
        + (", even though the expected annual loss is within it." if within else ".")
    )


def _scenario_summary(
    r: SimulationResult, tolerance: Tolerance | None
) -> ScenarioSummary:
    return ScenarioSummary(
        run_id=r.run_id,
        scenario=r.inputs.scenario,
        title=TEMPLATES[r.inputs.scenario].title,
        label=r.inputs.label,
        loss_event_frequency_mean=r.loss_event_frequency_mean,
        loss=loss_summary(r.distribution, tolerance),
        breakdown=r.breakdown,
        inputs=[
            InputSummary(
                path=path,
                min=rng.min,
                mode=rng.mode,
                max=rng.max,
                source=rng.source,
                rationale=rng.rationale,
            )
            for path, rng in r.inputs.ranges()
        ],
        assumption_count=r.inputs.assumption_count,
        drivers=drivers(r),
    )


def _controls(
    session: Session,
    final: list[SimulationResult],
    tolerance: Tolerance | None,
    recommended: set[str],
) -> list[ControlResult]:
    """
    Every control the agent tested on a final run, plus every catalog control
    it did not, tested here in code at its default setting (no model tokens).
    """
    ledger = session.ledger
    out = []
    for base in final:
        tested = {
            ledger.meta(r.run_id).control_id
            for r in ledger.control_runs_of(base.run_id)
        }
        for control in CONTROLS.values():
            if base.inputs.scenario in control.scenarios and control.id not in tested:
                ledger.run(
                    apply_control(control.id, base.inputs),
                    RunMeta(
                        kind="control", base_run_id=base.run_id, control_id=control.id
                    ),
                )
        for run in ledger.control_runs_of(base.run_id):
            meta = ledger.meta(run.run_id)
            others = [r for r in final if r.run_id != base.run_id]
            assert meta.control_id is not None
            out.append(
                ControlResult(
                    run_id=run.run_id,
                    base_run_id=base.run_id,
                    scenario=str(base.inputs.scenario),
                    control_id=meta.control_id,
                    title=CONTROLS[meta.control_id].title,
                    parameter=meta.parameter,
                    tested_by="code"
                    if run.run_id not in session.agent_control_runs
                    else "agent",
                    recommended=run.run_id in recommended,
                    ale_before=base.distribution.ale_mean,
                    ale_after=run.distribution.ale_mean,
                    p99_before=base.distribution.p99,
                    p99_after=run.distribution.p99,
                    combined_after=loss_summary(
                        combine([*others, run]).distribution, tolerance
                    ),
                )
            )
    return sorted(out, key=lambda c: (not c.recommended, c.combined_after.ale_mean))


# Which client fact would narrow each kind of input
_FACT_FOR_INPUT = {
    "primary_loss.response": "records_shared",
    "secondary_loss": "records_shared",
    "primary_loss.productivity": "revenue_at_risk_per_hour",
    "primary_loss.replacement": "annual_contract_value",
    "threat_event_frequency": "vendor security evidence (attestations, breach history)",
    "vulnerability": "service_criticality",
}


def _sharpen(ctx: AnalysisContext, scenarios: list[ScenarioSummary]) -> list[str]:
    """The missing facts that matter most here, ordered by the drivers they feed."""
    missing = set(ctx.missing_inputs())
    ranked: list[tuple[float, str]] = []
    if "risk_tolerance_usd" in missing:
        ranked.append((float("inf"), "risk_tolerance_usd"))  # needed for any decision
    for s in scenarios:
        for d in s.drivers:
            key = d.path if d.path in _FACT_FOR_INPUT else d.path.split(".")[0]
            fact = _FACT_FOR_INPUT.get(key)
            if fact and (fact in missing or " " in fact):
                ranked.append((abs(d.swing), fact))
    seen: list[str] = []
    for _, fact in sorted(ranked, key=lambda x: -x[0]):
        if fact not in seen:
            seen.append(fact)
    return seen


def run_figures(r: SimulationResult, tolerance: Tolerance | None) -> dict:
    """A run as the model sees it: every dollar figure already formatted."""
    d = r.distribution
    out = {
        "run_id": r.run_id,
        "scenario": str(r.inputs.scenario),
        "label": r.inputs.label,
        "loss_events_per_year": round(r.loss_event_frequency_mean, 4),
        "expected_annual_loss": format_usd(d.ale_mean),
        "p10": format_usd(d.p10),
        "p50": format_usd(d.p50),
        "p90": format_usd(d.p90),
        "p99": format_usd(d.p99),
        "chance_of_any_loss": format_percent(d.prob_any_loss),
        "breakdown": {k: format_usd(v) for k, v in r.breakdown.items()},
        "assumptions": f"{r.inputs.assumption_count} of {len(list(r.inputs.ranges()))} inputs",
    }
    if tolerance is not None:
        out["tolerance"] = format_usd(tolerance.value_usd)
        out["chance_exceeds_tolerance"] = format_percent(
            d.prob_exceeds(tolerance.value_usd)
        )
    return out


def _num(v: float) -> str:
    return f"{v:.4g}"


def render_text(report: FairReport) -> str:
    """A plain-text report for the CLI."""
    lines = [
        f"FAIR analysis: {report.subject}   [{report.status}, by {report.generated_by}]",
        "",
    ]
    c = report.combined
    lines += [
        f"Expected annual loss : {format_usd(c.ale_mean)}",
        "P10 / P50 / P90 / P99: "
        + " / ".join(format_usd(v) for v in (c.p10, c.p50, c.p90, c.p99)),
        f"Chance of any loss   : {format_percent(c.prob_any_loss)}",
    ]
    if report.tolerance and c.prob_exceeds_tolerance is not None:
        lines.append(
            f"Chance > tolerance   : {format_percent(c.prob_exceeds_tolerance)} "
            f"(tolerance {format_usd(report.tolerance.value_usd)}, {report.tolerance.source})"
        )
    lines.append(
        f"Assumptions          : {report.assumption_count} of {report.input_count} inputs"
        + ("  (not decision-grade)" if report.assumption_share > 0.5 else "")
    )
    for s in report.scenarios:
        lines += [
            "",
            f"{s.title} [{s.run_id}] {s.label}",
            (
                f"  expected {format_usd(s.loss.ale_mean)}, P90 {format_usd(s.loss.p90)}, "
                f"P99 {format_usd(s.loss.p99)}, any loss "
                f"{format_percent(s.loss.prob_any_loss)}"
            ),
        ]
        for i in s.inputs:
            fmt = format_usd if i.path.endswith("_loss") or "_loss." in i.path else _num
            lines.append(
                f"  - {i.path}: {fmt(i.min)} / {fmt(i.mode)} / {fmt(i.max)}  "
                f"[{i.source}] {i.rationale}"
            )
    if report.controls:
        lines += ["", "Controls (expected annual loss before -> after):"]
        for ctl in report.controls:
            lines.append(
                f"  - {ctl.title} on {ctl.scenario} [{ctl.run_id}, by {ctl.tested_by}]: "
                f"{format_usd(ctl.ale_before)} -> {format_usd(ctl.ale_after)}"
                + ("  (recommended)" if ctl.recommended else "")
            )
    lines += ["", report.narrative]
    for title, items in (
        ("Key findings", report.key_findings),
        ("Recommendations", report.recommendations),
        ("Notes", report.notes),
    ):
        if items:
            lines += ["", f"{title}:"] + [f"  - {x}" for x in items]
    if report.missing_inputs:
        lines += [
            "",
            "To sharpen this estimate, provide: " + ", ".join(report.missing_inputs),
        ]
    lines += [
        "",
        f"Investigation: {report.llm_calls} model calls, {report.tool_calls} tool calls, "
        f"{report.input_tokens + report.output_tokens:,} tokens "
        f"({report.input_tokens:,} in / {report.output_tokens:,} out)"
        + (f", paced {report.paced_seconds:.0f}s" if report.paced_seconds else ""),
    ]
    for t in report.trace:
        lines.append(
            f"  {t.step:>2}. [{t.kind}{'/' + t.tool if t.tool else ''}] {t.summary}"
        )
    return "\n".join(lines)
