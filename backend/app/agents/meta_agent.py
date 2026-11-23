import re

from pydantic import ValidationError

from app.orchestrator.state import VendorRiskState
from fair.agent.report import FairReport
from fair.decision import FairDecisionSignals, signals_from_report
from fair.format import format_percent, format_usd

TRACKED_CERTS = [
    "SOC1", "SOC2", "SOC3", "ISO27001", "ISO27701", "ISO9001", "ISO14001",
    "ISO45001", "ISO13485", "ISO22301", "ISO20000", "ISO42001", "GDPR",
    "CCPA", "HIPAA", "PCI_DSS", "FedRAMP", "NIST_CSF", "SOX", "FCPA", "CSA_STAR",
]


def _normalize(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper())


# ── Decision thresholds ───────────────────────────────────
APPROVE_MIN_COMPOSITE  = 70
REJECT_BELOW_COMPOSITE = 45
# Auto-approval requires every agent to report this confidence on average —
# matches the golden dataset's label criteria ("confidence > 95%").
APPROVE_MIN_CONFIDENCE = 0.95
# A low composite only auto-rejects if the agents are at least this sure of
# it; below this, a low score may just reflect thin data → human review.
REJECT_MIN_CONFIDENCE  = 0.50

# ── FAIR overlay (ADR 0005) ───────────────────────────────
# The FAIR agent's dollar figures are applied after the rules above and can
# only make the decision stricter. Rare events have P90 = $0, so the rules use
# expected loss and the chance of reaching the tolerance, not P90.
FAIR_REVIEW_PROB_EXCEEDS  = 0.10  # losses reach tolerance in 1 year in 10, or more often
FAIR_LOSS_TO_CONTRACT_MAX = 1.0   # expected yearly loss above the yearly fee
SEVERITY = {"APPROVE": 0, "REVIEW": 1, "REJECT": 2}

TIER_RANK = {
    "VERIFIED_REGISTRY": 3,
    "CORROBORATED": 2,
    "SELF_REPORTED_UNVERIFIABLE": 1,
    "CONTRADICTED": 0,
}


def _find_cert_keyword_in_text(text: str) -> str | None:
    norm_text = _normalize(text)
    for cert in TRACKED_CERTS:
        if _normalize(cert) in norm_text:
            return cert
    return None


_SOC_REPORT_TYPE = re.compile(r"\btype[\s_-]*(ii|2|i|1)\b", re.IGNORECASE)


def _canonical_cert_key(key: str) -> str:
    """"SOC2_Type2", "SOC 2 Type II" → "SOC2TYPE2"."""
    norm = _normalize(key)
    norm = re.sub(r"TYPEII$", "TYPE2", norm)
    return re.sub(r"TYPEI$", "TYPE1", norm)


def _accepted_cert_prefixes(cert: str, dealbreaker_text: str) -> list[str]:
    """
    Which tier keys can satisfy a dealbreaker. For SOC 2 the report type
    matters: a Type 1 report covers control design at a point in time, a
    Type 2 report covers operating effectiveness over a period. A dealbreaker
    requiring Type 2 is satisfied only by Type 2; one requiring Type 1 by
    either; one naming no type by any SOC 2 report.
    """
    family = _normalize(cert)
    if family != "SOC2":
        return [family]
    match = _SOC_REPORT_TYPE.search(dealbreaker_text or "")
    if not match:
        return [family]
    if match.group(1).lower() in ("ii", "2"):
        return ["SOC2TYPE2"]
    return ["SOC2TYPE1", "SOC2TYPE2"]


def _tier_for_cert(cert: str | None, tiers: dict, dealbreaker_text: str = "") -> str | None:
    """
    Dealbreaker text names a cert family ("SOC2"), but tiers are keyed by the
    specific cert ("SOC2_Type2"). Return the strongest tier among the keys
    that can satisfy the dealbreaker. Previously tiers.get("SOC2") always
    missed, so a SOC2 dealbreaker was hard-rejected even with a verified
    SOC2_Type2.
    """
    if not cert:
        return None
    prefixes = _accepted_cert_prefixes(cert, dealbreaker_text)
    candidates = [
        tier for key, tier in tiers.items()
        if any(_canonical_cert_key(key).startswith(p) for p in prefixes)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda t: TIER_RANK.get(t, -1))


