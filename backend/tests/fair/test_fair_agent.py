"""
The FAIR agent loop, driven by a scripted fake model: no network, no Groq.

Each script entry is the model's next reply: an AIMessage, a function that
builds one from the conversation so far (to copy a run id or figure from a
tool result, as a real model would), or an exception to raise.
"""

import json
import re
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from fair.agent import Budget, FairAgent
from fair.agent.session import Session
from fair.agent.tools import build_tools, inline_refs
from fair.context import AnalysisContext
from fair.ports import StaticEvidenceProvider

EXAMPLE = (
    Path(__file__).resolve().parents[2]
    / "fair"
    / "examples"
    / "analyze_healthcare_vendor.json"
)


def load_example():
    data = json.loads(EXAMPLE.read_text())
    return (
        AnalysisContext.model_validate(data["context"]),
        StaticEvidenceProvider.from_dict(data["evidence"]),
    )


def breach_only(ctx: AnalysisContext) -> AnalysisContext:
    return AnalysisContext.model_validate(
        {**ctx.model_dump(), "candidate_scenarios": ["vendor_breach"]}
    )


class ScriptedLLM:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.bound_tools: list[dict] = []

    def bind_tools(self, tools):
        self.bound_tools = tools
        return self

    async def ainvoke(self, messages):
        self.calls += 1
        if not self.script:
            return AIMessage(content="I have nothing more to do.")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item(messages) if callable(item) else item


_ids = iter(range(1, 10_000))


def call(name, **args):
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": args, "id": f"c{next(_ids)}"}]
    )


def last_result(messages, tool_call_index=-1):
    """The JSON payload of the most recent tool result."""
    results = [m for m in messages if isinstance(m, ToolMessage)]
    return json.loads(results[tool_call_index].content)


def results_named(messages, key):
    return [
        json.loads(m.content)
        for m in messages
        if isinstance(m, ToolMessage) and key in m.content
    ]


def baseline_of(messages, back=-1):
    """The first baseline's inputs in a get_baseline_inputs result."""
    return last_result(messages, back)["baselines"][0]["inputs"]


def run(*inputs):
    return call("run_simulation", runs=list(inputs))


def simulate_baseline(messages):
    """Run every baseline the previous tool call returned."""
    return run(*[b["inputs"] for b in last_result(messages)["baselines"]])


def submit_with(figure_key="p90", extra_text=""):
    def reply(messages):
        runs = [
            r for res in results_named(messages, '"runs"') for r in res.get("runs", [])
        ]
        latest = {}
        for r in runs:
            latest[r["scenario"]] = r
        breach = latest.get("vendor_breach")
        narrative = "Breach risk dominates."
        if breach:
            narrative += f" A bad year (P90) costs {breach[figure_key]}."
        narrative += extra_text
        return call(
            "submit_report",
            final_run_ids=[r["run_id"] for r in latest.values()],
            narrative=narrative,
            key_findings=["SOC 2 Type 2 verified"],
            recommendations=["Limit records shared"],
        )

    return reply


async def no_sleep(_seconds):
    return None


def agent(script, **kw):
    llm = ScriptedLLM(script)
    return FairAgent(llm, iterations=2_000, sleep=no_sleep, **kw), llm


HAPPY_PATH = [
    call("get_agent_findings", domains=["compliance"]),
    call("get_agent_findings", domains=["reputation"]),
    call("get_baseline_inputs", scenarios=["vendor_breach"]),
    simulate_baseline,
    call("get_baseline_inputs", scenarios=["vendor_insolvency"]),
    simulate_baseline,
    submit_with(),
]


# ── The happy path ────────────────────────────────────────────────────────


