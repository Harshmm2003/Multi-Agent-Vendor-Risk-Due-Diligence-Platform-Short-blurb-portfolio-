"""
Monte Carlo simulation of one FAIR scenario's annual loss.

Each iteration is one possible year:

    tef, vuln   ~ PERT                        uncertainty about the rate
    events      ~ Poisson(tef x vuln)         capped at 1 for one-shot scenarios
    per event:  each primary form             ~ PERT
                secondary follows?            ~ Bernoulli(secondary_probability)
                if so, each secondary form    ~ PERT
    annual loss = sum over that year's events

Each input draws from its own random stream, derived from the seed and the
input's name. The same inputs and seed always give the same result, and
changing one input never changes another input's draws ("common random
numbers"), so a what-if comparison shows the effect of the change, not noise. The samples are kept (not serialised) so
results can be combined across scenarios and queried for any threshold.
"""

import hashlib
from collections.abc import Sequence

import numpy as np
from pydantic import BaseModel, PrivateAttr

from fair.model.distributions import sample_pert
from fair.model.scenarios import TEMPLATES
from fair.model.taxonomy import PrimaryForm, Range, ScenarioInputs, SecondaryForm

DEFAULT_ITERATIONS = 10_000
LEC_POINTS = 50
_PERCENTILES = (10, 50, 90, 95, 99)


def derive_seed(*parts: str) -> int:
    """A stable 63-bit seed from text, e.g. derive_seed(evaluation_id, scenario)."""
    digest = hashlib.sha256("\x1f".join(parts).encode()).digest()
    return int.from_bytes(digest[:8], "big") >> 1


class LossExceedancePoint(BaseModel):
    loss: float
    probability: float  # chance the annual loss is at least `loss`


class LossDistribution(BaseModel):
    """Summary of simulated annual losses, in dollars per year."""

    iterations: int
    ale_mean: float  # annualised loss expectancy
    p10: float
    p50: float
    p90: float
    p95: float
    p99: float
    prob_any_loss: float
    loss_exceedance_curve: list[LossExceedancePoint]

    _samples: np.ndarray | None = PrivateAttr(default=None)

    @classmethod
    def from_samples(cls, samples: np.ndarray) -> "LossDistribution":
        p10, p50, p90, p95, p99 = (
            float(v) for v in np.percentile(samples, _PERCENTILES)
        )
        dist = cls(
            iterations=len(samples),
            ale_mean=float(samples.mean()),
            p10=p10,
            p50=p50,
            p90=p90,
            p95=p95,
            p99=p99,
            prob_any_loss=float(np.mean(samples > 0)),
            loss_exceedance_curve=_exceedance_curve(samples),
        )
        dist._samples = samples
        return dist

    @property
    def samples(self) -> np.ndarray:
        if self._samples is None:
            raise ValueError(
                "samples are not kept after serialisation; re-run the simulation"
            )
        return self._samples

    def prob_exceeds(self, threshold: float) -> float:
        """
        Chance that a year's loss reaches threshold (loss >= threshold), the
        same reading as the loss exceedance curve. A loss exactly equal to a
        tolerance counts as reaching it: the cautious choice.
        """
        if self._samples is not None:
            return float(np.mean(self._samples >= threshold))
        # Deserialised result: read it off the curve (approximate)
        if not self.loss_exceedance_curve:
            return 0.0
        losses = [p.loss for p in self.loss_exceedance_curve]
        probs = [p.probability for p in self.loss_exceedance_curve]
        return float(
            np.interp(threshold, losses, probs, left=self.prob_any_loss, right=0.0)
        )


def _exceedance_curve(samples: np.ndarray) -> list[LossExceedancePoint]:
    positive = samples[samples > 0]
    if positive.size == 0:
        return []
    lo, hi = float(positive.min()), float(positive.max())
    thresholds = np.array([lo]) if lo == hi else np.geomspace(lo, hi, LEC_POINTS)
    ordered = np.sort(samples)
    at_least = 1 - np.searchsorted(ordered, thresholds, side="left") / len(samples)
    return [
        LossExceedancePoint(loss=float(t), probability=float(p))
        for t, p in zip(thresholds, at_least, strict=True)
    ]


