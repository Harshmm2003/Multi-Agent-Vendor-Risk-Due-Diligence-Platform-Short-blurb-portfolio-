"""
Which inputs drive the loss: a one-at-a-time swing (tornado) analysis.

For each input range, re-run the scenario with that input pinned at its min,
then at its max, everything else unchanged. The swing in expected annual loss
says how much the result depends on that input, and so which uncertainty is
most worth reducing (or which control is most worth negotiating).

Runs share the base seed. Each input has its own random stream (see
simulation.py), so a swing reflects the input, not sampling noise.
"""

from pydantic import BaseModel

from fair.model.simulation import SimulationResult, simulate
from fair.model.taxonomy import ProbabilityRange, Range, ScenarioInputs

# Fewer iterations than a reported run: rankings settle quickly
SENSITIVITY_ITERATIONS = 4_000


class Driver(BaseModel):
    path: str
    low_ale: float  # expected annual loss with this input at its min
    high_ale: float  # ... at its max
    swing: float  # high_ale - low_ale
    share: float  # this swing / the sum of all swings


def with_range(inputs: ScenarioInputs, path: str, new: Range) -> ScenarioInputs:
    """A copy of inputs with the range at path replaced."""
    data = inputs.model_dump()
    node = data
    *parents, leaf = path.split(".")
    for key in parents:
        node = node[key]
    node[leaf] = new.model_dump()
    return ScenarioInputs.model_validate(data)


def _pinned(r: Range, value: float) -> Range:
    cls = ProbabilityRange if isinstance(r, ProbabilityRange) else Range
    return cls.fixed(value, r.source, r.rationale)


def drivers(
    base: SimulationResult, iterations: int = SENSITIVITY_ITERATIONS
) -> list[Driver]:
    """Inputs ranked by how much they move the expected annual loss."""
    rows = []
    for path, r in base.inputs.ranges():
        if r.min == r.max:
            continue  # a fixed value has nothing to swing
        lo = simulate(
            with_range(base.inputs, path, _pinned(r, r.min)),
            seed=base.seed,
            iterations=iterations,
        )
        hi = simulate(
            with_range(base.inputs, path, _pinned(r, r.max)),
            seed=base.seed,
            iterations=iterations,
        )
        rows.append((path, lo.distribution.ale_mean, hi.distribution.ale_mean))
    total = sum(abs(h - lo) for _, lo, h in rows) or 1.0
    ranked = sorted(rows, key=lambda row: -abs(row[2] - row[1]))
    return [
        Driver(
            path=p, low_ale=lo, high_ale=hi, swing=hi - lo, share=abs(hi - lo) / total
        )
        for p, lo, hi in ranked
    ]