async def test_agent_completes_and_report_comes_from_ledger():
    ctx, provider = load_example()
    a, llm = agent(HAPPY_PATH)
    report = await a.run(ctx, provider)

    assert report.status == "complete" and report.generated_by == "agent"
    assert [s.run_id for s in report.scenarios] == ["run_01", "run_02"]
    assert {s.scenario for s in report.scenarios} == {
        "vendor_breach",
        "vendor_insolvency",
    }
    assert report.combined.ale_mean > 0
    assert report.evidence[0].id == "ev_1" and report.evidence[0].kind == "finding"
    assert report.key_findings == ["SOC 2 Type 2 verified"]
    assert report.llm_calls == llm.calls == 7
    kinds = [t.kind for t in report.trace]
    assert kinds.count("tool_call") == 7 and kinds[-1] == "guardrail"
    assert "submission accepted" in report.trace[-1].summary


async def test_agent_can_cite_evidence_it_was_given():
    ctx, provider = load_example()

    def refine(messages):
        inputs = baseline_of(messages)
        inputs["threat_event_frequency"] = {
            "min": 0.01,
            "mode": 0.05,
            "max": 0.15,
            "source": "ev_1",  # SOC 2 Type 2 finding, fetched earlier
            "rationale": "Verified SOC 2 Type 2: fewer loss events than the average firm.",
        }
        return run(inputs)

    script = [
        call("get_agent_findings", domains=["compliance"]),
        call("get_baseline_inputs", scenarios=["vendor_breach"]),
        refine,
        submit_with(),
    ]
    ctx = breach_only(ctx)
    a, _ = agent(script)
    report = await a.run(ctx, provider)
    assert report.status == "complete"
    tef = next(
        i for i in report.scenarios[0].inputs if i.path == "threat_event_frequency"
    )
    assert tef.source == "ev_1"


def test_tool_schemas_are_self_contained():
    ctx, provider = load_example()
    tools = build_tools(Session(ctx, provider, 100))
    for tool in tools.values():
        assert "$ref" not in json.dumps(tool.schema()), tool.name
    assert "ask_user" not in tools  # only offered when a person can answer


def test_inline_refs_resolves_nested_definitions():
    schema = {
        "properties": {"a": {"$ref": "#/$defs/A"}},
        "$defs": {
            "A": {"properties": {"b": {"$ref": "#/$defs/B"}}},
            "B": {"type": "number"},
        },
    }
    assert inline_refs(schema) == {
        "properties": {"a": {"properties": {"b": {"type": "number"}}}}
    }


# ── Self-correction: errors go back to the model ──────────────────────────


async def test_invalid_arguments_are_returned_and_fixed():
    ctx, provider = load_example()

    def bad_then(messages):
        inputs = baseline_of(messages)
        inputs["vulnerability"] = {
            "min": 0.5,
            "mode": 1.2,
            "max": 1.5,
            "source": "assumption",
        }
        return run(inputs)

    def fixed(messages):
        error = last_result(messages)
        assert (
            "invalid arguments" in error["error"] and "vulnerability" in error["error"]
        )
        inputs = baseline_of(messages, -2)  # the baseline, two results back
        return run(inputs)

    script = [
        call("get_baseline_inputs", scenarios=["vendor_breach"]),
        bad_then,
        fixed,
        submit_with(),
    ]
    ctx = breach_only(ctx)
    a, _ = agent(script)
    report = await a.run(ctx, provider)
    assert report.status == "complete"
    assert any(t.kind == "tool_error" for t in report.trace)


async def test_citing_unknown_evidence_is_refused():
    ctx, provider = load_example()

    def cite_fake(messages):
        inputs = baseline_of(messages)
        inputs["threat_event_frequency"]["source"] = "ev_99"
        return run(inputs)

    def cite_fake_benchmark(messages):
        assert "ev_99 does not exist" in last_result(messages)["error"]
        inputs = baseline_of(messages, -2)
        inputs["threat_event_frequency"]["source"] = "benchmark:made_up"
        return run(inputs)

    script = [
        call("get_baseline_inputs", scenarios=["vendor_breach"]),
        cite_fake,
        cite_fake_benchmark,
    ]
    ctx = breach_only(ctx)
    a, _ = agent(script, budget=Budget(max_llm_calls=3))
    report = await a.run(ctx, provider)
    errors = [t.summary for t in report.trace if t.kind == "tool_error"]
    assert any("ev_99" in e for e in errors)
    assert any("not a known benchmark" in e for e in errors)
    # Neither fake run reached the ledger; the fallback ran the baseline instead
    assert report.generated_by == "fallback"
    assert report.scenarios[0].run_id == "run_01"


