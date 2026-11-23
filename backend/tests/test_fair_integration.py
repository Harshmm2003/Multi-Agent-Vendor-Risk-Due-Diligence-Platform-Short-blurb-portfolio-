"""
VendorIQ <-> FAIR agent: the adapter, the skip rule, and the meta-agent's
FAIR overlay. No network: FAIR reports come from the deterministic fallback,
and the agent itself is replaced where a run would call Groq.
"""

import asyncio

import pytest

from app.agents import meta_agent
from app.agents.meta_agent import apply_fair_rules, evaluate_fair, run_meta_agent
from app.integrations import fair_adapter
from app.integrations.fair_adapter import (
    VendorIQEvidenceProvider,
    build_context,
    run_fair_analysis,
    skip_reason,
)
from fair.agent import Session, fallback_report
from fair.context import AnalysisContext
from fair.decision import FairDecisionSignals
from fair.ports import StaticEvidenceProvider

CLIENT = {
    "company_name": "Northwind Health",
    "industry": "Healthcare",
    "contract_value": 1_200_000,
    "contract_years": 3,
    "risk_appetite": "Low",
    "priorities": [],
    "dealbreakers": [],
    "nice_to_have": [],
}


def output(score=85.0, confidence=0.97, **extra):
    return {
        "score": score,
        "confidence": confidence,
        "explanation": "Solid controls. " * 40,
        "evidence": [f"fact {i}" for i in range(8)],
        "red_flags": [f"flag {i}" for i in range(6)],
        **extra,
    }


def state(**overrides):
    s = {
        "vendor_name": "Okta",
        "evaluation_id": "eval-1",
        "compliance_data": {
            "ofac_status": "clear",
            "ofac_clear": True,
            "ofac_matches": [],
        },
        **{f"{d}_output": output() for d in fair_adapter.DOMAINS},
    }
    s.update(overrides)
    return s


# ── Adapter ───────────────────────────────────────────────────────────────


def test_findings_are_trimmed_for_tokens_and_include_cert_tiers():
    provider = VendorIQEvidenceProvider(
        state(),
        {"certification_evidence_tiers": {"SOC2_Type2": "VERIFIED_REGISTRY"}},
        {},
    )
    items = asyncio.run(provider.agent_findings("compliance"))
    titles = [i.title for i in items]
    assert titles[0] == "Compliance score 85/100 (confidence 97%)"
    assert len(items[0].detail) <= fair_adapter.MAX_TEXT
    assert (
        sum(t.startswith("Red flag") for t in titles) == fair_adapter.MAX_ITEMS_PER_LIST
    )
    assert "Certification SOC2_Type2: VERIFIED_REGISTRY" in titles


def test_only_agents_with_output_are_domains():
    provider = VendorIQEvidenceProvider(state(reputation_output=None), {}, {})
    assert "reputation" not in provider.finding_domains()
    assert "financial" in provider.finding_domains()


def test_financials_from_scraped_altman_inputs():
    raw = {
        "_altman_inputs": {
            "working_capital": 1.0,
            "total_assets": 10.0,
            "manufacturer": True,
            "unexpected": 5,
        },
        "currency_note": "Financial figures in EUR, not USD",
    }
    data = asyncio.run(VendorIQEvidenceProvider(state(), {}, raw).financials())
    assert data is not None and data.manufacturer is True
    assert data.statement.total_assets == 10.0
    assert "EUR" in data.citation
    assert asyncio.run(VendorIQEvidenceProvider(state(), {}, {}).financials()) is None


def test_context_from_client_profile():
    ctx = build_context(
        "Okta", {**CLIENT, "exposure": {"data_types": ["phi"]}}, "eval-1"
    )
    assert ctx.annual_contract_value == 400_000  # 1.2M over 3 years
    assert ctx.risk_appetite == "low"  # normalised from "Low"
    assert ctx.industry == "Healthcare"  # the client's: the data and fines are theirs
    assert ctx.seed_key == "eval-1"
    assert ctx.exposure.data_types == ["phi"]
    assert (
        build_context("Okta", {**CLIENT, "risk_appetite": "odd"}, "e").risk_appetite
        == "medium"
    )


# ── Skip: save ~15K tokens when FAIR cannot change the outcome ────────────


def test_skip_when_ofac_match():
    s = state(compliance_data={"ofac_status": "match", "ofac_matches": ["X"]})
    assert "OFAC" in skip_reason(s)


def test_skip_when_dealbreaker_rejects(monkeypatch):
    monkeypatch.setattr(
        fair_adapter, "_evaluate_dealbreakers", lambda *a: {"status": "hard_reject"}
    )
    assert "dealbreaker" in skip_reason(state())


def test_no_skip_normally():
    assert skip_reason(state()) is None


