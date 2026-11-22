"""
The Monte Carlo engine: reproducible, and matching analytical values where
they exist. Fixed ranges (min == mode == max) remove sampling noise from the
magnitudes, so the expected loss is known exactly: rate x magnitude.
"""

from itertools import pairwise

import numpy as np
import pytest

from fair.model import (
    ProbabilityRange,
    Range,
    ScenarioInputs,
    combine,
    derive_seed,
    simulate,
)
from fair.model.distributions import pert_mean, sample_pert


def fixed(v):
    return Range.fixed(v, "assumption")


def prob(v):
    return ProbabilityRange.fixed(v, "assumption")


def breach(tef=2.0, vuln=0.5, response=100_000.0, **extra) -> ScenarioInputs:
    return ScenarioInputs(
        scenario="vendor_breach",
        threat_event_frequency=fixed(tef),
        vulnerability=prob(vuln),
        primary_loss={"response": fixed(response)},
        **extra,
    )


# ── Sampling ──────────────────────────────────────────────────────────────


def test_pert_stays_in_bounds_and_matches_its_mean():
    r = Range(min=10, mode=20, max=100, source="assumption")
    s = sample_pert(r, 200_000, np.random.default_rng(1))
    assert s.min() >= 10 and s.max() <= 100
    assert s.mean() == pytest.approx(pert_mean(r), rel=0.01)  # (10 + 80 + 100) / 6


def test_pert_of_fixed_range_is_constant():
    s = sample_pert(fixed(7), 100, np.random.default_rng(1))
    assert np.all(s == 7)


# ── Reproducibility ───────────────────────────────────────────────────────


def test_same_seed_same_result():
    a = simulate(breach(), seed=42, iterations=5_000)
    b = simulate(breach(), seed=42, iterations=5_000)
    assert a.model_dump() == b.model_dump()
    assert a.run_id == b.run_id


def test_different_seed_different_samples():
    a = simulate(breach(), seed=1, iterations=5_000)
    b = simulate(breach(), seed=2, iterations=5_000)
    assert not np.array_equal(a.distribution.samples, b.distribution.samples)


def test_derive_seed_is_stable():
    assert derive_seed("eval-1", "vendor_breach") == derive_seed(
        "eval-1", "vendor_breach"
    )
    assert derive_seed("eval-1", "vendor_breach") != derive_seed(
        "eval-2", "vendor_breach"
    )
    assert 0 <= derive_seed("x") < 2**63


def test_key_order_in_input_does_not_change_draws():
    forms_a = {
        "response": fixed(1000),
        "productivity": Range(min=0, mode=5, max=9, source="client"),
    }
    forms_b = dict(reversed(list(forms_a.items())))
    a = simulate(_with_forms(forms_a), seed=3, iterations=2_000)
    b = simulate(_with_forms(forms_b), seed=3, iterations=2_000)
    assert np.array_equal(a.distribution.samples, b.distribution.samples)


def _with_forms(forms) -> ScenarioInputs:
    return ScenarioInputs(
        scenario="vendor_breach",
        threat_event_frequency=fixed(2),
        vulnerability=prob(0.5),
        primary_loss=forms,
    )


# ── Correctness ───────────────────────────────────────────────────────────


def test_expected_loss_matches_rate_times_magnitude():
    # λ = 2 x 0.5 = 1 event/yr, $100K each → $100K/yr expected
    r = simulate(breach(), seed=7, iterations=100_000)
    assert r.loss_event_frequency_mean == pytest.approx(1.0, rel=0.02)
    assert r.distribution.ale_mean == pytest.approx(100_000, rel=0.02)


def test_secondary_loss_scaled_by_its_probability():
    # λ = 1; each event: $100K primary + 50% chance of $200K fines → $200K/yr
    inputs = breach(
        secondary_probability=prob(0.5),
        secondary_loss={"fines_judgments": fixed(200_000)},
    )
    r = simulate(inputs, seed=7, iterations=100_000)
    assert r.distribution.ale_mean == pytest.approx(200_000, rel=0.03)
    assert r.breakdown["secondary_loss.fines_judgments"] == pytest.approx(
        100_000, rel=0.04
    )


def test_zero_vulnerability_means_no_loss():
    r = simulate(breach(vuln=0.0), seed=7, iterations=5_000)
    d = r.distribution
    assert d.ale_mean == d.p99 == d.prob_any_loss == 0
    assert d.loss_exceedance_curve == []