async def test_unknown_tool_is_reported_not_crashed():
    ctx, provider = load_example()
    a, _ = agent([call("delete_everything"), *HAPPY_PATH])
    report = await a.run(ctx, provider)
    assert report.status == "complete"
    assert "unknown tool" in report.trace[0].summary


# ── Guardrail: no invented dollar figures ─────────────────────────────────


async def test_invented_figure_rejected_then_fixed():
    ctx, provider = load_example()
    script = [
        *HAPPY_PATH[:-1],
        submit_with(extra_text=" Fines could reach $3.7M."),
        submit_with(),
    ]
    a, _ = agent(script)
    report = await a.run(ctx, provider)
    assert report.status == "complete"
    rejection = next(t for t in report.trace if "rejected" in t.summary)
    assert "$3.7M" in rejection.summary


async def test_repeated_invented_figures_fall_back():
    ctx, provider = load_example()
    lie = submit_with(extra_text=" Expect $123K.")
    a, _ = agent([*HAPPY_PATH[:-1], lie, lie])
    report = await a.run(ctx, provider)
    assert report.status == "partial" and report.generated_by == "fallback"
    # The agent's own runs are kept; they passed the same checks as any run
    assert [s.run_id for s in report.scenarios] == ["run_01", "run_02"]
    assert "rejected 2 times" in report.notes[0]


async def test_figures_from_tools_and_benchmarks_are_allowed():
    ctx, provider = load_example()
    script = [
        *HAPPY_PATH[:-1],
        submit_with(
            extra_text=" The healthcare average breach costs $7.42M; contract $400K."
        ),
    ]
    a, _ = agent(script)
    assert (await a.run(ctx, provider)).status == "complete"


async def test_two_final_runs_for_one_scenario_rejected():
    ctx, provider = load_example()

    def double_submit(messages):
        return call(
            "submit_report", final_run_ids=["run_01", "run_02"], narrative="Done."
        )

    script = [
        call("get_baseline_inputs", scenarios=["vendor_breach"]),
        simulate_baseline,
        call("get_baseline_inputs", scenarios=["vendor_breach"]),
        simulate_baseline,
        double_submit,
    ]
    ctx = breach_only(ctx)
    a, _ = agent(script, budget=Budget(max_llm_calls=5))
    report = await a.run(ctx, provider)
    assert any("counted twice" in t.summary for t in report.trace)


# ── Budget and failures: always a report ──────────────────────────────────


async def test_budget_exhausted_falls_back_to_baseline():
    ctx, provider = load_example()
    a, llm = agent([call("get_financials")] * 50, budget=Budget(max_llm_calls=4))
    report = await a.run(ctx, provider)
    assert llm.calls == 4
    assert report.status == "partial" and report.generated_by == "fallback"
    assert {s.scenario for s in report.scenarios} == {
        "vendor_breach",
        "vendor_insolvency",
    }
    assert "used all 4 model calls" in report.notes[0]


async def test_time_budget():
    ctx, provider = load_example()
    ticks = iter([0.0, 0.0, 500.0, 500.0, 500.0])
    a, _ = agent([call("get_financials")] * 5, clock=lambda: next(ticks))
    report = await a.run(ctx, provider)
    assert report.generated_by == "fallback"
    assert "ran out of time" in report.notes[0]


async def test_rate_limit_waits_do_not_count_against_the_time_budget():
    # Live run, Veeva: the pipeline's other agents had used the minute's
    # tokens, so the FAIR agent waited 300s on pacing and nearly fell back.
    ctx, provider = load_example()
    now = [0.0]

    async def wait(seconds):
        now[0] += seconds

    a, _ = agent(
        HAPPY_PATH, clock=lambda: now[0], tokens_per_minute=1_000, budget=Budget()
    )
    a._sleep = wait
    report = await a.run(ctx, provider)
    assert report.paced_seconds > Budget().max_seconds
    assert report.generated_by == "agent"


