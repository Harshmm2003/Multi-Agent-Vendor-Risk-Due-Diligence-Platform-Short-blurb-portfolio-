"""
VendorIQ as a host for the FAIR agent (fair/).

Maps an evaluation's pipeline state onto FAIR's ports: the five agents'
outputs become citable evidence, the scraped balance sheet becomes the Altman
Z input, and the client profile becomes the analysis context.

Token use matters here: the FAIR agent shares the Groq key with the five
agents. So findings are trimmed (top red flags and evidence, short texts),
and FAIR is skipped when a hard rule has already rejected the vendor, because
FAIR can only make a decision stricter (ADR 0005), never change a REJECT.
"""

import asyncio
from typing import Any, Literal

from langchain_groq import ChatGroq

from app.agents.meta_agent import _evaluate_dealbreakers, _resolve_ofac_status
from app.llm.config import PRIMARY_MODEL
from app.log import get_logger
from app.services.edgar_search import search_filings
from app.settings import get_settings
from fair.agent import Budget, FairAgent
from fair.context import AnalysisContext, Criticality, ExposureProfile
from fair.model.altman import FinancialStatement
from fair.model.taxonomy import Scenario
from fair.ports import EvidenceDraft, FilingTopic, FinancialData

log = get_logger(__name__)

DOMAINS = ("financial", "compliance", "operational", "reputation", "strategic")
MAX_ITEMS_PER_LIST = 4
MAX_TEXT = 220


def _short(text: Any, limit: int = MAX_TEXT) -> str:
    s = " ".join(str(text).split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


class VendorIQEvidenceProvider:
    """The five agents' outputs and the scraped financials, as FAIR evidence."""

    def __init__(
        self, state: dict, compliance_data: dict, financial_data: dict
    ) -> None:
        self._state = state
        self._compliance = compliance_data
        self._financial = financial_data

    def finding_domains(self) -> list[str]:
        return [d for d in DOMAINS if self._state.get(f"{d}_output")]

    async def agent_findings(self, domain: str) -> list[EvidenceDraft]:
        out = self._state.get(f"{domain}_output") or {}
        cite = f"{domain.capitalize()} agent"
        items = [
            EvidenceDraft(
                kind="finding",
                title=(
                    f"{domain.capitalize()} score {out.get('score', 0):.0f}/100 "
                    f"(confidence {out.get('confidence', 0):.0%})"
                ),
                detail=_short(out.get("explanation", "")),
                citation=cite,
            )
        ]
        for flag in (out.get("red_flags") or [])[:MAX_ITEMS_PER_LIST]:
            items.append(
                EvidenceDraft(
                    kind="finding",
                    title=_short(f"Red flag: {flag}", 160),
                    citation=cite,
                )
            )
        for fact in (out.get("evidence") or [])[:MAX_ITEMS_PER_LIST]:
            items.append(
                EvidenceDraft(kind="finding", title=_short(fact, 160), citation=cite)
            )
        if domain == "compliance":
            items += self._certifications()
        return items

    def _certifications(self) -> list[EvidenceDraft]:
        tiers = self._compliance.get("certification_evidence_tiers") or {}
        return [
            EvidenceDraft(
                kind="finding",
                title=f"Certification {cert}: {tier}",
                citation="Certification discovery (evidence tier)",
            )
            for cert, tier in list(tiers.items())[: MAX_ITEMS_PER_LIST * 2]
        ]

    async def financials(self) -> FinancialData | None:
        raw = dict(self._financial.get("_altman_inputs") or {})
        if not raw:
            return None
        manufacturer = bool(raw.pop("manufacturer", False))
        fields = set(FinancialStatement.model_fields)
        statement = FinancialStatement(**{k: v for k, v in raw.items() if k in fields})
        note = (
            f" ({self._financial['currency_note']})"
            if self._financial.get("currency_note")
            else ""
        )
        return FinancialData(
            statement=statement,
            manufacturer=manufacturer,
            citation=f"Yahoo Finance annual statements{note}",
        )

    async def search_filings(self, topic: FilingTopic) -> list[EvidenceDraft] | None:
        cik = self._compliance.get("edgar_cik")
        if not cik:
            return None  # not an SEC registrant (or not matched)
        hits = await asyncio.to_thread(search_filings, str(cik), topic)
        return [
            EvidenceDraft(
                kind="filing",
                title=f"SEC {h['form']} filed {h['filed']}: {h['description']}",
                detail=h["url"],
                citation="SEC EDGAR full-text search",
            )
            for h in hits
        ]


_APPETITE: dict[str, Literal["low", "medium", "high"]] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
}