def _resolve_ofac_status(state: dict, compliance_output: dict | None) -> str:
    """
    The deterministic OFAC screen lives in compliance_data and is the source
    of truth — it must not depend on the compliance LLM agent succeeding.
    Falls back to the agent's sanctions_clear only for states built without
    compliance_data (older callers / scripts).
    """
    status = (state.get("compliance_data") or {}).get("ofac_status")
    if status:
        return status
    if compliance_output is not None:
        sanctions_clear = compliance_output.get("sanctions_clear")
        if sanctions_clear is True:
            return "clear"
        if sanctions_clear is False:
            return "match"
    return "unavailable"


def _evaluate_dealbreakers(strategic_output: dict | None, compliance_output: dict | None) -> dict:
    """
    Resolves strategic_output['dealbreakers_triggered'] — previously ignored
    entirely by the recommendation logic — into hard_reject / needs_review /
    clear.

    Forward-compatible with the evidence-tier system (registry connectors +
    self-attestation, planned next): if compliance_output carries a
    `certification_evidence_tiers` dict mapping a tracked cert keyword to
    VERIFIED_REGISTRY / CORROBORATED / SELF_REPORTED_UNVERIFIABLE /
    CONTRADICTED, a triggered dealbreaker matching a tracked cert is resolved
    against that tier instead of being an automatic reject. Until that data
    exists upstream, every cert-matching dealbreaker falls through to the
    same safe default as a non-cert dealbreaker: hard reject. That default
    is intentional — never auto-approve or silently downgrade a dealbreaker
    without positive evidence to justify it.
    """
    if not strategic_output:
        return {"status": "clear", "reason": None}

    triggered = strategic_output.get("dealbreakers_triggered") or []
    if isinstance(triggered, str):
        # LLM returned a bare string — don't iterate it character by character
        triggered = [triggered]
    triggered = [str(d) for d in triggered if str(d).strip()]
    if not triggered:
        return {"status": "clear", "reason": None}

    tiers = (compliance_output or {}).get("certification_evidence_tiers") or {}

    unresolved, needs_review, cleared = [], [], []

    for dealbreaker in triggered:
        cert = _find_cert_keyword_in_text(dealbreaker)
        tier = _tier_for_cert(cert, tiers, dealbreaker)

        if tier in ("VERIFIED_REGISTRY", "CORROBORATED"):
            cleared.append(dealbreaker)
        elif tier == "SELF_REPORTED_UNVERIFIABLE":
            needs_review.append(dealbreaker)
        else:
            unresolved.append(dealbreaker)

    if unresolved:
        return {
            "status": "hard_reject",
            "reason": (
                "Client-specified dealbreaker(s) triggered with no verifying "
                f"evidence: {', '.join(unresolved)}."
            ),
        }
    if needs_review:
        return {
            "status": "needs_review",
            "reason": (
                "Dealbreaker(s) triggered but only self-reported, with no "
                f"independent corroboration available: {', '.join(needs_review)}. "
                "Expected for evidence types (e.g. SOC reports) with no public "
                "registry — route to human reviewer rather than auto-reject "
                "or auto-approve."
            ),
        }
    return {
        "status": "clear",
        "reason": (
            f"Dealbreaker(s) initially flagged but confirmed satisfied on "
            f"verification: {', '.join(cleared)}."
        ),
    }