async def test_plain_text_submission_is_read_as_the_tool_call():
    # Live run, CVS/Veeva: the model wrote its submission as JSON text.
    def as_text(messages):
        args = submit_with()(messages).tool_calls[0]["args"]
        return AIMessage(
            content="Here is the report:\n```json\n" + json.dumps(args) + "\n```"
        )

    ctx, provider = load_example()
    a, llm = agent([*HAPPY_PATH[:-1], as_text])
    report = await a.run(ctx, provider)
    assert report.generated_by == "agent" and report.status == "complete"
    assert llm.calls == len(HAPPY_PATH)  # no extra call to recover
    assert any("plain-text submission" in t.summary for t in report.trace)


async def test_plain_text_that_is_not_a_submission_is_still_nudged():
    ctx, provider = load_example()
    a, llm = agent(
        [*HAPPY_PATH[:-1], AIMessage(content='{"note": "thinking"}'), submit_with()]
    )
    report = await a.run(ctx, provider)
    assert report.generated_by == "agent" and llm.calls == len(HAPPY_PATH) + 1


async def test_model_that_stops_calling_tools_falls_back():
    ctx, provider = load_example()
    a, _ = agent(
        [AIMessage(content="Thinking..."), AIMessage(content="Still thinking.")]
    )
    report = await a.run(ctx, provider)
    assert report.generated_by == "fallback"
    assert "stopped calling tools" in report.notes[0]


class RateLimitError(Exception):
    status_code = 429


async def test_rate_limit_is_retried():
    ctx, provider = load_example()
    a, _ = agent([RateLimitError("slow down"), *HAPPY_PATH])
    report = await a.run(ctx, provider)
    assert report.status == "complete"
    assert any("rate limited" in t.summary for t in report.trace)


async def test_api_failure_falls_back():
    ctx, provider = load_example()
    a, _ = agent([ConnectionError("network down")])
    report = await a.run(ctx, provider)
    assert report.generated_by == "fallback"
    assert "model call failed: ConnectionError" in report.notes[0]


async def test_fallback_keeps_partial_agent_work():
    ctx, provider = load_example()
    script = [
        call("get_baseline_inputs", scenarios=["vendor_breach"]),
        simulate_baseline,
    ]
    a, _ = agent(script, budget=Budget(max_llm_calls=2))
    report = await a.run(ctx, provider)
    notes = " ".join(report.notes)
    assert "vendor_breach: the agent's last run (run_01)" in notes
    assert "vendor_insolvency: benchmark baseline used" in notes


# ── ask_user (interactive hosts only) ─────────────────────────────────────


async def test_ask_user_answer_becomes_citable_evidence():
    ctx, provider = load_example()
    asked = []

    async def ask(question):
        asked.append(question)
        return "About 50,000 records"

    script = [
        call("ask_user", question="How many records will the vendor hold?"),
        *HAPPY_PATH,
    ]
    a, llm = agent(script)
    report = await a.run(ctx, provider, ask_user=ask)
    assert asked == ["How many records will the vendor hold?"]
    assert any(t["function"]["name"] == "ask_user" for t in llm.bound_tools)
    client_ev = [e for e in report.evidence if e.kind == "client"]
    assert client_ev and client_ev[0].detail == "About 50,000 records"


# ── Determinism ───────────────────────────────────────────────────────────


async def test_same_script_same_numbers():
    ctx, provider = load_example()
    a1, _ = agent(HAPPY_PATH)
    a2, _ = agent(HAPPY_PATH)
    r1, r2 = await a1.run(ctx, provider), await a2.run(ctx, provider)
    assert r1.combined == r2.combined


@pytest.mark.parametrize("missing", [[], ["risk_tolerance_usd"]])
def test_missing_inputs_listed(missing):
    ctx, _ = load_example()
    if missing:
        assert "risk_tolerance_usd" in ctx.missing_inputs()
    else:
        full = ctx.model_copy(
            update={
                "exposure": ctx.exposure.model_copy(
                    update={
                        "risk_tolerance_usd": 1,
                        "revenue_at_risk_per_hour": 1,
                        "liability_cap_usd": 1,
                        "cyber_insurance_usd": 1,
                    }
                )
            }
        )
        assert full.missing_inputs() == []