def candidate_scenarios(
    exposure: ExposureProfile, ofac_status: str | None
) -> list[Scenario]:
    """
    Breach and insolvency always; outage when the service matters enough to
    hurt when it is down; sanctions exit when screening found a possible match.
    Fewer scenarios, fewer tokens.
    """
    out = [Scenario.VENDOR_BREACH, Scenario.VENDOR_INSOLVENCY]
    if exposure.revenue_at_risk_per_hour or exposure.service_criticality in (
        Criticality.HIGH,
        Criticality.CRITICAL,
    ):
        out.append(Scenario.VENDOR_OUTAGE)
    if ofac_status == "possible_match":
        out.append(Scenario.SANCTIONS_EXIT)
    return out


_OFAC: dict[str, Literal["clear", "possible_match", "match", "unknown"]] = {
    "clear": "clear",
    "possible_match": "possible_match",
    "match": "match",
    "unknown": "unknown",
}


def build_context(
    vendor_name: str,
    client_profile: dict,
    evaluation_id: str,
    ofac_status: str | None = None,
) -> AnalysisContext:
    years = client_profile.get("contract_years") or 1
    value = client_profile.get("contract_value")
    appetite = _APPETITE.get(
        str(client_profile.get("risk_appetite", "")).lower(), "medium"
    )
    exposure = ExposureProfile.model_validate(client_profile.get("exposure") or {})
    ofac = _OFAC.get(ofac_status or "")
    return AnalysisContext(
        subject=vendor_name,
        client_name=client_profile.get("company_name", ""),
        # The client's industry: the breached data and the fines are the client's
        industry=client_profile.get("industry", ""),
        annual_contract_value=value / years if value else None,
        risk_appetite=appetite,
        exposure=exposure,
        seed_key=evaluation_id,
        ofac_status=ofac,
        candidate_scenarios=candidate_scenarios(exposure, ofac),
    )


def skip_reason(state: dict) -> str | None:
    """Why FAIR cannot change this evaluation's outcome, if it cannot."""
    if _resolve_ofac_status(state, state.get("compliance_output")) == "match":
        return "OFAC sanctions match already rejects the vendor"
    deal = _evaluate_dealbreakers(
        state.get("strategic_output"), state.get("compliance_output")
    )
    if deal["status"] == "hard_reject":
        return "a dealbreaker already rejects the vendor"
    return None


async def run_fair_analysis(
    state: dict,
    client_profile: dict,
    compliance_data: dict,
    financial_data: dict,
) -> dict | None:
    """
    The FAIR report for this evaluation as JSON, or a {"skipped": reason}
    stub, or None when FAIR is switched off. Never raises: FAIR is an addition
    and must not fail an evaluation.
    """
    settings = get_settings()
    if not settings.fair_enabled:
        return None
    reason = skip_reason(state)
    if reason:
        log.info("fair.skipped", reason=reason)
        return {"skipped": reason}

    llm = ChatGroq(
        model=PRIMARY_MODEL,
        api_key=settings.groq_api_key,
        temperature=0,
        max_tokens=2_000,
        reasoning_effort=settings.fair_reasoning_effort,
    )
    agent = FairAgent(
        llm,
        budget=Budget(max_seconds=settings.fair_max_seconds),
        tokens_per_minute=settings.fair_tokens_per_minute or None,
    )
    context = build_context(
        state["vendor_name"],
        client_profile,
        state["evaluation_id"],
        _resolve_ofac_status(state, state.get("compliance_output")),
    )
    provider = VendorIQEvidenceProvider(state, compliance_data, financial_data)
    report = await agent.run(context, provider)
    log.info(
        "fair.done",
        status=report.status,
        generated_by=report.generated_by,
        tokens=report.input_tokens + report.output_tokens,
        llm_calls=report.llm_calls,
    )
    return report.model_dump(mode="json")
