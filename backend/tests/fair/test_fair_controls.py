"""Step 4: sensitivity analysis, controls, and how the agent and report use them."""

import json

import pytest
from test_fair_agent import (
    HAPPY_PATH,
    agent,
    breach_only,
    call,
    last_result,
    load_example,
    results_named,
    submit_with,
)

from fair.agent import Budget
from fair.model import ProbabilityRange, Range, ScenarioInputs, simulate
from fair.model.controls import CONTROLS, apply_control
from fair.model.sensitivity import drivers


def breach(**kw) -> ScenarioInputs:
    return ScenarioInputs(
        scenario="vendor_breach",
        threat_event_frequency=kw.get(
            "tef", Range(min=0.02, mode=0.09, max=0.25, source="benchmark:x.y")
        ),
        vulnerability=ProbabilityRange.fixed(1.0, "derived:loss_event_benchmark"),
        primary_loss={
            "response": Range(
                min=1e5, mode=1e6, max=5e6, source="assumption", rationale="r"
            )
        },
        label="Baseline",
    )


# ── Sensitivity ───────────────────────────────────────────────────────────


def test_drivers_ranked_and_shares_sum_to_one():
    base = simulate(breach(), seed=3, iterations=4_000)
    ds = drivers(base)
    # By hand: response swing ~ 0.105 events/yr x $4.9M = $514K; frequency swing
    # ~ $1.52M mean cost x 0.23 = $350K. So response cost drives this scenario.
    assert [d.path for d in ds] == ["primary_loss.response", "threat_event_frequency"]
    assert abs(ds[0].swing) >= abs(ds[-1].swing)
    assert sum(d.share for d in ds) == pytest.approx(1.0)
    assert all(d.high_ale >= d.low_ale for d in ds)  # more frequency or cost, more loss
    assert "vulnerability" not in {d.path for d in ds}  # fixed: nothing to swing


# ── Controls ──────────────────────────────────────────────────────────────


def test_soc2_cuts_frequency_and_keeps_provenance():
    changed = apply_control("require_soc2_type2", breach())
    tef = changed.threat_event_frequency
    assert tef.mode == pytest.approx(0.09 * 0.7)
    assert tef.source_ids == ["benchmark:x.y", "assumption"]  # original source kept
    assert "Control assumption" in tef.rationale
    assert (
        changed.label == "With control: Require SOC 2 Type 2 with annual re-attestation"
    )


def test_limit_records_takes_a_share():
    changed = apply_control("limit_records_shared", breach(), 0.2)
    assert changed.primary_loss["response"].mode == pytest.approx(0.2e6)
    with pytest.raises(ValueError):
        apply_control("limit_records_shared", breach(), 1.5)


def test_control_must_fit_the_scenario():
    with pytest.raises(ValueError, match="applies to"):
        apply_control("exit_plan_and_escrow", breach())
    with pytest.raises(KeyError, match="unknown control"):
        apply_control("magic", breach())


def test_controls_lower_expected_loss():
    base = simulate(breach(), seed=3, iterations=4_000)
    for cid in ("require_soc2_type2", "limit_records_shared", "client_held_encryption"):
        after = simulate(apply_control(cid, breach()), seed=3, iterations=4_000)
        assert after.distribution.ale_mean < base.distribution.ale_mean, cid


def test_catalog_controls_all_documented():
    for c in CONTROLS.values():
        assert c.title and c.description and c.scenarios


# ── Agent tools ───────────────────────────────────────────────────────────


def submit_recommending(messages, control_key="test_control"):
    runs = {}
    for res in results_named(messages, '"runs"'):
        for r in res.get("runs", []):
            runs[r["scenario"]] = r["run_id"]
    controls = [
        r["run_id"]
        for res in results_named(messages, '"control_id"')
        for r in res["results"]
    ]
    return call(
        "submit_report",
        final_run_ids=list(runs.values()),
        narrative="Breach dominates.",
        recommended_control_runs=controls,
    )