# ── Lessons from the first live run ───────────────────────────────────────


async def test_double_counting_loss_event_benchmark_is_refused():
    ctx, provider = load_example()

    def lower_both(messages):
        inputs = baseline_of(messages)
        inputs["threat_event_frequency"]["mode"] = 0.07
        inputs["vulnerability"] = {
            "min": 0.2,
            "mode": 0.4,
            "max": 0.6,
            "source": "ev_1",
        }
        return run(inputs)

    script = [
        call("get_agent_findings", domains=["compliance"]),
        call("get_baseline_inputs", scenarios=["vendor_breach"]),
        lower_both,
    ]
    a, _ = agent(script, budget=Budget(max_llm_calls=3))
    report = await a.run(breach_only(ctx), provider)
    refusal = next(t.summary for t in report.trace if t.kind == "tool_error")
    assert (
        "counts loss events" in refusal and "vulnerability must stay fixed" in refusal
    )


async def test_parallel_tool_calls_in_one_turn():
    ctx, provider = load_example()

    def batch(*calls):
        return AIMessage(
            content="",
            tool_calls=[
                {"name": n, "args": a, "id": f"c{next(_ids)}"} for n, a in calls
            ],
        )

    def simulate_both(messages):
        baselines = [
            b["inputs"]
            for res in results_named(messages, '"baselines"')
            for b in res["baselines"]
        ]
        return batch(*[("run_simulation", {"runs": [b]}) for b in baselines])

    script = [
        batch(
            ("get_baseline_inputs", {"scenarios": ["vendor_breach"]}),
            ("get_baseline_inputs", {"scenarios": ["vendor_insolvency"]}),
            ("get_agent_findings", {"domains": ["compliance"]}),
        ),
        simulate_both,
        submit_with(),
    ]
    a, llm = agent(script)
    report = await a.run(ctx, provider)
    assert report.status == "complete"
    assert llm.calls == 3 and report.tool_calls == 6


def test_fallback_narrative_for_rare_events():
    from fair.agent.loop import fallback_narrative
    from fair.model import ProbabilityRange, Range, ScenarioInputs, simulate

    rare = ScenarioInputs(
        scenario="vendor_breach",
        threat_event_frequency=Range.fixed(0.03, "assumption"),
        vulnerability=ProbabilityRange.fixed(1, "assumption"),
        primary_loss={"response": Range.fixed(5e6, "assumption")},
    )
    dist = simulate(rare, seed=1, iterations=20_000).distribution
    assert dist.p90 == 0
    text = fallback_narrative("V", dist, agent_runs=1, n_runs=2)
    assert "P90" not in text and "of years" in text and "P99" in text
    assert "1 of 2 scenarios use the agent's evidence-based inputs" in text


# ── Token use ─────────────────────────────────────────────────────────────


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


async def test_pacer_waits_only_when_the_window_is_full():
    from fair.agent.pacing import TokenPacer

    clock = FakeClock()
    pacer = TokenPacer(8_000, clock, clock.sleep)
    assert await pacer.wait_for(3_000) == 0
    pacer.record(3_000)
    clock.now += 5
    assert await pacer.wait_for(4_000) == 0  # 7,000 fits
    pacer.record(4_000)
    clock.now += 5
    waited = await pacer.wait_for(
        3_000
    )  # 10,000 would not: wait for the first to expire
    assert waited == pytest.approx(50.05)
    assert clock.now >= 60


async def test_pacer_sends_oversized_call_after_window_clears():
    from fair.agent.pacing import TokenPacer

    clock = FakeClock()
    pacer = TokenPacer(8_000, clock, clock.sleep)
    pacer.record(1_000)
    await pacer.wait_for(
        20_000
    )  # bigger than the limit: wait for an empty window, then go
    assert clock.now >= 60