def test_run_fair_analysis_off_skipped_and_on(monkeypatch):
    settings = fair_adapter.get_settings()
    assert (
        asyncio.run(run_fair_analysis(state(), CLIENT, {}, {})) is None
    )  # off in tests

    monkeypatch.setattr(settings, "fair_enabled", True)
    s = state(compliance_data={"ofac_status": "match"})
    assert asyncio.run(run_fair_analysis(s, CLIENT, {}, {})) == {
        "skipped": "OFAC sanctions match already rejects the vendor"
    }

    built = {}

    class FakeChatGroq:
        def __init__(self, **kw):
            built.update(kw)

    class FakeAgent:
        def __init__(self, llm, **kw):
            built["agent"] = kw

        async def run(self, context, provider):
            session = Session(context, provider, 1_000)
            return await fallback_report(session, "fake agent")

    monkeypatch.setattr(fair_adapter, "ChatGroq", FakeChatGroq)
    monkeypatch.setattr(fair_adapter, "FairAgent", FakeAgent)
    report = asyncio.run(run_fair_analysis(state(), CLIENT, {}, {}))
    assert report["subject"] == "Okta" and report["status"] == "partial"
    assert built["reasoning_effort"] == "low" and built["temperature"] == 0
    assert built["agent"]["tokens_per_minute"] == 8_000


# ── Meta-agent FAIR rules ─────────────────────────────────────────────────


def signals(**kw) -> FairDecisionSignals:
    base = {
        "status": "complete",
        "decision_grade": True,
        "not_decision_grade_reason": None,
        "assumption_share": 0.2,
        "ale_mean": 50_000.0,
        "p50": 0.0,
        "p90": 0.0,
        "p95": 0.0,
        "p99": 2_000_000.0,
        "prob_any_loss": 0.03,
        "tolerance_usd": 200_000.0,
        "tolerance_source": "client",
        "prob_exceeds_tolerance": 0.03,
        "annual_contract_value": 400_000.0,
        "loss_to_contract_ratio": 0.125,
        "uninsured_p99": None,
        "dominant_scenario": "vendor_breach",
        "run_ids": ["run_01"],
        "flags": [],
    }
    return FairDecisionSignals(**{**base, **kw})


@pytest.mark.parametrize(
    "overrides, effect, reason",
    [
        ({}, "none", None),
        ({"ale_mean": 250_000.0}, "review", "expected annual loss $250K reaches"),
        ({"prob_exceeds_tolerance": 0.12}, "review", "12.0% chance per year"),
        ({"uninsured_p99": 900_000.0}, "review", "leaves $900K not covered"),
        (
            {
                "loss_to_contract_ratio": 1.5,
                "ale_mean": 150_000.0,
                "annual_contract_value": 100_000.0,
            },
            "review",
            "exceeds the annual contract value",
        ),
        ({"p50": 250_000.0, "ale_mean": 300_000.0}, "reject", "typical year (P50)"),
        # a derived tolerance can never reject
        (
            {"p50": 250_000.0, "ale_mean": 300_000.0, "tolerance_source": "derived"},
            "review",
            None,
        ),
        # not decision-grade: shown, never acted on
        (
            {
                "decision_grade": False,
                "not_decision_grade_reason": "x",
                "ale_mean": 9e9,
            },
            "none",
            None,
        ),
        # no tolerance at all: only the contract-value rule can fire
        (
            {
                "tolerance_usd": None,
                "tolerance_source": None,
                "prob_exceeds_tolerance": None,
            },
            "none",
            None,
        ),
    ],
)
def test_fair_rules(overrides, effect, reason):
    result = apply_fair_rules(signals(**overrides), avg_conf=0.97)
    assert result["effect"] == effect
    if reason:
        assert any(reason in r for r in result["reasons"]), result["reasons"]


def test_reject_needs_confidence():
    s = signals(p50=250_000.0, ale_mean=300_000.0)
    assert apply_fair_rules(s, avg_conf=0.4)["effect"] == "review"


def real_report(status="complete", **ctx) -> dict:
    """A real FAIR report (deterministic fallback), optionally marked complete."""
    context = AnalysisContext.model_validate(
        {
            "subject": "Okta",
            "industry": "Healthcare",
            "annual_contract_value": 400_000,
            "risk_appetite": "low",
            "exposure": {"data_types": ["phi"], "risk_tolerance_usd": 200_000},
            **ctx,
        }
    )
    session = Session(context, StaticEvidenceProvider(), 4_000)
    report = asyncio.run(fallback_report(session, "test")).model_dump(mode="json")
    report["status"] = status
    return report


def test_evaluate_fair_reads_real_reports():
    assert evaluate_fair(None, 0.9)["effect"] == "none"
    assert "not run" in evaluate_fair({"skipped": "OFAC"}, 0.9)["note"]
    assert "could not be read" in evaluate_fair({"bogus": 1}, 0.9)["note"]

    partial = evaluate_fair(real_report(status="partial"), 0.9)
    assert partial["effect"] == "none" and "informational only" in partial["note"]
    assert partial["signals"]["ale_mean"] > 0

    complete = evaluate_fair(real_report(), 0.9)
    # healthcare baseline: expected loss well above a $200K stated tolerance
    assert complete["effect"] == "review"
    assert complete["signals"]["tolerance_source"] == "client"