class SimulationResult(BaseModel):
    run_id: str
    inputs: ScenarioInputs
    seed: int
    loss_event_frequency_mean: float  # loss events per year, after any one-shot cap
    breakdown: dict[
        str, float
    ]  # mean annual loss per input path, e.g. primary_loss.response
    distribution: LossDistribution


class CombinedResult(BaseModel):
    """Several scenarios' losses added year by year (assumed independent)."""

    run_ids: list[str]
    mean_by_run: dict[str, float]
    distribution: LossDistribution


def simulate(
    inputs: ScenarioInputs,
    *,
    seed: int,
    iterations: int = DEFAULT_ITERATIONS,
    run_id: str | None = None,
) -> SimulationResult:
    if iterations < 1:
        raise ValueError("iterations must be at least 1")
    n = iterations

    def rng(stream: str) -> np.random.Generator:
        return np.random.default_rng([seed, _stream_id(stream)])

    tef = _pert(inputs.threat_event_frequency, n, rng("threat_event_frequency"))
    vuln = _pert(inputs.vulnerability, n, rng("vulnerability"))
    events = rng("events").poisson(tef * vuln)
    if TEMPLATES[inputs.scenario].one_shot:
        events = np.minimum(events, 1)

    total_events = int(events.sum())
    owner = np.repeat(np.arange(n), events)  # which year each event falls in
    annual = np.zeros(n)
    breakdown: dict[str, float] = {}

    def add(path: str, per_event: np.ndarray) -> None:
        nonlocal annual
        per_year = np.bincount(owner, weights=per_event, minlength=n)
        annual += per_year
        breakdown[path] = float(per_year.mean())

    # Canonical enum order, not dict order, so key order in a file cannot
    # change the draws
    for form in PrimaryForm:
        if form in inputs.primary_loss:
            path = f"primary_loss.{form}"
            add(path, _pert(inputs.primary_loss[form], total_events, rng(path)))

    if inputs.secondary_probability is not None:
        slp = _pert(inputs.secondary_probability, n, rng("secondary_probability"))
        follows = rng("secondary_follows").random(total_events) < slp[owner]
        for sform in SecondaryForm:
            if sform in inputs.secondary_loss:
                path = f"secondary_loss.{sform}"
                magnitude = _pert(inputs.secondary_loss[sform], total_events, rng(path))
                add(path, magnitude * follows)

    return SimulationResult(
        run_id=run_id or _default_run_id(inputs, seed, n),
        inputs=inputs,
        seed=seed,
        loss_event_frequency_mean=float(events.mean()),
        breakdown=breakdown,
        distribution=LossDistribution.from_samples(annual),
    )


def combine(results: Sequence[SimulationResult]) -> CombinedResult:
    """
    Total annual loss across scenarios. Assumes the scenarios are independent,
    which holds when each was simulated with its own seed.
    """
    if not results:
        raise ValueError("nothing to combine")
    sizes = {r.distribution.iterations for r in results}
    if len(sizes) != 1:
        raise ValueError(f"results have different iteration counts: {sorted(sizes)}")
    total = np.sum([r.distribution.samples for r in results], axis=0)
    return CombinedResult(
        run_ids=[r.run_id for r in results],
        mean_by_run={r.run_id: r.distribution.ale_mean for r in results},
        distribution=LossDistribution.from_samples(total),
    )


def _pert(r: Range, n: int, rng: np.random.Generator) -> np.ndarray:
    return sample_pert(r, n, rng) if n else np.zeros(0)


def _stream_id(name: str) -> int:
    """A stable number per input, so each input draws from its own stream."""
    return int.from_bytes(hashlib.sha256(name.encode()).digest()[:4], "big")


def _default_run_id(inputs: ScenarioInputs, seed: int, n: int) -> str:
    key = f"{inputs.model_dump_json()}|{seed}|{n}"
    return "run_" + hashlib.sha256(key.encode()).hexdigest()[:10]