def with_usage(item, tokens_in, tokens_out):
    def reply(messages):
        msg = item(messages) if callable(item) else item
        msg.usage_metadata = {
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
            "total_tokens": tokens_in + tokens_out,
        }
        return msg

    return reply


async def test_tokens_counted_and_budgeted():
    ctx, provider = load_example()
    script = [with_usage(step, 3_000, 500) for step in HAPPY_PATH]
    a, _ = agent(script)
    report = await a.run(ctx, provider)
    assert report.status == "complete"
    assert report.input_tokens == 7 * 3_000 and report.output_tokens == 7 * 500

    a, _ = agent(script, budget=Budget(max_total_tokens=8_000))
    report = await a.run(ctx, provider)
    assert report.generated_by == "fallback"
    assert "token budget" in report.notes[0]


async def test_paced_agent_waits_instead_of_being_throttled():
    ctx, provider = load_example()
    clock = FakeClock()
    # per-call sizes seen in live runs: ~3K in, ~300 out
    script = [with_usage(step, 3_000, 300) for step in HAPPY_PATH]
    llm = ScriptedLLM(script)
    a = FairAgent(
        llm, iterations=2_000, tokens_per_minute=8_000, sleep=clock.sleep, clock=clock
    )
    report = await a.run(ctx, provider)
    assert report.status == "complete"
    assert clock.slept and report.paced_seconds == pytest.approx(sum(clock.slept))


async def test_seen_tool_results_are_compacted():
    ctx, provider = load_example()
    seen_by_model = []

    class Recording(ScriptedLLM):
        async def ainvoke(self, messages):
            seen_by_model.append(
                [m.content for m in messages if isinstance(m, ToolMessage)]
            )
            return await super().ainvoke(messages)

    llm = Recording(HAPPY_PATH)
    report = await FairAgent(llm, iterations=2_000, sleep=no_sleep).run(ctx, provider)
    assert report.status == "complete"

    # findings: full the first time the model sees them; later only ids and
    # titles (still citable), without details
    first, later = seen_by_model[1][0], seen_by_model[-1][0]
    assert "detail" in first and "detail" not in later and len(later) < len(first)
    assert json.loads(later)["results"][0]["evidence"][0] == {
        "id": "ev_1",
        "title": "SOC 2 Type 2 report verified",
    }
    # a baseline stays in full until its scenario has been simulated
    assert "threat_event_frequency" in seen_by_model[3][2]
    assert "already used" in seen_by_model[-1][2]
    # simulation results are never compacted (they are quoted in the report)
    assert "expected_annual_loss" in seen_by_model[-1][3]


async def test_unchanged_range_keeps_baseline_rationale():
    ctx, provider = load_example()

    def drop_rationales(messages):
        inputs = baseline_of(messages)
        for r in (inputs["threat_event_frequency"], inputs["vulnerability"]):
            r.pop("rationale", None)
        return run(inputs)

    script = [
        call("get_baseline_inputs", scenarios=["vendor_breach"]),
        drop_rationales,
        submit_with(),
    ]
    a, _ = agent(script)
    report = await a.run(breach_only(ctx), provider)
    assert report.status == "complete"
    assert all(i.rationale for i in report.scenarios[0].inputs)


async def test_changed_range_without_rationale_is_refused():
    ctx, provider = load_example()

    def change_silently(messages):
        inputs = baseline_of(messages)
        inputs["threat_event_frequency"] = {
            "min": 0.01,
            "mode": 0.03,
            "max": 0.1,
            "source": "ev_1",
        }
        return run(inputs)

    script = [
        call("get_agent_findings", domains=["compliance"]),
        call("get_baseline_inputs", scenarios=["vendor_breach"]),
        change_silently,
    ]
    a, _ = agent(script, budget=Budget(max_llm_calls=3))
    report = await a.run(breach_only(ctx), provider)
    refusal = next(t.summary for t in report.trace if t.kind == "tool_error")
    assert "give a rationale" in refusal and "threat_event_frequency" in refusal


