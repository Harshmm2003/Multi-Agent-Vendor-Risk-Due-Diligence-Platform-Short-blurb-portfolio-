"""Cited benchmarks and the baseline built from them."""

import pytest

from fair import benchmarks
from fair.agent.baseline import baseline_inputs
from fair.context import AnalysisContext
from fair.model import FinancialStatement, Scenario
from fair.ports import FinancialData


def test_every_benchmark_is_cited_and_dated():
    items = benchmarks.all_benchmarks()
    assert len(items) >= 10
    for b in items.values():
        assert b.source and b.url.startswith("https://") and b.year >= 2016
        assert b.topics, b.id


def test_lookup_and_exists():
    assert {b.id for b in benchmarks.lookup("default_rate")} >= {
        "sp_default.bbb_us_longrun",
        "sp_default.spec_grade_us_longrun",
        "sp_default.ccc_c_global_longrun",
    }
    assert benchmarks.exists("benchmark:ibm_codb_2026.global_avg")
    assert not benchmarks.exists("benchmark:nope")
    assert not benchmarks.exists("ibm_codb_2026.global_avg")  # needs the prefix


def ctx(**kw) -> AnalysisContext:
    return AnalysisContext.model_validate({"subject": "V", **kw})


def response(inputs):
    return inputs.primary_loss["response"]


def test_breach_uses_industry_benchmark():
    health = baseline_inputs(Scenario.VENDOR_BREACH, ctx(industry="Healthcare"), None)
    other = baseline_inputs(Scenario.VENDOR_BREACH, ctx(industry="Retail"), None)
    assert response(health).source == "benchmark:ibm_codb_2025.healthcare_avg"
    assert response(other).source == "benchmark:ibm_codb_2026.global_avg"
    assert response(health).mode > response(other).mode


def test_breach_scaled_down_without_sensitive_data():
    sensitive = baseline_inputs(
        Scenario.VENDOR_BREACH, ctx(exposure={"data_types": ["pii"]}), None
    )
    internal = baseline_inputs(
        Scenario.VENDOR_BREACH, ctx(exposure={"data_types": ["internal"]}), None
    )
    assert response(internal).mode == pytest.approx(0.3 * response(sensitive).mode)


def test_breach_mean_close_to_benchmark_mean():
    # PERT mean of (0.1, 0.6, 2.5) x mean ≈ 0.83 x mean: right-skewed, not inflated
    r = response(baseline_inputs(Scenario.VENDOR_BREACH, ctx(), None))
    mean = (r.min + 4 * r.mode + r.max) / 6
    assert mean == pytest.approx(
        0.833 * benchmarks.get("ibm_codb_2026.global_avg").value, rel=0.01
    )


def test_insolvency_uses_altman_when_financials_exist():
    data = FinancialData(
        statement=FinancialStatement(
            working_capital=-100,
            retained_earnings=-300,
            ebit=-50,
            total_assets=1000,
            total_liabilities=900,
            book_equity=100,
        ),
        citation="test",
    )
    inputs = baseline_inputs(Scenario.VENDOR_INSOLVENCY, ctx(), data)
    assert (
        inputs.threat_event_frequency.source
        == "benchmark:sp_default.ccc_c_global_longrun"
    )


def test_insolvency_without_financials_is_cautious():
    inputs = baseline_inputs(Scenario.VENDOR_INSOLVENCY, ctx(), None)
    tef = inputs.threat_event_frequency
    assert tef.source == "benchmark:sp_default.spec_grade_us_longrun"
    assert inputs.assumption_count == 2  # replacement and productivity


def test_insolvency_scales_with_contract_and_criticality():
    low = baseline_inputs(
        Scenario.VENDOR_INSOLVENCY,
        ctx(annual_contract_value=100_000, exposure={"service_criticality": "low"}),
        None,
    )
    critical = baseline_inputs(
        Scenario.VENDOR_INSOLVENCY,
        ctx(
            annual_contract_value=100_000, exposure={"service_criticality": "critical"}
        ),
        None,
    )
    assert (
        critical.primary_loss["productivity"].mode
        == 10 * low.primary_loss["productivity"].mode
    )
    assert low.primary_loss["replacement"].mode == 75_000


def test_outage_uses_client_revenue_at_risk():
    inputs = baseline_inputs(
        Scenario.VENDOR_OUTAGE,
        ctx(
            exposure={
                "revenue_at_risk_per_hour": 10_000,
                "service_criticality": "critical",
            }
        ),
        None,
    )
    prod = inputs.primary_loss["productivity"]
    assert (prod.min, prod.mode, prod.max) == (
        20_000,
        80_000,
        480_000,
    )  # 2 / 8 / 48 hours
    assert prod.source_ids == ["client", "assumption"]
    assert inputs.vulnerability.mode == 0.25  # critical service


def test_outage_without_client_figures_is_all_assumption():
    inputs = baseline_inputs(Scenario.VENDOR_OUTAGE, ctx(), None)
    assert inputs.assumption_count == len(list(inputs.ranges()))


def test_sanctions_exit_follows_screening():
    clear = baseline_inputs(Scenario.SANCTIONS_EXIT, ctx(ofac_status="clear"), None)
    possible = baseline_inputs(
        Scenario.SANCTIONS_EXIT, ctx(ofac_status="possible_match"), None
    )
    assert (
        possible.threat_event_frequency.mode > 100 * clear.threat_event_frequency.mode
    )
    assert "possible match" in possible.threat_event_frequency.rationale


def test_every_scenario_has_a_baseline_that_simulates():
    from fair.model import simulate

    for scenario in Scenario:
        inputs = baseline_inputs(scenario, ctx(annual_contract_value=200_000), None)
        assert simulate(inputs, seed=1, iterations=500).distribution.ale_mean >= 0


@pytest.mark.parametrize(
    "given, expected",
    [
        ({"exposure": {"risk_tolerance_usd": 250_000}}, (250_000, "client")),
        (
            {"annual_contract_value": 400_000, "risk_appetite": "low"},
            (200_000, "derived"),
        ),
        (
            {"annual_contract_value": 400_000, "risk_appetite": "high"},
            (800_000, "derived"),
        ),
    ],
)
def test_tolerance(given, expected):
    tol = ctx(**given).tolerance()
    assert tol is not None and (tol.value_usd, tol.source) == expected


def test_no_tolerance_without_any_figure():
    assert ctx().tolerance() is None
