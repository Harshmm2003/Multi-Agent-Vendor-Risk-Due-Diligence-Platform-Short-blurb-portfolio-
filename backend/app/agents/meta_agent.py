from app.orchestrator.state import VendorRiskState


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

    if missing_weight > 0 and available:
        total_avail = sum(weights[k] for k in available)
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

    # ── OFAC override ─────────────────────────────────────
    ofac_flagged = False
    if outputs.get("compliance"):
        ofac_flagged = not outputs["compliance"].get("sanctions_clear", True)

    # ── Recommendation logic ──────────────────────────────
    if ofac_flagged:
        recommendation = "REJECT"
        decision_reason = (
            "OFAC sanctions match detected. "
            "Vendor must be rejected immediately — no exceptions."
        )
    elif avg_conf < 0.95:
        recommendation = "REVIEW"
        decision_reason = (
            f"Confidence ({avg_conf:.0%}) is below the 95% auto-approval "
            f"threshold. Route to human reviewer before proceeding."
        )
    elif composite >= 70:
        recommendation = "APPROVE"
        decision_reason = (
            f"Composite score of {composite:.1f} with {avg_conf:.0%} "
            f"confidence meets approval criteria."
        )
    elif composite >= 45:
        recommendation = "REVIEW"
        decision_reason = (
            f"Composite score of {composite:.1f} falls in the review band. "
            f"Proceed with caution — additional due diligence recommended."
        )
    else:
        recommendation = "REJECT"
        decision_reason = (
            f"Composite score of {composite:.1f} is below the minimum "
            f"acceptable threshold. Do not proceed with this vendor."
        )

    # ── Collect all red flags across agents ───────────────
    all_red_flags = []
    for k in available:
        flags = outputs[k].get("red_flags", [])
        for f in flags:
            all_red_flags.append(f"[{k.upper()}] {f}")

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

RED FLAGS ({len(all_red_flags)} total):
{chr(10).join(f'  ⚠ {f}' for f in all_red_flags) if all_red_flags else '  None detected'}

WHAT YOU SHOULD DO:
{_get_action_items(recommendation, composite, avg_conf, all_red_flags)}
"""

    return {
        "composite_score":     round(composite, 2),
        "risk_score":          round(risk_score, 2),
        "fit_score":           round(fit_score, 2),
        "aggregate_confidence": round(avg_conf, 4),
        "final_recommendation": recommendation,
        "decision_reason":     decision_reason,
        "all_red_flags":       all_red_flags,
        "final_explanation":   explanation.strip(),
        "pipeline_status":     "complete"
    }


def _get_action_items(
    recommendation: str,
    composite: float,
    confidence: float,
    red_flags: list
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

    if "ftc" in flag_text or "sec" in flag_text:
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