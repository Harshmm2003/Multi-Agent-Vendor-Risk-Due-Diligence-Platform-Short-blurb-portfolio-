"""The search_sec_filings tool, driven by a scripted agent."""

from test_fair_agent import agent, breach_only, call, load_example

from fair.agent import Budget
from fair.ports import EvidenceDraft, StaticEvidenceProvider


async def run_with(provider, script):
    ctx, _ = load_example()
    a, _ = agent(script, budget=Budget(max_llm_calls=len(script)))
    return await a.run(breach_only(ctx), provider)


async def test_search_tool_evidence_is_citable():
    provider = StaticEvidenceProvider(
        filings={
            "cyber_incident": [
                EvidenceDraft(
                    kind="filing", title="SEC 8-K filed 2024-01-19", citation="EDGAR"
                )
            ]
        }
    )
    script = [call("search_sec_filings", topics=["cyber_incident", "going_concern"])]
    report = await run_with(provider, script)
    step = next(t for t in report.trace if t.tool == "search_sec_filings")
    assert step.summary == "cyber_incident: 1 filings, going_concern: 0 filings"
    assert report.evidence[0].id == "ev_1" and report.evidence[0].kind == "filing"


async def test_search_tool_for_non_filer():
    script = [call("search_sec_filings", topics=["cyber_incident"])]
    report = await run_with(StaticEvidenceProvider(), script)
    step = next(t for t in report.trace if t.tool == "search_sec_filings")
    assert "not a registrant" in step.summary


async def test_unknown_topic_is_refused():
    script = [call("search_sec_filings", topics=["insider_trading"])]
    report = await run_with(StaticEvidenceProvider(), script)
    step = next(t for t in report.trace if t.tool == "search_sec_filings")
    assert step.kind == "tool_error" and "topics" in step.summary
