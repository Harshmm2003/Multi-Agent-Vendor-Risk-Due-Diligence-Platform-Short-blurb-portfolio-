"""
SEC EDGAR full-text search, for the FAIR agent's search_sec_filings tool.

Three fixed searches, not free text, so results stay relevant and the number
of tokens they add stays bounded:

  cyber_incident  8-K Item 1.05 "Material Cybersecurity Incidents" (required
                  since December 2023), matched on the filing's item list
  going_concern   10-K / 10-Q language on substantial doubt about continuing
                  as a going concern
  privacy_order   10-K mentions of a privacy-related consent order

The API accepts one form type per request (a comma-separated list returns an
error, and "8-K" already includes 8-K/A), so each form is searched on its own.

Uses the same generic User-Agent as edgar_pipeline (SEC asks automated
clients to identify themselves; no personal details are sent).
"""

from datetime import UTC, date, datetime, timedelta

import requests

from app.log import get_logger
from app.services.edgar_pipeline import EDGAR_HEADERS

log = get_logger(__name__)

SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
MAX_HITS = 5

PRESETS: dict[str, dict] = {
    "cyber_incident": {
        "q": '"Item 1.05"',
        "forms": ["8-K"],  # includes 8-K/A amendments
        "years": 5,
        "require_item": "1.05",
        "describe": "8-K Item 1.05 material cybersecurity incident disclosure",
    },
    "going_concern": {
        "q": '"substantial doubt" "going concern"',
        "forms": ["10-K", "10-Q"],
        "years": 3,
        "describe": "going-concern doubt language in a periodic report",
    },
    "privacy_order": {
        "q": '"consent order" privacy',
        "forms": ["10-K"],
        "years": 3,
        "describe": "privacy-related consent order mentioned in an annual report",
    },
}


def search_filings(cik: str, topic: str, today: date | None = None) -> list[dict]:
    """
    Filings by this company matching the preset. Each hit: form, filed date,
    items, url, description. Raises on network or HTTP errors (the caller
    turns that into a tool error the agent can see).
    """
    preset = PRESETS[topic]
    end = today or datetime.now(UTC).date()  # SEC dates are US; UTC is close enough
    start = end - timedelta(days=365 * preset["years"])
    raw = []
    for form in preset["forms"]:
        resp = requests.get(
            SEARCH_URL,
            params={
                "q": preset["q"],
                "forms": form,
                "ciks": cik.zfill(10),
                "dateRange": "custom",
                "startdt": start.isoformat(),
                "enddt": end.isoformat(),
            },
            headers=EDGAR_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        raw += resp.json().get("hits", {}).get("hits", [])

    hits = []
    for hit in sorted(
        raw, key=lambda h: h.get("_source", {}).get("file_date", ""), reverse=True
    ):
        src = hit.get("_source", {})
        items = src.get("items") or []
        if preset.get("require_item") and preset["require_item"] not in items:
            continue  # the phrase appeared, but the filing is not that item
        adsh = (src.get("adsh") or "").replace("-", "")
        filename = hit.get("_id", ":").split(":", 1)[-1]
        hits.append(
            {
                "form": src.get("form"),
                "filed": src.get("file_date"),
                "items": items,
                "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{adsh}/{filename}",
                "description": preset["describe"],
            }
        )
        if len(hits) >= MAX_HITS:
            break
    log.info("edgar_search.done", topic=topic, hits=len(hits))
    return hits