async def run_meta_agent(state: VendorRiskState) -> dict:
    """
    Aggregates all agent scores into a final decision.
    Applies company-defined weights.
    Produces composite score, risk score, fit score,
    recommendation, and CPO-ready explanation.
    """

    weights = {
        "financial":   state.get("weight_financial",   0.30),
        "compliance":  state.get("weight_compliance",  0.25),
        "operational": state.get("weight_operational", 0.20),
        "reputation":  state.get("weight_reputation",  0.10),
        "strategic":   state.get("weight_strategic",   0.15),
    }

    outputs = {
        "financial":   state.get("financial_output"),
        "compliance":  state.get("compliance_output"),
        "operational": state.get("operational_output"),
        "reputation":  state.get("reputation_output"),
        "strategic":   state.get("strategic_output"),
    }

    # ── Redistribute weights for missing agents ───────────
    available       = {k: v for k, v in outputs.items() if v is not None}
    missing         = {k: v for k, v in outputs.items() if v is None}
    missing_weight  = sum(weights[k] for k in missing)

    total_avail = sum(weights[k] for k in available)
    if missing_weight > 0 and total_avail > 0:
        for k in available:
            weights[k] += weights[k] / total_avail * missing_weight

    # ── Composite score ───────────────────────────────────
    composite = sum(
        outputs[k]["score"] * weights[k]
        for k in available
    )

    # ── Risk score (Financial 55% + Compliance 45%) ───────
    fin_score  = outputs["financial"]["score"]  if outputs.get("financial")  else 50
    comp_score = outputs["compliance"]["score"] if outputs.get("compliance") else 50
    risk_score = (fin_score * 0.55) + (comp_score * 0.45)

    # ── Fit score (Strategic 50% + Operational 35% + Reputation 15%) ──
    strat_score = outputs["strategic"]["score"]   if outputs.get("strategic")   else 50
    ops_score   = outputs["operational"]["score"] if outputs.get("operational") else 50
    rep_score   = outputs["reputation"]["score"]  if outputs.get("reputation")  else 50
    fit_score   = (strat_score * 0.50) + (ops_score * 0.35) + (rep_score * 0.15)

    # ── Confidence aggregation ────────────────────────────
    confidences = [outputs[k]["confidence"] for k in available]
    avg_conf    = sum(confidences) / len(confidences) if confidences else 0

    # Apply penalty if any agent is low confidence
    if any(c < 0.5 for c in confidences):
        avg_conf *= 0.85

    # ── OFAC status (deterministic screen, independent of the LLM agent) ──
    ofac_status  = _resolve_ofac_status(state, outputs.get("compliance"))
    ofac_matches = (state.get("compliance_data") or {}).get("ofac_matches") or []
    ofac_detail  = f" SDN entries: {'; '.join(ofac_matches)}." if ofac_matches else ""

    # ── Dealbreaker override (previously ignored — see _evaluate_dealbreakers) ──
    dealbreaker_result = _evaluate_dealbreakers(
        outputs.get("strategic"), outputs.get("compliance")
    )

    unavailable_agents = sorted(missing)
    # Salvaged from truncated JSON (see base_agent.parse_agent_output): the
    # score and confidence are real, but anything after the cut-off — such
    # as red flags — may be lost, so the evaluation can't be trusted to
    # auto-approve or auto-reject.
    truncated_agents = sorted(k for k in available if outputs[k].get("truncated"))

    # ── Recommendation logic ──────────────────────────────
    # Order matters: hard rejects first, then anything that blocks an
    # automated decision, then score-based outcomes. The confidence gate only
    # guards APPROVE — it previously ran before the score checks, so a vendor
    # scoring 20/100 at 80% confidence came out as REVIEW instead of REJECT.
    if ofac_status == "match":
        recommendation = "REJECT"
        decision_reason = (
            "OFAC sanctions match detected. "
            "Vendor must be rejected immediately — no exceptions." + ofac_detail
        )
    elif dealbreaker_result["status"] == "hard_reject":
        recommendation = "REJECT"
        decision_reason = dealbreaker_result["reason"]
    elif ofac_status == "possible_match":
        recommendation = "REVIEW"
        decision_reason = (
            "Possible OFAC sanctions match — the vendor name overlaps SDN list "
            "entries, or matches one exactly but is too short or generic to "
            "confirm on name alone. A compliance officer must confirm or clear "
            "the match before any decision." + ofac_detail
        )
    elif ofac_status != "clear":
        recommendation = "REVIEW"
        decision_reason = (
            "OFAC sanctions screening could not be completed (SDN list "
            "unavailable). Screen manually before proceeding."
        )
    elif dealbreaker_result["status"] == "needs_review":
        recommendation = "REVIEW"
        decision_reason = dealbreaker_result["reason"]
    elif unavailable_agents:
        # Missing agents are excluded from the confidence average, so without
        # this gate a partial evaluation could auto-approve or auto-reject.
        recommendation = "REVIEW"
        decision_reason = (
            f"{len(unavailable_agents)} of {len(outputs)} analysis agents did not "
            f"produce a usable result ({', '.join(unavailable_agents)}). "
            f"The evaluation is incomplete — route to a human reviewer."
        )
    elif truncated_agents:
        recommendation = "REVIEW"
        decision_reason = (
            f"Output from {', '.join(truncated_agents)} was truncated — its "
            f"score was recovered, but red flags and evidence after the "
            f"cut-off may be missing. Route to a human reviewer."
        )
    elif composite < REJECT_BELOW_COMPOSITE and avg_conf >= REJECT_MIN_CONFIDENCE:
        recommendation = "REJECT"
        decision_reason = (
            f"Composite score of {composite:.1f} is below the minimum "
            f"acceptable threshold. Do not proceed with this vendor."
        )
    elif composite < REJECT_BELOW_COMPOSITE:
        recommendation = "REVIEW"
        decision_reason = (
            f"Composite score of {composite:.1f} is below the rejection "
            f"threshold, but confidence ({avg_conf:.0%}) is too low to "
            f"auto-reject — the low score may reflect missing data."
        )
    elif avg_conf < APPROVE_MIN_CONFIDENCE:
        recommendation = "REVIEW"
        decision_reason = (
            f"Confidence ({avg_conf:.0%}) is below the "
            f"{APPROVE_MIN_CONFIDENCE:.0%} auto-approval threshold. "
            f"Route to human reviewer before proceeding."
        )
    elif composite >= APPROVE_MIN_COMPOSITE:
        recommendation = "APPROVE"
        decision_reason = (
            f"Composite score of {composite:.1f} with {avg_conf:.0%} "
            f"confidence meets approval criteria."
        )
    else:
        recommendation = "REVIEW"
        decision_reason = (
            f"Composite score of {composite:.1f} falls in the review band. "
            f"Proceed with caution — additional due diligence recommended."
        )

    # ── FAIR overlay: stricter of the two, never looser ───
    base_recommendation = recommendation
    fair_eval = evaluate_fair(state.get("fair_output"), avg_conf)
    fair_recommendation = {"reject": "REJECT", "review": "REVIEW"}.get(fair_eval["effect"])
    escalated_by_fair = (
        fair_recommendation is not None
        and SEVERITY[fair_recommendation] > SEVERITY[recommendation]
    )
    if escalated_by_fair and fair_recommendation is not None:
        recommendation = fair_recommendation
        decision_reason = (
            "Financial exposure (FAIR): " + "; ".join(fair_eval["reasons"]) +
            f". Scores alone would have recommended {base_recommendation}."
        )
        if fair_eval["conditions"]:
            decision_reason += " Acceptable if: " + "; ".join(fair_eval["conditions"]) + "."

    # ── Collect all red flags across agents ───────────────
    all_red_flags = []
    for k in available:
        flags = outputs[k].get("red_flags", [])
        for f in flags:
            all_red_flags.append(f"[{k.upper()}] {f}")

    if fair_eval["effect"] != "none":
        all_red_flags += [f"[FAIR] {r}" for r in fair_eval["reasons"]]

    # ── Build CPO explanation ─────────────────────────────
    score_lines = "\n".join([
        f"  • {k.capitalize()}: {outputs[k]['score']:.1f}/100"
        for k in available
    ])

    explanation = f"""
VENDOR EVALUATION SUMMARY — {state.get('vendor_name', 'Unknown')}

RECOMMENDATION: {recommendation}
{decision_reason}

SCORES:
{score_lines}

  Composite Score : {composite:.1f}/100
  Risk Score      : {risk_score:.1f}/100  (lower = more risk)
  Strategic Fit   : {fit_score:.1f}/100
  Confidence      : {avg_conf:.0%}

  OFAC Screening  : {ofac_status}
  Agents Missing  : {', '.join(unavailable_agents) if unavailable_agents else 'none'}
  Agents Truncated: {', '.join(truncated_agents) if truncated_agents else 'none'}

RED FLAGS ({len(all_red_flags)} total):
{chr(10).join(f'  ⚠ {f}' for f in all_red_flags) if all_red_flags else '  None detected'}

WHAT YOU SHOULD DO:
{_get_action_items(recommendation, composite, avg_conf, all_red_flags, ofac_status, unavailable_agents, truncated_agents)}
{_fair_section(fair_eval, base_recommendation, recommendation, state.get("fair_output"))}"""

    return {
        "composite_score":     round(composite, 2),
        "risk_score":          round(risk_score, 2),
        "fit_score":           round(fit_score, 2),
        "aggregate_confidence": round(avg_conf, 4),
        "final_recommendation": recommendation,
        "decision_reason":     decision_reason,
        "dealbreaker_status":  dealbreaker_result["status"],
        "ofac_status":         ofac_status,
        "unavailable_agents":  unavailable_agents,
        "truncated_agents":    truncated_agents,
        "all_red_flags":       all_red_flags,
        "final_explanation":   explanation.strip(),
        "base_recommendation": base_recommendation,
        "escalated_by_fair":   escalated_by_fair,
        "fair_effect":         fair_eval["effect"],
        "fair_reasons":        fair_eval["reasons"],
        "conditions":          fair_eval["conditions"],
        "fair_note":           fair_eval["note"],
        "financial_exposure":  fair_eval["signals"],
        "pipeline_status":     "complete"
    }


