from app.agents.base_agent import BaseAgent
from app.orchestrator.state import VendorRiskState

STRATEGIC_SYSTEM_PROMPT = """
You are the Strategic Fit Agent for an enterprise vendor risk platform.

Your job: evaluate how well this vendor matches the CLIENT's specific needs,
priorities, and requirements. This is NOT a general vendor assessment —
you must score fit against what THIS client needs.

SCORING CRITERIA (0-100):
- Alignment with client's stated priorities              → 35 pts
  (Score each priority match: full=7pts, partial=4pts, none=0pts)
- Vendor capability vs contract requirements             → 25 pts
  (Can they deliver the scope at the contract value/duration?)
- Dealbreaker check                                      → 20 pts
  (Any dealbreaker present = automatic -20 pts each)
- Nice-to-have alignment                                 → 10 pts
- Long-term partnership potential                        → 10 pts

RULES:
1. You MUST reference the client's specific priorities by name
2. If a dealbreaker is present, flag it explicitly
3. Score each priority match individually then aggregate
4. A vendor can be financially strong but a poor strategic fit — be honest
5. Return ONLY valid JSON

OUTPUT FORMAT:
{
  "score": <float 0-100>,
  "explanation": "<paragraph citing specific priority matches>",
  "evidence": ["<priority 1: match/partial/none>", "<priority 2: ...>"],
  "confidence": <float 0.0-1.0>,
  "red_flags": ["<flag>"],
  "priority_matches": {
    "<priority name>": "<FULL | PARTIAL | NONE>"
  },
  "dealbreakers_triggered": ["<dealbreaker>"],
  "recommendation_note": "<one line for CPO on strategic fit>"
}
"""


class StrategicAgent(BaseAgent):

    @property
    def system_prompt(self) -> str:
        return STRATEGIC_SYSTEM_PROMPT

    @property
    def domain_data_key(self) -> str:
        return "strategic_data"

    def _build_user_prompt(
        self,
        vendor_name: str,
        domain_data: dict,
        reasoning: str
    ) -> str:
        client = domain_data.get("client_profile", {})
        priorities  = client.get("priorities", [])
        dealbreakers = client.get("dealbreakers", [])
        nice_to_have = client.get("nice_to_have", [])

        return f"""
You have already reasoned through this vendor's strategic fit.
Now produce the final strategic fit score.

Vendor: {vendor_name}
Vendor Industry: {domain_data.get('industry', 'Unknown')}
Years in Operation: {domain_data.get('years_in_operation', 'Unknown')}
Certifications: {domain_data.get('certifications', [])}

CLIENT: {client.get('company_name', 'Unknown')}
Client Industry: {client.get('industry', 'Unknown')}
Contract Value: ${client.get('contract_value', 0):,} over {client.get('contract_years', 1)} years
Risk Appetite: {client.get('risk_appetite', 'medium')}

CLIENT PRIORITIES (must match each explicitly):
{chr(10).join(f'- {p}' for p in priorities)}

DEALBREAKERS (automatic deduction if present):
{chr(10).join(f'- {d}' for d in dealbreakers)}

NICE TO HAVE:
{chr(10).join(f'- {n}' for n in nice_to_have)}

Your prior reasoning:
{reasoning}

Full vendor data:
{domain_data}

Score each priority as FULL, PARTIAL, or NONE.
Check every dealbreaker explicitly.
Return ONLY the JSON scoring object.
"""


async def run_strategic_agent(state: VendorRiskState) -> dict:
    agent = StrategicAgent()
    return await agent.run(state)