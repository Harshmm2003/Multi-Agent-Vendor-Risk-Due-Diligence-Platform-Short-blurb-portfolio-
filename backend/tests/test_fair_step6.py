"""
Step 6: SEC filing search, outage and sanctions scenarios in VendorIQ.
EDGAR is faked (tests run offline); a live check is in edgar_search's docstring.
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.integrations.fair_adapter import (
    VendorIQEvidenceProvider,
    build_context,
    candidate_scenarios,
)
from app.services import edgar_search
from fair.context import ExposureProfile
from fair.model.taxonomy import Scenario

# Shape of a real efts.sec.gov response (Microsoft, January 2024)
EFTS = {
    "hits": {
        "hits": [
            {
                "_id": "0001193125-24-011295:d708866d8k.htm",
                "_source": {
                    "form": "8-K",
                    "file_date": "2024-01-19",
                    "items": ["1.05", "7.01", "9.01"],
                    "adsh": "0001193125-24-011295",
                },
            },
            {
                "_id": "0001193125-23-000001:other.htm",
                "_source": {
                    "form": "8-K",
                    "file_date": "2023-05-01",
                    "items": ["8.01"],  # mentions "Item 1.05" but is not that item
                    "adsh": "0001193125-23-000001",
                },
            },
        ]
    }
}


@pytest.fixture
def efts(monkeypatch):
    calls = []

    def fake_get(url, params, headers, timeout):
        calls.append(params)
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: EFTS)

    monkeypatch.setattr(edgar_search.requests, "get", fake_get)
    return calls


def test_cyber_incident_keeps_only_item_105(efts):
    hits = edgar_search.search_filings("789019", "cyber_incident")
    assert [h["filed"] for h in hits] == ["2024-01-19"]
    assert hits[0]["url"] == (
        "https://www.sec.gov/Archives/edgar/data/789019/000119312524011295/d708866d8k.htm"
    )
    [params] = efts
    assert params["forms"] == "8-K" and params["ciks"] == "0000789019"


def test_one_request_per_form(efts):
    edgar_search.search_filings("1", "going_concern")
    assert [p["forms"] for p in efts] == ["10-K", "10-Q"]  # the API rejects comma lists


def test_generic_user_agent_only():
    assert "@example.com" in edgar_search.EDGAR_HEADERS["User-Agent"]


def test_adapter_filings_and_non_filers(efts):
    filer = VendorIQEvidenceProvider({}, {"edgar_cik": "789019"}, {})
    [ev] = asyncio.run(filer.search_filings("cyber_incident"))
    assert ev.kind == "filing" and "SEC 8-K filed 2024-01-19" in ev.title
    assert ev.detail.startswith("https://www.sec.gov/")
    private = VendorIQEvidenceProvider({}, {}, {})
    assert asyncio.run(private.search_filings("cyber_incident")) is None


@pytest.mark.parametrize(
    "exposure, ofac, expected",
    [
        ({}, "clear", {"vendor_breach", "vendor_insolvency"}),
        (
            {"service_criticality": "critical"},
            "clear",
            {"vendor_breach", "vendor_insolvency", "vendor_outage"},
        ),
        (
            {"revenue_at_risk_per_hour": 500},
            None,
            {"vendor_breach", "vendor_insolvency", "vendor_outage"},
        ),
        (
            {},
            "possible_match",
            {"vendor_breach", "vendor_insolvency", "sanctions_exit"},
        ),
    ],
)
def test_candidate_scenarios(exposure, ofac, expected):
    got = candidate_scenarios(ExposureProfile.model_validate(exposure), ofac)
    assert {str(s) for s in got} == expected


def test_context_carries_ofac_status():
    profile = {
        "company_name": "N",
        "industry": "Retail",
        "contract_value": 1,
        "contract_years": 1,
    }
    ctx = build_context("V", profile, "e", "possible_match")
    assert ctx.ofac_status == "possible_match"
    assert Scenario.SANCTIONS_EXIT in ctx.candidate_scenarios
    assert build_context("V", profile, "e", "weird").ofac_status is None
