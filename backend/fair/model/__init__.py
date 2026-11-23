"""The FAIR math engine: no LLM, no I/O, deterministic for a given seed."""

from fair.model.altman import AltmanResult, FinancialStatement, Zone, altman_z
from fair.model.scenarios import TEMPLATES, ScenarioTemplate
from fair.model.simulation import (
    DEFAULT_ITERATIONS,
    CombinedResult,
    LossDistribution,
    SimulationResult,
    combine,
    derive_seed,
    simulate,
)
from fair.model.taxonomy import (
    PrimaryForm,
    ProbabilityRange,
    Range,
    Scenario,
    ScenarioInputs,
    SecondaryForm,
)

__all__ = [
    "DEFAULT_ITERATIONS",
    "TEMPLATES",
    "AltmanResult",
    "CombinedResult",
    "FinancialStatement",
    "LossDistribution",
    "PrimaryForm",
    "ProbabilityRange",
    "Range",
    "Scenario",
    "ScenarioInputs",
    "ScenarioTemplate",
    "SecondaryForm",
    "SimulationResult",
    "Zone",
    "altman_z",
    "combine",
    "derive_seed",
    "simulate",
]