def test_schemas_are_slim():
    ctx, provider = load_example()
    tools = build_tools(Session(ctx, provider, 100))
    schemas = json.dumps([t.schema() for t in tools.values()])
    assert '"title"' not in schemas
    assert "Beta-PERT" not in schemas  # class docstrings are not resent per Range
    assert "list_scenarios" not in tools
    # field descriptions the model needs are kept
    assert "Exactly one run id per scenario" in schemas
    # regression guard: 4,153 chars before slimming; ~2,950 now that it takes a
    # list of runs (one call instead of one per scenario)
    assert len(json.dumps(tools["run_simulation"].schema())) < 3_100


# ── List tools: fewer turns for one-call-per-reply models ─────────────────


async def test_list_tools_finish_in_four_turns():
    ctx, provider = load_example()
    script = [
        call("get_baseline_inputs", scenarios=["vendor_breach", "vendor_insolvency"]),
        call("get_agent_findings", domains=["compliance", "reputation"]),
        lambda m: run(
            *[b["inputs"] for b in results_named(m, '"baselines"')[0]["baselines"]]
        ),
        submit_with(),
    ]
    a, llm = agent(script)
    report = await a.run(ctx, provider)
    assert report.status == "complete"
    assert llm.calls == 4 and report.tool_calls == 4
    assert [s.run_id for s in report.scenarios] == ["run_01", "run_02"]
    assert len(report.evidence) == 3


async def test_partial_errors_in_a_list_call_are_reported_alongside_results():
    ctx, provider = load_example()

    def one_good_one_bad(messages):
        good = baseline_of(messages)
        bad = json.loads(json.dumps(good))
        bad["threat_event_frequency"]["source"] = "ev_404"
        return run(good, bad)

    script = [
        call("get_agent_findings", domains=["compliance", "finance"]),
        call("get_baseline_inputs", scenarios=["vendor_breach"]),
        one_good_one_bad,
    ]
    a, _ = agent(script, budget=Budget(max_llm_calls=3))
    report = await a.run(breach_only(ctx), provider)
    summaries = " ".join(t.summary for t in report.trace)
    assert "compliance: 2 evidence items; errors: unknown domain 'finance'" in summaries
    assert "run_01 vendor_breach" in summaries and "ev_404 does not exist" in summaries


async def test_several_evidence_ids_can_back_one_range():
    ctx, provider = load_example()

    def cite_two(messages):
        inputs = baseline_of(messages)
        inputs["threat_event_frequency"] = {
            "min": 0.005,
            "mode": 0.02,
            "max": 0.08,
            "source": "ev_1, ev_3",
            "rationale": "SOC 2 Type 2 and no breach history.",
        }
        return run(inputs)

    script = [
        call("get_agent_findings", domains=["compliance", "reputation"]),
        call("get_baseline_inputs", scenarios=["vendor_breach"]),
        cite_two,
        submit_with(),
    ]
    a, _ = agent(script)
    report = await a.run(breach_only(ctx), provider)
    assert report.status == "complete"
    tef = report.scenarios[0].inputs[0]
    assert tef.source == "ev_1, ev_3"


def test_call_estimator_learns_from_real_counts():
    from langchain_core.messages import HumanMessage

    from fair.agent.loop import _CallEstimator

    est = _CallEstimator(schema_tokens=1_000)
    convo = [HumanMessage("x" * 4_000)]  # ~1,000 tokens by character count
    first = est.next_call(convo)
    assert first == 1_000 + 1_000 + 1_200  # chars + schemas + default output

    reply = AIMessage(content="")
    reply.usage_metadata = {
        "input_tokens": 1_500,
        "output_tokens": 300,
        "total_tokens": 1_800,
    }
    est.observe(convo, reply)
    grown = convo + [HumanMessage("y" * 4_000)]  # conversation doubled
    assert est.next_call(grown) == 3_000 + 300  # scaled real input + real output


