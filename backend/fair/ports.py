"""
What a host (VendorIQ, the CLI, a test) provides to the FAIR agent.

The agent core depends only on these interfaces, never on a host's code, so
the same agent runs inside VendorIQ's pipeline and from the command line.
"""

from collections.abc import Awaitable, Callable
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from fair.model.altman import FinancialStatement

EvidenceKind = Literal["finding", "financial", "filing", "client", "benchmark", "note"]

# Regulatory-filing searches a host may support (VendorIQ: SEC EDGAR)
FilingTopic = Literal["cyber_incident", "going_concern", "privacy_order"]


class EvidenceDraft(BaseModel):
    """A fact a tool found, before the evidence store gives it an id."""

    model_config = ConfigDict(extra="forbid")

    kind: EvidenceKind
    title: str
    detail: str = ""
    citation: str  # where it came from, in words: "Compliance agent", "SEC 10-K 2025"


class Evidence(EvidenceDraft):
    id: str  # ev_<n>; what an input Range cites as its source


class FinancialData(BaseModel):
    statement: FinancialStatement
    manufacturer: bool = False
    citation: str


class EvidenceProvider(Protocol):
    def finding_domains(self) -> list[str]:
        """Domains get_agent_findings can be asked about, e.g. 'compliance'."""
        ...

    async def agent_findings(self, domain: str) -> list[EvidenceDraft]: ...

    async def financials(self) -> FinancialData | None: ...

    async def search_filings(self, topic: FilingTopic) -> list[EvidenceDraft] | None:
        """Matching filings; [] when none; None when the vendor files nowhere searchable."""
        ...


# Asks the person running the analysis a question and returns the answer.
# Only hosts with a person present (the interactive CLI) provide one.
AskUser = Callable[[str], Awaitable[str]]


class StaticEvidenceProvider:
    """Evidence given up front: for the CLI, tests and offline runs."""

    def __init__(
        self,
        findings: dict[str, list[EvidenceDraft]] | None = None,
        financials: FinancialData | None = None,
        filings: dict[str, list[EvidenceDraft]] | None = None,
    ) -> None:
        self._findings = findings or {}
        self._financials = financials
        self._filings = filings

    @classmethod
    def from_dict(cls, data: dict) -> "StaticEvidenceProvider":
        parsed = _StaticEvidence.model_validate(data)
        return cls(parsed.findings, parsed.financials, parsed.filings)

    def finding_domains(self) -> list[str]:
        return sorted(self._findings)

    async def agent_findings(self, domain: str) -> list[EvidenceDraft]:
        return list(self._findings.get(domain, []))

    async def financials(self) -> FinancialData | None:
        return self._financials

    async def search_filings(self, topic: FilingTopic) -> list[EvidenceDraft] | None:
        if self._filings is None:
            return None
        return list(self._filings.get(topic, []))


class _StaticEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: dict[str, list[EvidenceDraft]] = Field(default_factory=dict)
    financials: FinancialData | None = None
    filings: dict[str, list[EvidenceDraft]] | None = (
        None  # by FilingTopic; None = not a filer
    )