def _get_action_items(
    recommendation: str,
    composite: float,
    confidence: float,
    red_flags: list,
    ofac_status: str = "clear",
    unavailable_agents: list | None = None,
    truncated_agents: list | None = None,
) -> str:
    actions = []

    if recommendation == "REJECT":
        return (
            "1. Do not proceed with this vendor.\n"
            "2. Document rejection reason in audit trail.\n"
            "3. Notify procurement team to source alternatives immediately."
        )

    if recommendation == "APPROVE":
        return (
            "1. Proceed with contract negotiation.\n"
            "2. Schedule quarterly performance reviews.\n"
            "3. Set up automated monitoring alerts for score changes."
        )

    # REVIEW — build specific actions based on what's actually wrong
    actions.append(
        "1. Escalate to senior procurement officer for manual review."
    )

    # Confidence-specific actions
    if confidence < 0.75:
        actions.append(
            "2. Do NOT approve until confidence exceeds 75% — "
            "request missing data from vendor."
        )
    elif confidence < 0.95:
        actions.append(
            "2. Confidence is acceptable but below auto-approval — "
            "one senior sign-off required."
        )

    # Flag-specific actions
    action_num = 3
    flag_text  = " ".join(red_flags).lower()

    if ofac_status != "clear":
        actions.append(
            f"{action_num}. Run a manual OFAC/sanctions screen and record the "
            "disposition before any further step."
        )
        action_num += 1

    if unavailable_agents:
        actions.append(
            f"{action_num}. Re-run the evaluation or manually assess the missing "
            f"areas: {', '.join(unavailable_agents)}."
        )
        action_num += 1

    if truncated_agents:
        actions.append(
            f"{action_num}. Re-run or manually check the truncated analysis "
            f"({', '.join(truncated_agents)}) — red flags may be missing."
        )
        action_num += 1

    # Word boundaries — a bare substring check matched "sec" in "security"
    if re.search(r"\b(ftc|sec)\b", flag_text):
        actions.append(
            f"{action_num}. Investigate FTC/SEC regulatory action — "
            "request legal team review before proceeding."
        )
        action_num += 1

    if "concentration" in flag_text:
        actions.append(
            f"{action_num}. Negotiate revenue concentration clause "
            "into contract to protect against single-customer dependency."
        )
        action_num += 1

    if "breach" in flag_text:
        actions.append(
            f"{action_num}. Request full incident report on data breach "
            "and confirm remediation steps taken."
        )
        action_num += 1

    if "certif" in flag_text or "missing" in flag_text:
        actions.append(
            f"{action_num}. Require vendor to provide missing "
            "certifications before contract signing."
        )
        action_num += 1

    if "operational" in flag_text and "stub" in flag_text:
        actions.append(
            f"{action_num}. Request vendor's operational SLA history "
            "and business continuity plan directly."
        )
        action_num += 1

    return "\n".join(actions)

