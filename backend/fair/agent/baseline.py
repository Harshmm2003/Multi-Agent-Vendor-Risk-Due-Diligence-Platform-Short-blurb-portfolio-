"""
Baseline inputs: what each scenario looks like from benchmarks and the client
profile alone, before any vendor-specific evidence.

Two uses:
  - the agent's starting point (get_baseline_inputs), which it then refines
    with evidence;
  - the fallback when the agent cannot finish, so an analysis always
    produces numbers.

Fallback order for every input: client fact -> derived from the profile ->
cited benchmark -> labelled assumption. Ranges are deliberately wide.
"""

from fair import benchmarks
from fair.context import SENSITIVE_DATA, AnalysisContext, Criticality
from fair.model.altman import altman_z
from fair.model.taxonomy import (
    PrimaryForm,
    ProbabilityRange,
    Range,
    Scenario,
    ScenarioInputs,
)
from fair.ports import FinancialData

# Spread of a breach's cost around the benchmark mean. PERT mean of
# (0.1, 0.6, 2.5) x mean is 0.83 x mean: a right-skewed shape where most
# breaches cost less than the average and a few cost far more.
_BREACH_COST_SHAPE = (0.1, 0.6, 2.5)
# Breach cost when the vendor holds no sensitive data (only internal data).
_NON_SENSITIVE_FACTOR = 0.3
# Insolvency productivity loss as a share of annual contract value.
_CRITICALITY_SHARE = {
    Criticality.LOW: 0.1,
    Criticality.MEDIUM: 0.25,
    Criticality.HIGH: 0.5,
    Criticality.CRITICAL: 1.0,
}


class NoBaseline(Exception):
    """No baseline exists for this scenario; the agent must build it from evidence."""


def baseline_inputs(
    scenario: Scenario,
    context: AnalysisContext,
    financials: FinancialData | None,
) -> ScenarioInputs:
    if scenario == Scenario.VENDOR_BREACH:
        return _breach(context)
    if scenario == Scenario.VENDOR_INSOLVENCY:
        return _insolvency(context, financials)
    if scenario == Scenario.VENDOR_OUTAGE:
        return _outage(context)
    if scenario == Scenario.SANCTIONS_EXIT:
        return _sanctions_exit(context)
    raise NoBaseline(f"no baseline for {scenario}; build its inputs from evidence")


def _breach(context: AnalysisContext) -> ScenarioInputs:
    freq = benchmarks.get("cyentia_iris_2025.major_event_annual_prob")
    healthcare = "health" in context.industry.lower()
    cost = benchmarks.get(
        "ibm_codb_2025.healthcare_avg" if healthcare else "ibm_codb_2026.global_avg"
    )

    data = set(context.exposure.data_types)
    if data and not data & SENSITIVE_DATA:
        factor, data_note = (
            _NON_SENSITIVE_FACTOR,
            "no sensitive data shared, so scaled to 30%",
        )
    elif data:
        factor, data_note = 1.0, f"sensitive data shared ({', '.join(sorted(data))})"
    else:
        factor, data_note = 1.0, "data shared unknown, so the full figure is used"

    lo, mode, hi = (k * cost.value * factor for k in _BREACH_COST_SHAPE)
    return ScenarioInputs(
        scenario=Scenario.VENDOR_BREACH,
        label="Baseline from benchmarks",
        threat_event_frequency=Range(
            min=0.02,
            mode=freq.value,
            max=0.25,
            source=freq.ref,
            rationale=(
                "Annual chance of a major cyber event at an average organisation, used "
                "as the vendor's loss-event rate. Vendor evidence (certifications, "
                "breach history) should narrow it."
            ),
        ),
        vulnerability=ProbabilityRange.fixed(
            1.0,
            "derived:loss_event_benchmark",
            "The frequency benchmark already counts loss events, not attempts.",
        ),
        primary_loss={
            PrimaryForm.RESPONSE: Range(
                min=lo,
                mode=mode,
                max=hi,
                source=cost.ref,
                rationale=(
                    f"{cost.source}: mean total breach cost "
                    f"({'healthcare' if healthcare else 'global'}), spread around the "
                    f"mean; {data_note}. It includes response, fines and lost business, "
                    "so no separate secondary loss is added."
                ),
            )
        },
    )


def _insolvency(
    context: AnalysisContext, financials: FinancialData | None
) -> ScenarioInputs:
    pd: ProbabilityRange | None = None
    if financials is not None:
        z = altman_z(financials.statement, manufacturer=financials.manufacturer)
        pd = z.annual_default_probability
    if pd is None:
        spec = benchmarks.get("sp_default.spec_grade_us_longrun")
        pd = ProbabilityRange(
            min=0.005,
            mode=spec.value,
            max=0.15,
            source=spec.ref,
            rationale=(
                "No usable financials, so the vendor is treated like a speculative-grade "
                "issuer: a cautious default for an unrated vendor."
            ),
        )

    replacement, productivity = _switching_losses(context)

    return ScenarioInputs(
        scenario=Scenario.VENDOR_INSOLVENCY,
        label="Baseline from benchmarks",
        threat_event_frequency=Range(
            min=pd.min,
            mode=pd.mode,
            max=pd.max,
            source=pd.source,
            rationale=pd.rationale,
        ),
        vulnerability=ProbabilityRange.fixed(
            1.0, "derived:every_default_is_loss", "Every default is a loss event."
        ),
        primary_loss={
            PrimaryForm.REPLACEMENT: replacement,
            PrimaryForm.PRODUCTIVITY: productivity,
        },
    )