async def test_agent_reflects_with_sensitivity_and_controls():
    ctx, provider = load_example()
    script = [
        *HAPPY_PATH[:-1],
        call("sensitivity_analysis", run_ids=["run_01"]),
        call(
            "test_control",
            tests=[
                {
                    "run_id": "run_01",
                    "control_id": "limit_records_shared",
                    "parameter": 0.2,
                },
                {"run_id": "run_02", "control_id": "exit_plan_and_escrow"},
            ],
        ),
        submit_recommending,
    ]
    a, _ = agent(script)
    report = await a.run(ctx, provider)
    assert report.status == "complete"

    sens = next(t for t in report.trace if t.tool == "sensitivity_analysis")
    assert sens.summary.startswith("run_01: top driver")

    by_id = {c.run_id: c for c in report.controls}
    agent_tested = [c for c in report.controls if c.tested_by == "agent"]
    assert {c.control_id for c in agent_tested} == {
        "limit_records_shared",
        "exit_plan_and_escrow",
    }
    assert all(c.recommended for c in agent_tested)
    limited = next(c for c in agent_tested if c.control_id == "limit_records_shared")
    assert limited.parameter == 0.2 and limited.ale_after < limited.ale_before
    # catalog controls the agent did not test are tested in code, not recommended
    code = {c.control_id for c in report.controls if c.tested_by == "code"}
    assert {"require_soc2_type2", "client_held_encryption"} <= code
    assert not any(
        by_id[c.run_id].recommended for c in report.controls if c.tested_by == "code"
    )
    # recommended controls are listed first
    assert report.controls[0].recommended


async def test_control_run_cannot_be_final():
    ctx, provider = load_example()

    def submit_control_as_final(messages):
        cid = last_result(messages)["results"][0]["run_id"]
        return call("submit_report", final_run_ids=[cid], narrative="x")

    script = [
        call("get_baseline_inputs", scenarios=["vendor_breach"]),
        lambda m: call(
            "run_simulation", runs=[last_result(m)["baselines"][0]["inputs"]]
        ),
        call(
            "test_control",
            tests=[{"run_id": "run_01", "control_id": "require_soc2_type2"}],
        ),
        submit_control_as_final,
        submit_with(),
    ]
    a, _ = agent(script)
    report = await a.run(breach_only(ctx), provider)
    assert report.status == "complete"
    assert any("control what-ifs, not the scenario" in t.summary for t in report.trace)


async def test_bad_control_requests_are_reported():
    ctx, provider = load_example()
    script = [
        call("get_baseline_inputs", scenarios=["vendor_breach"]),
        lambda m: call(
            "run_simulation", runs=[last_result(m)["baselines"][0]["inputs"]]
        ),
        call(
            "test_control",
            tests=[
                {"run_id": "run_01", "control_id": "exit_plan_and_escrow"},
                {"run_id": "run_99", "control_id": "require_soc2_type2"},
            ],
        ),
    ]
    a, _ = agent(script, budget=Budget(max_llm_calls=3))
    report = await a.run(breach_only(ctx), provider)
    error = next(t.summary for t in report.trace if t.tool == "test_control")
    assert "applies to" in error and "unknown run id run_99" in error


async def test_fallback_report_still_lists_controls_and_drivers():
    ctx, provider = load_example()
    a, _ = agent([], budget=Budget(max_llm_calls=1))
    report = await a.run(ctx, provider)
    assert report.generated_by == "fallback"
    assert report.controls and all(c.tested_by == "code" for c in report.controls)
    assert all(s.drivers for s in report.scenarios)
    # the tolerance is derived in the example, so stating one comes first
    assert report.sharpen[0] == "risk_tolerance_usd"
    assert (
        json.loads(report.model_dump_json())["controls"][0]["combined_after"][
            "ale_mean"
        ]
        >= 0
    )
