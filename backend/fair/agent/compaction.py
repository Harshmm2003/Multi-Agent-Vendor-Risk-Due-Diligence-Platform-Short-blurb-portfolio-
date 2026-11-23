"""
Shrink tool results the model has already read.

Every model call resends the whole conversation. A baseline or a findings
list is needed in full once, when the model acts on it; after that a short
form is enough (ids to cite, figures to quote). Compacting old results keeps
each call small without the model losing anything it still needs.

Rules (a result is compacted only after the model has seen it once):
  - errors and simulation results: never compacted (short, and quoted later)
  - findings: keep ids and titles, drop details
  - benchmarks: keep source, value, unit
  - financials / Altman: keep the evidence id and the headline figures
  - baseline inputs: kept in full until a simulation of that scenario exists
"""

from typing import Any

from fair.agent.session import Session


def compact(name: str, payload: dict, session: Session) -> dict | None:
    """The short form of a seen tool result, or None to keep it in full."""
    if "error" in payload or name == "run_simulation":
        return None
    if name == "get_agent_findings":
        return {
            "results": [
                {
                    "domain": r["domain"],
                    "evidence": [
                        {"id": e["id"], "title": e["title"]} for e in r["evidence"]
                    ],
                }
                for r in payload.get("results", [])
            ]
        }
    if name == "search_sec_filings":
        if "results" not in payload:
            return None
        return {
            "results": [
                {
                    "topic": r["topic"],
                    "filings": [
                        {"id": f["id"], "title": f["title"]}
                        for f in r.get("filings", [])
                    ],
                }
                for r in payload["results"]
            ]
        }
    if name == "lookup_benchmark":
        return {
            "benchmarks": [
                {k: b[k] for k in ("source", "value", "unit")}
                for b in payload.get("benchmarks", [])
            ]
        }
    if name == "get_financials":
        return _pick(payload, ("available", "evidence_id", "citation", "altman"))
    if name == "get_baseline_inputs":
        simulated = {str(r.inputs.scenario) for r in session.ledger.results()}
        baselines = payload.get("baselines", [])
        if not any(b["inputs"]["scenario"] in simulated for b in baselines):
            return None
        return {
            "baselines": [
                {
                    "scenario": b["inputs"]["scenario"],
                    "seen": "already used in a simulation",
                }
                if b["inputs"]["scenario"] in simulated
                else b
                for b in baselines
            ]
        }
    return None


def _pick(payload: dict, keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: payload[k] for k in keys if k in payload}