def test_one_shot_scenario_happens_at_most_once():
    # 5 defaults/yr is nonsense, but the cap must hold: never more than one loss
    inputs = ScenarioInputs(
        scenario="vendor_insolvency",
        threat_event_frequency=fixed(5),
        vulnerability=prob(1),
        primary_loss={"replacement": fixed(300_000)},
    )
    r = simulate(inputs, seed=7, iterations=5_000)
    assert r.distribution.samples.max() == 300_000
    assert r.loss_event_frequency_mean <= 1


def test_percentiles_ordered_and_breakdown_adds_up():
    inputs = ScenarioInputs(
        scenario="vendor_breach",
        threat_event_frequency=Range(min=0.5, mode=2, max=6, source="assumption"),
        vulnerability=ProbabilityRange(
            min=0.02, mode=0.05, max=0.15, source="assumption"
        ),
        primary_loss={
            "response": Range(min=1e5, mode=5e5, max=2e6, source="assumption"),
            "productivity": Range(min=1e4, mode=1e5, max=5e5, source="assumption"),
        },
        secondary_probability=ProbabilityRange(
            min=0.3, mode=0.6, max=0.9, source="assumption"
        ),
        secondary_loss={
            "reputation": Range(min=1e5, mode=5e5, max=4e6, source="assumption")
        },
    )
    r = simulate(inputs, seed=11, iterations=20_000)
    d = r.distribution
    assert d.p10 <= d.p50 <= d.p90 <= d.p95 <= d.p99
    assert sum(r.breakdown.values()) == pytest.approx(d.ale_mean, rel=1e-9)


def test_loss_exceedance_curve_is_non_increasing():
    r = simulate(breach(tef=3, vuln=0.3, response=50_000), seed=5, iterations=10_000)
    curve = r.distribution.loss_exceedance_curve
    losses = [p.loss for p in curve]
    probs = [p.probability for p in curve]
    assert losses == sorted(losses)
    assert all(a >= b for a, b in pairwise(probs))
    assert probs[0] == pytest.approx(r.distribution.prob_any_loss)


def test_prob_exceeds_counts_reaching_the_threshold():
    # Fixed $50K per event: a year with exactly 2 events loses exactly $100K,
    # which counts as reaching a $100K tolerance
    r = simulate(breach(tef=3, vuln=0.3, response=50_000), seed=5, iterations=20_000)
    samples = r.distribution.samples
    assert r.distribution.prob_exceeds(100_000) == pytest.approx(
        np.mean(samples >= 100_000)
    )
    assert r.distribution.prob_exceeds(100_000) > np.mean(samples > 100_000)


def test_prob_exceeds_with_and_without_samples():
    inputs = ScenarioInputs(
        scenario="vendor_breach",
        threat_event_frequency=Range(min=1, mode=3, max=6, source="assumption"),
        vulnerability=ProbabilityRange(min=0.1, mode=0.3, max=0.5, source="assumption"),
        primary_loss={
            "response": Range(min=10_000, mode=50_000, max=200_000, source="assumption")
        },
    )
    r = simulate(inputs, seed=5, iterations=20_000)
    exact = r.distribution.prob_exceeds(100_000)

    # After a JSON round trip the samples are gone; the curve gives an estimate
    restored = type(r.distribution).model_validate_json(
        r.distribution.model_dump_json()
    )
    with pytest.raises(ValueError, match="not kept"):
        _ = restored.samples
    assert restored.prob_exceeds(100_000) == pytest.approx(exact, abs=0.05)


def test_combine_adds_scenarios():
    a = simulate(breach(response=100_000), seed=derive_seed("a"), iterations=20_000)
    b = simulate(breach(response=40_000), seed=derive_seed("b"), iterations=20_000)
    total = combine([a, b])
    assert total.distribution.ale_mean == pytest.approx(
        a.distribution.ale_mean + b.distribution.ale_mean
    )
    assert total.run_ids == [a.run_id, b.run_id]


def test_combine_rejects_mismatched_sizes():
    a = simulate(breach(), seed=1, iterations=1_000)
    b = simulate(breach(), seed=2, iterations=2_000)
    with pytest.raises(ValueError, match="different iteration counts"):
        combine([a, b])


def test_iterations_must_be_positive():
    with pytest.raises(ValueError):
        simulate(breach(), seed=1, iterations=0)