def evaluate_fair(fair_output: dict | None, avg_conf: float) -> dict:
    """
    VendorIQ's policy on the FAIR agent's figures.

    Returns {"effect": "none" | "review" | "reject", "reasons", "signals", "note"}.
    REJECT needs a tolerance the client stated (a derived one can only cause
    REVIEW) and a typical year (P50) at or above it. A report that is partial
    or mostly assumptions is shown but never changes the decision.
    """
    none: dict = {
        "effect": "none", "reasons": [], "conditions": [], "signals": None, "note": None,
    }
    if not fair_output:
        return none
    if "skipped" in fair_output:
        return {**none, "note": f"FAIR not run: {fair_output['skipped']}."}
    try:
        s = signals_from_report(FairReport.model_validate(fair_output))
    except ValidationError:
        return {**none, "note": "FAIR report could not be read."}
    return apply_fair_rules(s, avg_conf)


def apply_fair_rules(s: FairDecisionSignals, avg_conf: float) -> dict:
    """The rules on already-computed signals (see evaluate_fair)."""
    if not s.decision_grade:
        return {
            "effect": "none",
            "reasons": [],
            "conditions": [],
            "signals": s.model_dump(),
            "note": f"FAIR is informational only: {s.not_decision_grade_reason}.",
        }

    reject, review = [], []
    tol = s.tolerance_usd
    if tol is not None:
        if (s.tolerance_source == "client" and s.p50 >= tol
                and avg_conf >= REJECT_MIN_CONFIDENCE):
            reject.append(
                f"in a typical year (P50) losses of {format_usd(s.p50)} reach the "
                f"client's stated tolerance of {format_usd(tol)}"
            )
        if s.ale_mean >= tol:
            review.append(
                f"expected annual loss {format_usd(s.ale_mean)} reaches the "
                f"{s.tolerance_source} tolerance of {format_usd(tol)}"
            )
        if (s.prob_exceeds_tolerance or 0) >= FAIR_REVIEW_PROB_EXCEEDS:
            review.append(
                f"{format_percent(s.prob_exceeds_tolerance or 0)} chance per year that "
                f"losses reach {format_usd(tol)}"
            )
        if s.uninsured_p99 and s.uninsured_p99 > tol:
            review.append(
                f"a 1-in-100-year loss leaves {format_usd(s.uninsured_p99)} not covered "
                "by the liability cap and insurance"
            )
    if s.loss_to_contract_ratio and s.loss_to_contract_ratio > FAIR_LOSS_TO_CONTRACT_MAX:
        review.append(
            f"expected annual loss {format_usd(s.ale_mean)} exceeds the annual "
            f"contract value of {format_usd(s.annual_contract_value or 0)}"
        )
    # A control that brings the exposure within tolerance turns the finding
    # into a contract condition: REVIEW with that condition, never REJECT.
    conditions = []
    best = s.best_control
    if best is not None and best.fixes_tolerance and (reject or review):
        chance = (
            f", chance of reaching tolerance {format_percent(best.prob_exceeds_after)}"
            if best.prob_exceeds_after is not None else ""
        )
        conditions.append(
            f"{best.title}: expected annual loss falls to {format_usd(best.ale_after)}"
            f"{chance} [{best.run_id}]"
        )
        review, reject = reject + review, []
    effect = "reject" if reject else "review" if review else "none"
    return {
        "effect": effect,
        "reasons": reject + review,
        "conditions": conditions,
        "signals": s.model_dump(),
        "note": None,
    }