# ── Overlay in run_meta_agent: stricter of the two, never looser ──────────


def meta(fair_output, **outputs):
    s = {
        "vendor_name": "Okta",
        "compliance_data": {
            "ofac_status": "clear",
            "ofac_clear": True,
            "ofac_matches": [],
        },
        **{f"{d}_output": outputs.get(d, output()) for d in fair_adapter.DOMAINS},
        "fair_output": fair_output,
    }
    return asyncio.run(run_meta_agent(s))


def test_fair_escalates_approve_to_review():
    r = meta(real_report())
    assert r["base_recommendation"] == "APPROVE"
    assert r["final_recommendation"] == "REVIEW" and r["escalated_by_fair"] is True
    assert r["decision_reason"].startswith("Financial exposure (FAIR):")
    assert "Scores alone would have recommended APPROVE." in r["decision_reason"]
    assert any(f.startswith("[FAIR]") for f in r["all_red_flags"])
    assert "FINANCIAL EXPOSURE (FAIR):" in r["final_explanation"]
    assert "escalated APPROVE → REVIEW" in r["final_explanation"]


def test_fair_never_softens_a_reject():
    tiny = real_report(
        industry="Retail",
        exposure={"data_types": ["internal"]},
        annual_contract_value=10_000_000,
    )
    r = meta(
        tiny,
        financial=output(score=10.0),
        compliance=output(score=10.0),
        operational=output(score=10.0),
        reputation=output(score=10.0),
        strategic=output(score=10.0),
    )
    assert r["base_recommendation"] == "REJECT" == r["final_recommendation"]
    assert r["escalated_by_fair"] is False


def test_partial_fair_report_is_shown_but_not_acted_on():
    r = meta(real_report(status="partial"))
    assert r["final_recommendation"] == "APPROVE" and r["fair_effect"] == "none"
    assert "informational only" in r["final_explanation"]


def test_no_fair_output_changes_nothing():
    with_none = meta(None)
    assert with_none["final_recommendation"] == "APPROVE"
    assert (
        with_none["financial_exposure"] is None and with_none["fair_effect"] == "none"
    )
    assert "FINANCIAL EXPOSURE" not in with_none["final_explanation"]


def test_thresholds_are_named_constants():
    assert meta_agent.FAIR_REVIEW_PROB_EXCEEDS == 0.10
    assert meta_agent.FAIR_LOSS_TO_CONTRACT_MAX == 1.0


# ── Step 4: controls become contract conditions ───────────────────────────


def control(fixes=True, **kw):
    from fair.decision import ControlEffect

    base = {
        "control_id": "limit_records_shared",
        "title": "Share fewer records with the vendor",
        "run_id": "run_07",
        "recommended": True,
        "ale_after": 40_000.0,
        "p50_after": 0.0,
        "prob_exceeds_after": 0.02,
        "fixes_tolerance": fixes,
    }
    return ControlEffect(**{**base, **kw})


def test_fixing_control_turns_reject_into_review_with_condition():
    s = signals(p50=250_000.0, ale_mean=300_000.0, best_control=control())
    r = apply_fair_rules(s, avg_conf=0.97)
    assert r["effect"] == "review"
    assert r["conditions"] == [
        (
            "Share fewer records with the vendor: expected annual loss falls to $40K, "
            "chance of reaching tolerance 2.0% [run_07]"
        )
    ]


def test_non_fixing_control_adds_no_condition():
    s = signals(p50=250_000.0, ale_mean=300_000.0, best_control=control(fixes=False))
    r = apply_fair_rules(s, avg_conf=0.97)
    assert r["effect"] == "reject" and r["conditions"] == []


def test_no_condition_when_nothing_is_wrong():
    r = apply_fair_rules(signals(best_control=control()), avg_conf=0.97)
    assert r["effect"] == "none" and r["conditions"] == []


def test_condition_reaches_decision_reason():
    report = real_report()
    report_with_fix = dict(report)
    # make the first control a recommended, cheap fix
    best = dict(report["controls"][0])
    best["recommended"] = True
    best["combined_after"] = {
        **best["combined_after"],
        "ale_mean": 1_000.0,
        "p50": 0.0,
        "prob_exceeds_tolerance": 0.0,
    }
    report_with_fix["controls"] = [best, *report["controls"][1:]]
    r = meta(report_with_fix)
    assert r["final_recommendation"] == "REVIEW"
    assert r["conditions"] and "Acceptable if:" in r["decision_reason"]
    assert "Acceptable if:" in r["final_explanation"]
