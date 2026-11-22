"""Input validation for FAIR scenarios: bad inputs must fail loudly."""

import math

import pytest
from pydantic import ValidationError

from fair.model import ProbabilityRange, Range, Scenario, ScenarioInputs


def rng(lo, mode, hi, source="assumption"):
    return {"min": lo, "mode": mode, "max": hi, "source": source}


def breach(**overrides):
    base = {
        "scenario": "vendor_breach",
        "threat_event_frequency": rng(1, 2, 4),
        "vulnerability": rng(0.05, 0.1, 0.2),
        "primary_loss": {"response": rng(1000, 2000, 5000)},
    }
    base.update(overrides)
    return base


def test_valid_scenario_parses():
    inputs = ScenarioInputs.model_validate(breach())
    assert inputs.scenario is Scenario.VENDOR_BREACH
    assert inputs.assumption_count == 3


@pytest.mark.parametrize(
    "values",
    [(3, 2, 4), (1, 5, 4), (-1, 0, 1), (0, 1, math.inf), (0, math.nan, 1)],
    ids=["min>mode", "mode>max", "negative", "infinite", "nan"],
)
def test_range_rejects_bad_numbers(values):
    with pytest.raises(ValidationError):
        Range(min=values[0], mode=values[1], max=values[2], source="assumption")


def test_fixed_range_is_allowed():
    r = Range.fixed(5, "client")
    assert (r.min, r.mode, r.max) == (5, 5, 5)


def test_probability_cannot_exceed_one():
    with pytest.raises(ValidationError, match="cannot exceed 1"):
        ProbabilityRange(min=0.5, mode=0.9, max=1.2, source="assumption")


@pytest.mark.parametrize(
    "source",
    ["client", "assumption", "ev_12", "benchmark:ibm.2025", "derived:altman_z"],
)
def test_known_sources_accepted(source):
    Range.fixed(1, source)


@pytest.mark.parametrize("source", ["", "Reuters", "ev_", "benchmark:", "my guess"])
def test_unknown_sources_rejected(source):
    with pytest.raises(ValidationError, match="source must be"):
        Range.fixed(1, source)


def test_secondary_probability_and_loss_go_together():
    with pytest.raises(ValidationError, match="go together"):
        ScenarioInputs.model_validate(breach(secondary_probability=rng(0.1, 0.2, 0.3)))
    with pytest.raises(ValidationError, match="go together"):
        ScenarioInputs.model_validate(
            breach(secondary_loss={"reputation": rng(1, 2, 3)})
        )


def test_primary_loss_required():
    with pytest.raises(ValidationError, match="at least one"):
        ScenarioInputs.model_validate(breach(primary_loss={}))


def test_loss_form_must_fit_the_scenario():
    bad = breach(
        scenario="vendor_insolvency",
        primary_loss={"response": rng(1, 2, 3)},  # insolvency has no response cost
    )
    with pytest.raises(ValidationError, match="does not use loss form"):
        ScenarioInputs.model_validate(bad)


def test_threat_frequency_capped():
    with pytest.raises(ValidationError, match="events/year"):
        ScenarioInputs.model_validate(breach(threat_event_frequency=rng(1, 2, 400)))


def test_unknown_fields_rejected():
    with pytest.raises(ValidationError):
        ScenarioInputs.model_validate(breach(threat_event_frequncy=rng(1, 2, 3)))


def test_ranges_lists_every_input_path():
    inputs = ScenarioInputs.model_validate(
        breach(
            secondary_probability=rng(0.1, 0.2, 0.3),
            secondary_loss={"fines_judgments": rng(1, 2, 3, source="client")},
        )
    )
    paths = [p for p, _ in inputs.ranges()]
    assert paths == [
        "threat_event_frequency",
        "vulnerability",
        "primary_loss.response",
        "secondary_probability",
        "secondary_loss.fines_judgments",
    ]
    assert inputs.assumption_count == 4