def _switching_losses(context: AnalysisContext) -> tuple[Range, Range]:
    """Replacing a vendor that stops: migration cost, and disruption meanwhile."""
    acv = context.annual_contract_value
    if acv:
        replacement = Range(
            min=0.25 * acv,
            mode=0.75 * acv,
            max=2.0 * acv,
            source="assumption",
            rationale="Switching and migration cost assumed at 0.25-2x the annual contract value.",
        )
    else:
        replacement = Range(
            min=25_000,
            mode=100_000,
            max=500_000,
            source="assumption",
            rationale="No contract value given; generic switching cost.",
        )

    crit = context.exposure.service_criticality
    if acv and crit:
        share = _CRITICALITY_SHARE[crit]
        productivity = Range(
            min=0.2 * share * acv,
            mode=share * acv,
            max=3 * share * acv,
            source="assumption",
            rationale=(
                f"Disruption while switching, '{crit}' service: about {share:g}x the "
                "annual contract value."
            ),
        )
    else:
        productivity = Range(
            min=10_000,
            mode=50_000,
            max=300_000,
            source="assumption",
            rationale="Service criticality or contract value unknown; generic disruption cost.",
        )
    return replacement, productivity


def _outage(context: AnalysisContext) -> ScenarioInputs:
    """
    No published outage-frequency benchmark is cited yet, so frequency and
    vulnerability are labelled assumptions. The loss uses the client's own
    revenue at risk per hour when given.
    """
    crit = context.exposure.service_criticality
    critical = crit in (Criticality.HIGH, Criticality.CRITICAL)
    rate = context.exposure.revenue_at_risk_per_hour
    if rate:
        productivity = Range(
            min=2 * rate,
            mode=8 * rate,
            max=48 * rate,
            source="client, assumption",
            rationale=(
                "The client's revenue at risk per hour x an assumed 2 / 8 / 48 hours "
                "of outage beyond what the client can absorb."
            ),
        )
    elif context.annual_contract_value and crit:
        share = _CRITICALITY_SHARE[crit]
        acv = context.annual_contract_value
        productivity = Range(
            min=0.05 * share * acv,
            mode=0.2 * share * acv,
            max=share * acv,
            source="assumption",
            rationale=(
                f"No revenue-at-risk figure; scaled from the contract value for a "
                f"'{crit}' service."
            ),
        )
    else:
        productivity = Range(
            min=5_000,
            mode=25_000,
            max=150_000,
            source="assumption",
            rationale="No revenue-at-risk figure or criticality; generic outage cost.",
        )
    return ScenarioInputs(
        scenario=Scenario.VENDOR_OUTAGE,
        label="Baseline from assumptions",
        threat_event_frequency=Range(
            min=1,
            mode=3,
            max=8,
            source="assumption",
            rationale="Significant service outages per year for a hosted vendor (assumed).",
        ),
        vulnerability=ProbabilityRange(
            min=0.1 if critical else 0.05,
            mode=0.25 if critical else 0.1,
            max=0.5 if critical else 0.25,
            source="assumption",
            rationale=(
                "Chance an outage lasts long enough to hurt the business; higher for a "
                f"{crit or 'service of unknown'} criticality service."
            ),
        ),
        primary_loss={
            PrimaryForm.PRODUCTIVITY: productivity,
            PrimaryForm.RESPONSE: Range(
                min=2_000,
                mode=10_000,
                max=50_000,
                source="assumption",
                rationale="Staff time to handle the incident and communicate with customers.",
            ),
        },
    )


def _sanctions_exit(context: AnalysisContext) -> ScenarioInputs:
    """A designation forces an immediate exit: the same switching losses as insolvency."""
    possible = context.ofac_status == "possible_match"
    replacement, productivity = _switching_losses(context)
    return ScenarioInputs(
        scenario=Scenario.SANCTIONS_EXIT,
        label="Baseline from assumptions",
        threat_event_frequency=Range(
            min=0.05 if possible else 0.0005,
            mode=0.15 if possible else 0.001,
            max=0.4 if possible else 0.005,
            source="assumption",
            rationale=(
                "Sanctions screening returned a possible match, so designation is a real "
                "prospect until a compliance officer clears it."
                if possible
                else "Screening was clear; designation of a clear vendor is rare."
            ),
        ),
        vulnerability=ProbabilityRange.fixed(
            1.0, "derived:designation_forces_exit", "A designation forces an exit."
        ),
        primary_loss={
            PrimaryForm.REPLACEMENT: replacement,
            PrimaryForm.PRODUCTIVITY: productivity,
        },
    )