def _fair_section(fair_eval: dict, base: str, final: str, fair_output: dict | None) -> str:
    """The FINANCIAL EXPOSURE part of the CPO explanation."""
    s = fair_eval["signals"]
    if s is None:
        return f"\nFINANCIAL EXPOSURE (FAIR):\n  {fair_eval['note']}\n" if fair_eval["note"] else ""
    tol = (
        f"{format_percent(s['prob_exceeds_tolerance'] or 0)} / year "
        f"(tolerance {format_usd(s['tolerance_usd'])}, {s['tolerance_source']})"
        if s["tolerance_usd"] is not None else "no tolerance given"
    )
    effect = (
        f"escalated {base} → {final}" if base != final
        else {"none": "none", "review": "supports REVIEW", "reject": "supports REJECT"}[fair_eval["effect"]]
    )
    lines = [
        "",
        "FINANCIAL EXPOSURE (FAIR):",
        f"  Expected annual loss : {format_usd(s['ale_mean'])}",
        f"  Chance of any loss   : {format_percent(s['prob_any_loss'])} / year",
        f"  1-in-100-year loss   : {format_usd(s['p99'])}",
        f"  Chance ≥ tolerance   : {tol}",
        f"  Main scenario        : {s['dominant_scenario']}",
        f"  Assumed inputs       : {s['assumption_share']:.0%}",
        f"  Effect on decision   : {effect}",
    ]
    if fair_eval["note"]:
        lines.append(f"  Note                 : {fair_eval['note']}")
    if fair_eval["conditions"]:
        lines += ["  Acceptable if:"] + [f"    - {c}" for c in fair_eval["conditions"]]
    actions = []
    if s["tolerance_source"] == "derived":
        actions.append("State a dollar risk tolerance; FAIR used one derived from risk appetite.")
    if s["uninsured_p99"]:
        actions.append(
            f"Negotiate a liability cap or insurance covering the 1-in-100-year loss "
            f"({format_usd(s['p99'])})."
        )
    actions.extend(((fair_output or {}).get("recommendations") or [])[:2])
    if actions:
        lines += ["  Actions:"] + [f"    - {a}" for a in actions]
    return "\n".join(lines) + "\n"
