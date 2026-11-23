"""
The numbers a host's decision rules need from a FAIR report.

Computed by code from the report's simulated figures, never from the model's
text, so a decision built on them cannot be talked into anything. The rules
themselves belong to the host (VendorIQ: app/agents/meta_agent.py), because
what counts as acceptable risk is the host's policy, not FAIR's.
"""

from typing import Literal

from pydantic import BaseModel

from fair.agent.report import FairReport
from fair.format import format_percent, format_usd

# More than half the inputs assumed: the figures are indicative only.
MAX_ASSUMPTION_SHARE = 0.5


class FairDecisionSignals(BaseModel):
    status: Literal["complete", "partial"]
    decision_grade: bool
    not_decision_grade_reason: str | None

    assumption_share: float
    ale_mean: float  # expected annual loss
    p50: float
    p90: float
    p95: float
    p99: float
    prob_any_loss: float

    tolerance_usd: float | None
    tolerance_source: Literal["client", "derived"] | None
    prob_exceeds_tolerance: float | None

    annual_contract_value: float | None
    loss_to_contract_ratio: float | None  # expected annual loss / annual contract value
    uninsured_p99: (
        float | None
    )  # 1-in-100-year loss not covered by liability cap + insurance

    dominant_scenario: str
    run_ids: list[str]
    flags: list[str]

    # The control that lowers the combined expected loss most (recommended
    # ones first), and whether it brings the exposure within tolerance
    best_control: "ControlEffect | None" = None


class ControlEffect(BaseModel):
    control_id: str
    title: str
    run_id: str
    recommended: bool
    ale_after: float
    p50_after: float
    prob_exceeds_after: float | None
    fixes_tolerance: bool  # with the control, no VendorIQ REVIEW trigger would fire


FairDecisionSignals.model_rebuild()

# A control "fixes" the exposure only if the chance of reaching the tolerance
# also drops below this (same threshold VendorIQ uses for REVIEW)
FIXED_PROB_EXCEEDS = 0.10


def signals_from_report(report: FairReport) -> FairDecisionSignals:
    c = report.combined
    ctx = report.context
    tol = report.tolerance

    reason = None
    if report.status != "complete":
        reason = "the agent did not finish; figures are a fallback estimate"
    elif report.assumption_share > MAX_ASSUMPTION_SHARE:
        reason = (
            f"{report.assumption_count} of {report.input_count} inputs are assumptions "
            "(over half)"
        )

    acv = ctx.annual_contract_value
    cover = [
        v
        for v in (ctx.exposure.liability_cap_usd, ctx.exposure.cyber_insurance_usd)
        if v
    ]
    uninsured = max(0.0, c.p99 - sum(cover)) if cover else None
    dominant = max(report.scenarios, key=lambda s: s.loss.ale_mean)

    flags = []
    if tol is not None and c.ale_mean >= tol.value_usd:
        flags.append(
            f"Expected annual loss {format_usd(c.ale_mean)} reaches the "
            f"{tol.source} tolerance of {format_usd(tol.value_usd)}"
        )
    if tol is not None and c.prob_exceeds_tolerance:
        flags.append(
            f"{format_percent(c.prob_exceeds_tolerance)} chance per year that losses "
            f"reach {format_usd(tol.value_usd)}"
        )
    if c.p99 > 0:
        flags.append(f"1-in-100-year loss (P99): {format_usd(c.p99)}")
    if uninsured:
        flags.append(f"{format_usd(uninsured)} of a 1-in-100-year loss is not covered")

    best = _best_control(
        report, tol.value_usd if tol else None, acv, sum(cover) if cover else None
    )

    return FairDecisionSignals(
        best_control=best,
        status=report.status,
        decision_grade=reason is None,
        not_decision_grade_reason=reason,
        assumption_share=report.assumption_share,
        ale_mean=c.ale_mean,
        p50=c.p50,
        p90=c.p90,
        p95=c.p95,
        p99=c.p99,
        prob_any_loss=c.prob_any_loss,
        tolerance_usd=tol.value_usd if tol else None,
        tolerance_source=tol.source if tol else None,
        prob_exceeds_tolerance=c.prob_exceeds_tolerance,
        annual_contract_value=acv,
        loss_to_contract_ratio=c.ale_mean / acv if acv else None,
        uninsured_p99=uninsured,
        dominant_scenario=dominant.scenario,
        run_ids=[s.run_id for s in report.scenarios],
        flags=flags,
    )


def _best_control(
    report: FairReport,
    tolerance: float | None,
    annual_contract_value: float | None,
    cover: float | None,
) -> ControlEffect | None:
    if not report.controls:
        return None
    c = min(
        report.controls, key=lambda c: (not c.recommended, c.combined_after.ale_mean)
    )
    after = c.combined_after
    # "Fixes" means no VendorIQ REVIEW trigger would still fire with the
    # control in place, not just the tolerance tests: otherwise the meta-agent
    # would offer "acceptable if" for a vendor still over the contract value
    # or with a 1-in-100 loss far beyond the cap and insurance.
    fixes = (
        tolerance is not None
        and after.ale_mean < tolerance
        and after.p50 < tolerance
        and (after.prob_exceeds_tolerance or 0) < FIXED_PROB_EXCEEDS
        and (not annual_contract_value or after.ale_mean <= annual_contract_value)
        and (cover is None or after.p99 - cover <= tolerance)
    )
    return ControlEffect(
        control_id=c.control_id,
        title=c.title,
        run_id=c.run_id,
        recommended=c.recommended,
        ale_after=after.ale_mean,
        p50_after=after.p50,
        prob_exceeds_after=after.prob_exceeds_tolerance,
        fixes_tolerance=fixes,
    )
