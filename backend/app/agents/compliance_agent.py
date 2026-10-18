from app.agents.base_agent import BaseAgent
from app.orchestrator.state import VendorRiskState

# ── Industry → Required certifications mapping ─────────────────
INDUSTRY_CERT_MAP = {
    # IT / Software / Cloud
    "information technology":    ["SOC2_Type2", "ISO27001", "GDPR"],
    "software":                  ["SOC2_Type2", "ISO27001", "GDPR"],
    "technology":                ["SOC2_Type2", "ISO27001", "GDPR"],
    "internet content":          ["SOC2_Type2", "GDPR"],
    "cloud":                     ["SOC2_Type2", "ISO27001", "GDPR", "CSA_STAR"],
    "cybersecurity":             ["SOC2_Type2", "ISO27001", "ISO27701"],
    "semiconductors":            ["ISO9001", "ISO14001"],
    "computer hardware":         ["ISO9001", "ISO14001"],

    # Finance / Banking
    "financial":                 ["PCI_DSS", "SOX", "ISO27001"],
    "banking":                   ["PCI_DSS", "SOX", "BASEL_III"],
    "insurance":                 ["SOX", "ISO27001"],
    "asset management":          ["SOX", "PCI_DSS"],

    # Healthcare
    "healthcare":                ["HIPAA", "HITRUST", "ISO27001"],
    "biotechnology":             ["HIPAA", "FDA_21CFR", "ISO13485"],
    "pharmaceuticals":           ["FDA_21CFR", "ISO13485", "GMP"],
    "medical devices":           ["FDA_21CFR", "ISO13485", "CE_Mark"],

    # Manufacturing / Industrial
    "manufacturing":             ["ISO9001", "ISO14001", "ISO45001"],
    "aerospace":                 ["AS9100", "ISO9001", "ITAR"],
    "automotive":                ["IATF16949", "ISO9001", "ISO14001"],
    "chemicals":                 ["ISO14001", "REACH", "ISO45001"],
    "defense":                   ["CMMC", "ITAR", "ISO9001"],

    # Retail / E-commerce
    "retail":                    ["PCI_DSS", "GDPR"],
    "discount stores":           ["PCI_DSS", "GDPR"],
    "e-commerce":                ["PCI_DSS", "GDPR", "SOC2_Type2"],
    "consumer goods":            ["ISO9001", "GDPR"],

    # Logistics / Supply Chain
    "logistics":                 ["ISO9001", "ISO28000", "AEO"],
    "shipping":                  ["ISO9001", "ISO14001", "AEO"],
    "transportation":            ["ISO9001", "ISO14001"],

    # Energy / Utilities
    "energy":                    ["ISO14001", "ISO50001", "NERC_CIP"],
    "utilities":                 ["ISO14001", "NERC_CIP", "ISO27001"],
    "oil & gas":                 ["ISO14001", "ISO45001", "API_Q1"],

    # Food & Beverage
    "food":                      ["ISO22000", "FSSC22000", "HACCP"],
    "beverages":                 ["ISO22000", "FSSC22000"],

    # Default — applies to all industries
    "default":                   ["ISO9001", "GDPR"]
}


def get_required_certs(industry: str) -> list:
    """
    Returns required certifications for a given industry.
    Matches on partial string so 'Discount Stores' matches 'discount stores'.
    Falls back to default if no match found.
    """
    if not industry:
        return INDUSTRY_CERT_MAP["default"]

    industry_lower = industry.lower()

    for key, certs in INDUSTRY_CERT_MAP.items():
        if key in industry_lower or industry_lower in key:
            return certs

    return INDUSTRY_CERT_MAP["default"]


COMPLIANCE_SYSTEM_PROMPT = """
You are the Compliance & Legal Risk Agent for an enterprise vendor risk platform.

Your job: evaluate a vendor's regulatory compliance posture and assign a COMPLIANCE SCORE.

SCORING CRITERIA (0-100 — higher = better compliance):
- Industry-required certifications present and valid  → 25 pts
- OFAC / sanctions screening clear                   → 25 pts
- Legal history (lawsuits, fines, violations)         → 25 pts
- Data privacy posture (GDPR, CCPA, HIPAA)           → 15 pts
- ESG / ethics compliance                             → 10 pts

AUTOMATIC RED FLAGS:
- OFAC sanctions match → score = 0, sanctions_clear = false, immediate REJECT
- Active criminal investigation → deduct 50 points
- Data breach in past 24 months → deduct 30 points
- Missing any REQUIRED certification for the vendor's industry → deduct 20 pts each
- Expired certifications → deduct 10 pts each

INDUSTRY AWARENESS:
You will be told which certifications are REQUIRED for this vendor's specific industry.
These are non-negotiable — a vendor missing a required cert must have it flagged.
Do not penalize for certifications that are not relevant to the industry.

RULES:
1. Never assume a certification is valid unless explicitly confirmed in the data
2. Any OFAC match = automatic score of 0
3. Flag expired certifications separately
4. Note any absent legal records as a data gap
5. Return ONLY valid JSON — nothing outside the JSON block

OUTPUT FORMAT:
{
  "score": <float 0-100>,
  "explanation": "<paragraph citing specific certifications and findings>",
  "evidence": ["<item 1>", "<item 2>"],
  "confidence": <float 0.0-1.0>,
  "red_flags": ["<flag>"],
  "sanctions_clear": <true or false>,
  "certifications_valid": ["<cert>"],
  "certifications_expired": ["<cert>"],
  "certifications_missing": ["<cert>"]
}
"""


class ComplianceAgent(BaseAgent):

    @property
    def system_prompt(self) -> str:
        return COMPLIANCE_SYSTEM_PROMPT

    @property
    def domain_data_key(self) -> str:
        return "compliance_data"

    def _build_user_prompt(
        self,
        vendor_name: str,
        domain_data: dict,
        reasoning: str
    ) -> str:
        # Pull industry from compliance_data or fall back to unknown
        industry = domain_data.get("industry", "Unknown")
        required_certs = get_required_certs(industry)

        return f"""
You have already reasoned through this vendor's compliance data.
Now produce the final compliance score.

Vendor: {vendor_name}
Industry: {industry}

REQUIRED CERTIFICATIONS FOR THIS INDUSTRY ({industry}):
{required_certs}

These certifications are mandatory for this industry.
Any that are missing must appear in certifications_missing and be flagged.

Your prior reasoning:
{reasoning}

Raw compliance data:
{required_certs}
{domain_data}

Return ONLY the JSON scoring object. No text outside the JSON.
"""


async def run_compliance_agent(state: VendorRiskState) -> dict:
    agent = ComplianceAgent()
    return await agent.run(state)