async def test_get_financials_includes_altman_in_one_call():
    ctx, provider = load_example()
    a, _ = agent([call("get_financials")], budget=Budget(max_llm_calls=1))
    report = await a.run(ctx, provider)
    step = next(t for t in report.trace if t.tool == "get_financials")
    assert step.summary == "financials; Altman z_double_prime = 3.195 (safe)"
    titles = [e.title for e in report.evidence]
    assert titles[:2] == [
        "Vendor financial statement",
        "Altman z_double_prime = 3.20 (safe zone)",
    ]


@pytest.mark.parametrize(
    "text, ok",
    [
        ("$4.45M", True),  # as shown
        ("$4.452M", True),  # more digits of a real value (4,452,000)
        ("$4.5M", True),  # correct rounding to 2 significant figures
        ("$4M", False),  # 1 significant figure: too loose
        ("$4.46M", False),  # wrong at its own precision
        ("$1,500", True),
        ("$2K", False),  # $1.5K rounded to 1 significant figure
        ("$3.7M", False),  # nothing near it
    ],
)
def test_figure_matching(text, ok):
    from fair.agent.guardrails import figure_matches

    assert figure_matches(text, [4_452_000.0, 1_500.0]) is ok


async def test_rejection_suggests_the_nearest_real_figure():
    ctx, provider = load_example()
    script = [
        *HAPPY_PATH[:-1],
        submit_with(extra_text=" Insolvency costs about $2K."),
        submit_with(),
    ]
    a, _ = agent(script)
    report = await a.run(ctx, provider)
    assert report.status == "complete"
    rejection = next(t.summary for t in report.trace if "rejected" in t.summary)
    assert re.search(r"\$2K \(nearest tool figure: \$[\d.]+K\)", rejection)


async def test_quoting_an_input_value_is_allowed():
    ctx, provider = load_example()
    script = [
        *HAPPY_PATH[:-1],
        submit_with(extra_text=" A typical healthcare breach response costs $4.452M."),
    ]
    a, _ = agent(script)
    assert (await a.run(ctx, provider)).status == "complete"


class DailyLimitError(Exception):
    status_code = 429


async def test_daily_limit_is_not_retried_and_account_ids_are_hidden():
    ctx, provider = load_example()
    error = DailyLimitError(
        "Rate limit reached for model `openai/gpt-oss-120b` in organization "
        "`org_01abcDEF` on tokens per day (TPD): Limit 200000, Used 196796. "
        "Please try again in 4m50.736s."
    )
    clock = FakeClock()
    llm = ScriptedLLM([error, *HAPPY_PATH])
    report = await FairAgent(llm, iterations=2_000, sleep=clock.sleep, clock=clock).run(
        ctx, provider
    )
    assert llm.calls == 1 and clock.slept == []  # no pointless retries
    assert report.generated_by == "fallback"
    assert (
        "daily token limit reached; the provider says retry in 4m50.736s"
        in report.notes[0]
    )
    assert "org_01abcDEF" not in report.model_dump_json()


async def test_other_errors_are_redacted_in_the_trace():
    ctx, provider = load_example()
    a, _ = agent([RuntimeError("boom for org_SECRET123 and user_abc")])
    report = await a.run(ctx, provider)
    text = report.model_dump_json()
    assert "org_SECRET123" not in text and "org_<redacted>" in text


def test_tail_note_is_written_in_code_when_p99_exceeds_tolerance():
    from fair.agent.report import LossSummary, tail_note
    from fair.context import Tolerance

    def summary(ale, p99):
        return LossSummary(
            ale_mean=ale,
            p10=0,
            p50=0,
            p90=0,
            p95=0,
            p99=p99,
            prob_any_loss=0.03,
            prob_exceeds_tolerance=0.03,
            loss_exceedance_curve=[],
        )

    derived = Tolerance(value_usd=200_000, source="derived", rationale="0.5x ACV")
    note = tail_note(summary(152_000, 6_890_000), derived)
    assert note == (
        "Tail risk: a 1-in-100-year loss of $6.89M is 34× the derived tolerance of "
        "$200K, even though the expected annual loss is within it."
    )
    assert tail_note(summary(152_000, 150_000), derived) is None
    assert tail_note(summary(152_000, 6_890_000), None) is None
