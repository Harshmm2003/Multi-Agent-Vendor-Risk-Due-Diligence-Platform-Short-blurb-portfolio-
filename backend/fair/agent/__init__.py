"""The agentic layer: a tool-calling FAIR analyst with guardrails and a fallback."""

from fair.agent.guardrails import Budget
from fair.agent.loop import FairAgent, fallback_report
from fair.agent.report import FairReport, render_text
from fair.agent.session import Session

__all__ = [
    "Budget",
    "FairAgent",
    "FairReport",
    "Session",
    "fallback_report",
    "render_text",
]
