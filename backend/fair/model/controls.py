"""
Controls: contract terms or measures a client can ask for, and how each one
changes a scenario's FAIR inputs.

The effect sizes are planning assumptions, not measured results: published
data on how much a given contract clause reduces vendor loss is scarce. Each
control says so in the rationale it writes onto the ranges it changes, and
those ranges are sourced "assumption", so they count towards the report's
assumption share. Replace them with your own control-effectiveness data.
"""

from collections.abc import Callable
from dataclasses import dataclass

from fair.model.taxonomy import ProbabilityRange, Range, Scenario, ScenarioInputs


@dataclass(frozen=True)
class Control:
    id: str
    title: str
    description: str
    scenarios: tuple[Scenario, ...]
    # (inputs, parameter) -> changed inputs
    apply: Callable[[ScenarioInputs, float | None], ScenarioInputs]
    parameter: str | None = None  # what the optional number means, if used


def _scale(r: Range, k: float, why: str) -> Range:
    cls = ProbabilityRange if isinstance(r, ProbabilityRange) else Range
    top = min(r.max * k, 1.0) if cls is ProbabilityRange else r.max * k
    # Keep the original source and add "assumption" for the control's effect
    source = r.source if "assumption" in r.source_ids else f"{r.source}, assumption"
    return cls(
        min=r.min * k,
        mode=min(r.mode * k, top),
        max=top,
        source=source,
        rationale=f"{why}. Before the control: {r.rationale}"[:500],
    )


def _scale_paths(
    inputs: ScenarioInputs, factors: dict[str, float], why: str
) -> ScenarioInputs:
    data = inputs.model_dump()
    for path, rng in inputs.ranges():
        head = path.split(".")[0]
        k = factors.get(path, factors.get(head))
        if k is None:
            continue
        node = data
        *parents, leaf = path.split(".")
        for key in parents:
            node = node[key]
        node[leaf] = _scale(rng, k, why).model_dump()
    return ScenarioInputs.model_validate(data)


def _soc2(inputs: ScenarioInputs, _: float | None) -> ScenarioInputs:
    return _scale_paths(
        inputs,
        {"threat_event_frequency": 0.7},
        "Control assumption: requiring a current SOC 2 Type 2 report and annual "
        "re-attestation cuts loss events by about 30%",
    )


def _limit_records(inputs: ScenarioInputs, share: float | None) -> ScenarioInputs:
    k = 0.5 if share is None else share
    if not 0 < k <= 1:
        raise ValueError("share of records still shared must be in (0, 1]")
    return _scale_paths(
        inputs,
        {"primary_loss": k, "secondary_loss": k},
        f"Control assumption: sharing only {k:.0%} of the records scales breach "
        "costs in proportion",
    )


def _encryption(inputs: ScenarioInputs, _: float | None) -> ScenarioInputs:
    return _scale_paths(
        inputs,
        {"primary_loss.response": 0.6, "secondary_loss": 0.5},
        "Control assumption: data encrypted with client-held keys makes a vendor "
        "breach far less damaging (response cost -40%, secondary loss -50%)",
    )


def _exit_plan(inputs: ScenarioInputs, _: float | None) -> ScenarioInputs:
    return _scale_paths(
        inputs,
        {"primary_loss.replacement": 0.5, "primary_loss.productivity": 0.5},
        "Control assumption: a tested exit plan with data-export and escrow terms "
        "halves switching cost and disruption",
    )


def _fallback_service(inputs: ScenarioInputs, _: float | None) -> ScenarioInputs:
    return _scale_paths(
        inputs,
        {"vulnerability": 0.5},
        "Control assumption: a fallback service or manual process halves the "
        "chance an outage turns into a business loss",
    )


CONTROLS: dict[str, Control] = {
    c.id: c
    for c in (
        Control(
            "require_soc2_type2",
            "Require SOC 2 Type 2 with annual re-attestation",
            "Contract requires a current SOC 2 Type 2 report every year.",
            (Scenario.VENDOR_BREACH,),
            _soc2,
        ),
        Control(
            "limit_records_shared",
            "Share fewer records with the vendor",
            "Data minimisation: send the vendor only the records it needs.",
            (Scenario.VENDOR_BREACH,),
            _limit_records,
            parameter="share of today's records still shared, 0-1 (default 0.5)",
        ),
        Control(
            "client_held_encryption",
            "Encrypt shared data with client-held keys",
            "The vendor stores the client's data encrypted under keys the client controls.",
            (Scenario.VENDOR_BREACH,),
            _encryption,
        ),
        Control(
            "exit_plan_and_escrow",
            "Exit plan, data export and escrow terms",
            "Contractual exit support, regular data exports and source/data escrow.",
            (Scenario.VENDOR_INSOLVENCY, Scenario.SANCTIONS_EXIT),
            _exit_plan,
        ),
        Control(
            "fallback_service",
            "Fallback service or manual workaround",
            "A second provider or a documented manual process for outages.",
            (Scenario.VENDOR_OUTAGE,),
            _fallback_service,
        ),
    )
}


def apply_control(
    control_id: str, inputs: ScenarioInputs, parameter: float | None = None
) -> ScenarioInputs:
    control = CONTROLS.get(control_id)
    if control is None:
        raise KeyError(f"unknown control {control_id!r}; known: {', '.join(CONTROLS)}")
    if inputs.scenario not in control.scenarios:
        raise ValueError(
            f"{control_id} applies to {', '.join(control.scenarios)}, not {inputs.scenario}"
        )
    changed = control.apply(inputs, parameter)
    return changed.model_copy(update={"label": f"With control: {control.title}"})
