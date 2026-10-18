import json
import re
from abc import ABC, abstractmethod
from langchain_core.messages import SystemMessage, HumanMessage
from app.config_watsonx import get_llm, PRIMARY_MODEL
from app.orchestrator.state import VendorRiskState


class BaseAgent(ABC):

    def __init__(self, model_id: str = PRIMARY_MODEL):
        self.llm = get_llm(model_id=model_id, temperature=0.0)

    @property
    @abstractmethod
    def system_prompt(self) -> str:
        pass

    @property
    @abstractmethod
    def domain_data_key(self) -> str:
        pass

    async def run(self, state: VendorRiskState) -> dict:
        domain_data = state.get(self.domain_data_key, {})
        vendor_name = state.get("vendor_name", "Unknown Vendor")

        # Step 1 — Chain of thought reasoning pass
        print(f"\n  → Step 1: Reasoning through {vendor_name}'s data...")
        reasoning = await self._get_reasoning(vendor_name, domain_data)

        print("\n── Chain-of-Thought Reasoning ──────────────────────────")
        print(reasoning)
        print("────────────────────────────────────────────────────────\n")

        # Step 2 — Scoring pass informed by the reasoning
        print(f"  → Step 2: Producing final score based on reasoning...")
        user_prompt = self._build_user_prompt(vendor_name, domain_data, reasoning)

        messages = [
            SystemMessage(content=self.system_prompt),
            HumanMessage(content=user_prompt)
        ]

        try:
            response = await self.llm.ainvoke(messages)
            result = self._parse_json_response(response.content)
            result["reasoning"] = reasoning
            return result

        except Exception as e:
            return {
                "score": 0.0,
                "explanation": f"Agent execution failed: {str(e)}",
                "evidence": [],
                "confidence": 0.0,
                "red_flags": [f"Agent error: {str(e)}"],
                "reasoning": ""
            }

    async def _get_reasoning(self, vendor_name: str, domain_data: dict) -> str:
        # Extract sources if present
        sources = domain_data.get("_sources", {})
        sources_text = ""
        if sources:
            sources_text = "\n\nDATA SOURCES (cite these in your reasoning):\n"
            sources_text += "\n".join(
                f"  - {field}: {source}"
                for field, source in sources.items()
                if not field.startswith("_")
            )

        # Also include certification sources if present
        cert_sources = domain_data.get("certifications_sources", {})
        if cert_sources:
            sources_text += "\n\nCERTIFICATION SOURCES:\n"
            sources_text += "\n".join(
                f"  - {cert}: verified at {url}"
                for cert, url in cert_sources.items()
            )

        reasoning_prompt = f"""
    You are a senior analyst evaluating a vendor for enterprise procurement.

    Vendor: {vendor_name}
    Domain: {self.domain_data_key.replace('_data', '').upper()} ANALYSIS

    Data provided:
    {json.dumps(
        {k: v for k, v in domain_data.items()
        if not k.startswith('_') and k != 'certifications_sources'},
        indent=2
    )}
    {sources_text}

    Analyze this vendor strictly from your domain perspective.
    In 6-8 sentences cover the most important signals in your data.

    IMPORTANT: For every data point you mention, cite its source in brackets.
    Example: "The current ratio of 1.4 [Yahoo Finance / yfinance] indicates..."
    Example: "ISO27001 certification [verified at trust.microsoft.com] confirms..."

    Be direct. Cite actual numbers with their sources.
    End with a one-line verdict for the CPO.
    """
        messages = [HumanMessage(content=reasoning_prompt)]
        response = await self.llm.ainvoke(messages)
        return response.content
    
    def _build_user_prompt(
        self,
        vendor_name: str,
        domain_data: dict,
        reasoning: str
    ) -> str:
        sources = domain_data.get("_sources", {})
        cert_sources = domain_data.get("certifications_sources", {})

        sources_text = ""
        if sources or cert_sources:
            sources_text = "\nData sources for evidence citations:\n"
            for field, source in sources.items():
                if not field.startswith("_"):
                    sources_text += f"  - {field}: {source}\n"
            for cert, url in cert_sources.items():
                sources_text += f"  - {cert}: {url}\n"

        return f"""
    You have already reasoned through this vendor's data.
    Now produce the final score.

    Vendor: {vendor_name}

    Your prior reasoning (with sources):
    {reasoning}

    Raw data:
    {json.dumps(
        {k: v for k, v in domain_data.items()
        if not k.startswith('_') and k != 'certifications_sources'},
        indent=2
    )}
    {sources_text}

    In your evidence array, cite the source for each data point.
    Example: "current_ratio: 1.4 [Source: Yahoo Finance / yfinance]"

    Return ONLY the JSON scoring object. No text outside the JSON.
    """

    def _parse_json_response(self, raw: str) -> dict:
        cleaned = re.sub(r"```(?:json)?", "", raw).strip().strip("`").strip()
        start = cleaned.find("{")
        end   = cleaned.rfind("}") + 1
        if start != -1 and end > start:
            cleaned = cleaned[start:end]
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return {
                "score": 0.0,
                "explanation": f"Failed to parse output: {raw[:200]}",
                "evidence": [],
                "confidence": 0.0,
                "red_flags": ["JSON parse error — check agent output"]
            }