"""
The FAIR (Factor Analysis of Information Risk) inputs for one loss scenario.

    Risk ($/yr)          = Loss Event Frequency x Loss Magnitude
    Loss Event Frequency = Threat Event Frequency x Vulnerability
    Loss Magnitude       = Primary loss + (Secondary loss, when it follows)

Every factor is a Range (min / most likely / max), never a single number, and
every Range says where it came from. The agent layer (fair/agent) checks that
each source id exists; this module only checks the format, so a hand-written
scenario file is validated the same way as one the agent builds.
"""

import math
import re
from collections.abc import Iterator
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# A threat event more than once a day is outside what this model is for.
MAX_THREAT_EVENTS_PER_YEAR = 365.0

# Where a value came from:
#   client          entered by the person running the analysis
#   assumption      a labelled guess; counted and shown in the report
#   ev_<id>         an evidence item a tool returned (fair/agent/ledger.py)
#   benchmark:<id>  a cited default from fair/data/benchmarks.json
#   derived:<id>    computed from other inputs, e.g. derived:altman_z
_SOURCE = re.compile(r"^(client|assumption|ev_\w+|benchmark:[\w.-]+|derived:[\w.-]+)$")


class Scenario(StrEnum):
    VENDOR_BREACH = "vendor_breach"
    VENDOR_INSOLVENCY = "vendor_insolvency"
    VENDOR_OUTAGE = "vendor_outage"
    SANCTIONS_EXIT = "sanctions_exit"


class PrimaryForm(StrEnum):
    """Losses that land directly on the organisation when the event happens."""

    PRODUCTIVITY = "productivity"  # work and revenue lost while things are down
    RESPONSE = "response"  # investigation, notification, legal, PR
    REPLACEMENT = "replacement"  # replacing the vendor, systems or assets


class SecondaryForm(StrEnum):
    """Losses caused by how others (regulators, customers, rivals) react."""

    FINES_JUDGMENTS = "fines_judgments"
    REPUTATION = "reputation"
    COMPETITIVE_ADVANTAGE = "competitive_advantage"


class Range(BaseModel):
    """
    A three-point estimate, sampled as a Beta-PERT distribution.

    min == mode == max is allowed and means a known, fixed value.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    min: float = Field(ge=0)
    mode: float = Field(ge=0)
    max: float = Field(ge=0)
    source: str
    rationale: str = ""

    @field_validator("min", "mode", "max")
    @classmethod
    def _finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("must be a finite number")
        return v

    @field_validator("source")
    @classmethod
    def _known_source(cls, v: str) -> str:
        if not _SOURCE.match(v):
            raise ValueError(
                "source must be 'client', 'assumption', 'ev_<id>', "
                f"'benchmark:<id>' or 'derived:<id>', got {v!r}"
            )
        return v

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if not self.min <= self.mode <= self.max:
            raise ValueError(
                f"need min <= mode <= max, got {self.min} / {self.mode} / {self.max}"
            )
        return self

    @classmethod
    def fixed(cls, value: float, source: str, rationale: str = "") -> "Range":
        return cls(min=value, mode=value, max=value, source=source, rationale=rationale)

    @property
    def is_assumption(self) -> bool:
        return self.source == "assumption"


class ProbabilityRange(Range):
    """A Range of probabilities: every point must be within 0..1."""

    @model_validator(mode="after")
    def _probability(self) -> Self:
        if self.max > 1:
            raise ValueError(f"a probability cannot exceed 1, got max {self.max}")
        return self


class ScenarioInputs(BaseModel):
    """Everything the simulation needs for one loss scenario."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario: Scenario
    label: str = ""

    # Loss Event Frequency = TEF x Vulnerability
    threat_event_frequency: Range  # events per year
    vulnerability: ProbabilityRange  # chance a threat event becomes a loss event

    # Loss Magnitude, in dollars per loss event
    primary_loss: dict[PrimaryForm, Range]
    secondary_probability: ProbabilityRange | None = None
    secondary_loss: dict[SecondaryForm, Range] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        # Imported here: scenarios.py imports this module
        from fair.model.scenarios import TEMPLATES

        if self.threat_event_frequency.max > MAX_THREAT_EVENTS_PER_YEAR:
            raise ValueError(
                f"threat_event_frequency max {self.threat_event_frequency.max} is above "
                f"{MAX_THREAT_EVENTS_PER_YEAR:g} events/year"
            )
        if not self.primary_loss:
            raise ValueError("primary_loss needs at least one loss form")
        if bool(self.secondary_loss) != (self.secondary_probability is not None):
            raise ValueError(
                "secondary_probability and secondary_loss go together: give both or neither"
            )

        template = TEMPLATES[self.scenario]
        bad_primary = set(self.primary_loss) - set(template.primary_forms)
        bad_secondary = set(self.secondary_loss) - set(template.secondary_forms)
        if bad_primary or bad_secondary:
            bad = sorted(str(f) for f in bad_primary | bad_secondary)
            raise ValueError(
                f"{self.scenario} does not use loss form(s): {', '.join(bad)}"
            )
        return self

    def ranges(self) -> Iterator[tuple[str, Range]]:
        """Every input with a dotted path, e.g. ('primary_loss.response', Range)."""
        yield "threat_event_frequency", self.threat_event_frequency
        yield "vulnerability", self.vulnerability
        for form, r in self.primary_loss.items():
            yield f"primary_loss.{form}", r
        if self.secondary_probability is not None:
            yield "secondary_probability", self.secondary_probability
        for sform, r in self.secondary_loss.items():
            yield f"secondary_loss.{sform}", r

    @property
    def assumption_count(self) -> int:
        return sum(1 for _, r in self.ranges() if r.is_assumption)
