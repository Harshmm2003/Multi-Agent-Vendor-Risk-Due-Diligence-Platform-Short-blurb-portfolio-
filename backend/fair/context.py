"""
What the FAIR agent is asked to analyse: the subject (a vendor), the client
it would serve, and the client's exposure. Every exposure field is optional;
missing ones fall back to derived values, benchmarks or labelled assumptions,
and the report counts how many inputs were assumed.
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from fair.model.taxonomy import Scenario

# Default tolerance when the client gives no dollar figure:
# risk appetite x annual contract value. A derived tolerance can trigger
# REVIEW in VendorIQ but never REJECT (ADR 0005).
TOLERANCE_MULTIPLIER = {"low": 0.5, "medium": 1.0, "high": 2.0}


class DataType(StrEnum):
    NONE = "none"
    INTERNAL = "internal"
    PII = "pii"
    PHI = "phi"
    PAYMENT = "payment"


SENSITIVE_DATA = {DataType.PII, DataType.PHI, DataType.PAYMENT}


class Criticality(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ExposureProfile(BaseModel):
    """What the client would put at risk with this vendor."""

    model_config = ConfigDict(extra="forbid")

    data_types: list[DataType] = Field(default_factory=list)
    records_shared: int | None = Field(default=None, ge=0)
    service_criticality: Criticality | None = None
    revenue_at_risk_per_hour: float | None = Field(default=None, ge=0)
    risk_tolerance_usd: float | None = Field(default=None, gt=0)
    liability_cap_usd: float | None = Field(default=None, ge=0)
    cyber_insurance_usd: float | None = Field(default=None, ge=0)


class Tolerance(BaseModel):
    value_usd: float
    source: Literal["client", "derived"]
    rationale: str


class AnalysisContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1)  # the vendor
    client_name: str = ""
    industry: str = ""
    annual_contract_value: float | None = Field(default=None, ge=0)
    risk_appetite: Literal["low", "medium", "high"] = "medium"
    exposure: ExposureProfile = Field(default_factory=ExposureProfile)
    candidate_scenarios: list[Scenario] = Field(
        default_factory=lambda: [Scenario.VENDOR_BREACH, Scenario.VENDOR_INSOLVENCY],
        min_length=1,
    )
    seed_key: str = ""  # default: subject; VendorIQ passes the evaluation id
    # The vendor's sanctions screening result, when the host has one; drives
    # the sanctions_exit baseline
    ofac_status: Literal["clear", "possible_match", "match", "unknown"] | None = None

    @property
    def seed_base(self) -> str:
        return self.seed_key or self.subject

    def tolerance(self) -> Tolerance | None:
        if self.exposure.risk_tolerance_usd is not None:
            return Tolerance(
                value_usd=self.exposure.risk_tolerance_usd,
                source="client",
                rationale="Stated by the client.",
            )
        if self.annual_contract_value:
            k = TOLERANCE_MULTIPLIER[self.risk_appetite]
            return Tolerance(
                value_usd=k * self.annual_contract_value,
                source="derived",
                rationale=(
                    f"Not stated; derived as {k:g} x annual contract value "
                    f"for '{self.risk_appetite}' risk appetite."
                ),
            )
        return None

    def missing_inputs(self) -> list[str]:
        """Exposure facts the client did not give, in the order worth asking."""
        e = self.exposure
        missing = []
        if not e.data_types:
            missing.append("data_types")
        if e.records_shared is None:
            missing.append("records_shared")
        if e.risk_tolerance_usd is None:
            missing.append("risk_tolerance_usd")
        if e.service_criticality is None:
            missing.append("service_criticality")
        if e.revenue_at_risk_per_hour is None:
            missing.append("revenue_at_risk_per_hour")
        if self.annual_contract_value is None:
            missing.append("annual_contract_value")
        if e.liability_cap_usd is None:
            missing.append("liability_cap_usd")
        if e.cyber_insurance_usd is None:
            missing.append("cyber_insurance_usd")
        return missing
