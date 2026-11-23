"""
The FAIR agent: a tool-calling loop.

    brief -> [model picks tools -> tools run -> results back] ... -> submit_report

The loop owns the guardrails. It counts the budget, validates every tool's
arguments, sends errors back for the model to fix, and checks the final
submission. When the model cannot finish (budget spent, repeated rejection,
an API failure), it falls back to the deterministic baseline, so an analysis
always ends with a report, marked partial.
"""

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from pydantic import ValidationError

from fair.agent.baseline import NoBaseline, baseline_inputs
from fair.agent.compaction import compact
from fair.agent.guardrails import Budget, GuardrailError, check_submission
from fair.agent.pacing import TokenPacer
from fair.agent.prompts import NUDGE, SYSTEM_PROMPT, brief
from fair.agent.report import FairReport, assemble
from fair.agent.session import Session
from fair.agent.tools import MAX_RESULT_CHARS, SubmitArgs, Tool, ToolError, build_tools
from fair.context import AnalysisContext
from fair.format import format_usd
from fair.model.simulation import DEFAULT_ITERATIONS, SimulationResult, combine
from fair.ports import AskUser, EvidenceProvider

MAX_RATE_LIMIT_RETRIES = 3
MAX_REJECTED_SUBMISSIONS = 2
MAX_IDLE_TURNS = 2  # replies without a tool call before giving up
# Pacing estimates before the first real token count is known
EXPECTED_OUTPUT_TOKENS = 1_200
CHARS_PER_TOKEN = 4


class ToolCallingModel(Protocol):
    """What the loop needs from a chat model (LangChain's ChatGroq fits)."""

    def bind_tools(self, tools: list[dict]) -> Any: ...


class _Stop(Exception):
    """The model cannot finish; build the fallback report."""


class FairAgent:
    def __init__(
        self,
        llm: ToolCallingModel,
        *,
        budget: Budget | None = None,
        iterations: int = DEFAULT_ITERATIONS,
        tokens_per_minute: int | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """
        tokens_per_minute: the provider's limit (8,000 on Groq's free tier).
        When set, calls are paced to stay under it instead of being throttled.
        """
        self.llm = llm
        self.budget = budget or Budget()
        self.iterations = iterations
        self.tokens_per_minute = tokens_per_minute
        self._sleep = sleep
        self._clock = clock

    async def run(
        self,
        context: AnalysisContext,
        provider: EvidenceProvider,
        ask_user: AskUser | None = None,
    ) -> FairReport:
        session = Session(context, provider, self.iterations, ask_user)
        try:
            return await self._loop(session)
        except _Stop as stop:
            return await fallback_report(session, str(stop))

    async def _loop(self, session: Session) -> FairReport:
        tools = build_tools(session)
        schemas = [t.schema() for t in tools.values()]
        model = self.llm.bind_tools(schemas)
        schema_tokens = len(_dumps(schemas)) // CHARS_PER_TOKEN
        pacer = (
            TokenPacer(self.tokens_per_minute, self._clock, self._sleep)
            if self.tokens_per_minute
            else None
        )
        results: dict[str, tuple[str, dict]] = {}  # tool_call_id -> (tool, payload)
        sent: set[str] = set()  # tool results the model has already seen
        messages: list[BaseMessage] = [
            SystemMessage(SYSTEM_PROMPT),
            HumanMessage(
                brief(session.context, session.provider, self.budget.max_llm_calls)
            ),
        ]
        started = self._clock()
        idle = rejected = 0
        estimate = _CallEstimator(schema_tokens)

        while True:
            self._check_budget(session, started)
            outgoing = _compacted(messages, results, sent, session)
            if pacer is not None:
                expected = estimate.next_call(outgoing)
                session.paced_seconds += await pacer.wait_for(expected)
            reply = await self._invoke(model, outgoing, session)
            estimate.observe(outgoing, reply)
            if pacer is not None:
                pacer.record(_reply_tokens(reply) or expected)
            sent.update(m.tool_call_id for m in messages if isinstance(m, ToolMessage))
            messages.append(reply)
            if isinstance(reply.content, str) and reply.content.strip():
                session.record("thought", reply.content.strip())

            if not reply.tool_calls:
                idle += 1
                if idle >= MAX_IDLE_TURNS:
                    raise _Stop("the model stopped calling tools")
                messages.append(HumanMessage(NUDGE))
                continue
            idle = 0

            for call in reply.tool_calls:
                session.tool_calls += 1
                payload, args = await self._call_tool(tools, call, session)
                if call["name"] == "submit_report" and isinstance(args, SubmitArgs):
                    try:
                        final = check_submission(
                            session,
                            args.final_run_ids,
                            [args.narrative, *args.key_findings, *args.recommendations],
                            args.recommended_control_runs,
                        )
                    except GuardrailError as e:
                        rejected += 1
                        session.record(
                            "guardrail",
                            f"submission rejected: {e}",
                            tool="submit_report",
                        )
                        if rejected >= MAX_REJECTED_SUBMISSIONS:
                            raise _Stop(
                                f"submission rejected {rejected} times: {e}"
                            ) from e
                        payload = {"error": f"submission rejected: {e}"}
                    else:
                        session.record(
                            "guardrail",
                            f"submission accepted: {', '.join(args.final_run_ids)}",
                            tool="submit_report",
                        )
                        return assemble(
                            session,
                            final,
                            status="complete",
                            generated_by="agent",
                            narrative=args.narrative,
                            key_findings=args.key_findings,
                            recommendations=args.recommendations,
                            notes=_missing_scenario_notes(session, final),
                            recommended_control_runs=args.recommended_control_runs,
                        )
                results[call["id"] or ""] = (call["name"], payload)
                messages.append(
                    ToolMessage(
                        content=_dumps(payload)[:MAX_RESULT_CHARS],
                        tool_call_id=call["id"],
                    )
                )

    def _check_budget(self, session: Session, started: float) -> None:
        b = self.budget
        if session.llm_calls >= b.max_llm_calls:
            raise _Stop(f"used all {b.max_llm_calls} model calls")
        if session.tool_calls >= b.max_tool_calls:
            raise _Stop(f"used all {b.max_tool_calls} tool calls")
        if self._clock() - started >= b.max_seconds:
            raise _Stop(f"ran out of time ({b.max_seconds:g}s)")
        if session.input_tokens + session.output_tokens >= b.max_total_tokens:
            raise _Stop(f"used the token budget ({b.max_total_tokens:,} tokens)")

    async def _invoke(
        self, model: Any, messages: list[BaseMessage], session: Session
    ) -> AIMessage:
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            try:
                session.llm_calls += 1
                reply = await model.ainvoke(messages)
            except Exception as e:
                if _is_daily_limit(e):
                    # Retrying cannot help until the provider's day window frees up
                    msg = "daily token limit reached" + _retry_after(e)
                    session.record("error", msg)
                    raise _Stop(msg) from e
                if _is_rate_limit(e) and attempt < MAX_RATE_LIMIT_RETRIES:
                    wait = min(5 * 2**attempt, 60)
                    session.record("error", f"rate limited; retrying in {wait}s")
                    await self._sleep(wait)
                    continue
                session.record(
                    "error", f"model call failed: {type(e).__name__}: {_clean(str(e))}"
                )
                raise _Stop(f"model call failed: {type(e).__name__}") from e
            if not isinstance(reply, AIMessage):
                raise _Stop(f"model returned {type(reply).__name__}, not AIMessage")
            usage: dict[str, Any] = dict(reply.usage_metadata or {})
            session.input_tokens += usage.get("input_tokens", 0)
            session.output_tokens += usage.get("output_tokens", 0)
            return reply
        raise _Stop("rate limited")  # unreachable: the last attempt raises above

    async def _call_tool(
        self, tools: dict[str, Tool], call: Mapping[str, Any], session: Session
    ) -> tuple[dict, Any]:
        name, raw = call["name"], call.get("args") or {}
        tool = tools.get(name)
        if tool is None:
            session.record("tool_error", f"unknown tool {name!r}", tool=name)
            return {
                "error": f"unknown tool {name!r}; available: {', '.join(tools)}"
            }, None
        try:
            args = tool.args_model.model_validate(raw)
        except ValidationError as e:
            msg = _validation_message(e)
            session.record(
                "tool_error",
                f"invalid arguments: {msg}",
                tool=name,
                detail={"args": raw},
            )
            return {"error": f"invalid arguments: {msg}"}, None
        try:
            payload = await tool.handler(args)
        except (ToolError, GuardrailError) as e:
            session.record("tool_error", str(e), tool=name, detail={"args": raw})
            return {"error": str(e)}, args
        except Exception as e:  # noqa: BLE001 - any tool failure goes back to the model
            session.record("tool_error", f"{type(e).__name__}: {e}", tool=name)
            return {"error": f"tool failed: {type(e).__name__}"}, args
        session.record(
            "tool_call", _summarise(name, raw, payload), tool=name, detail={"args": raw}
        )
        return payload, args


async def fallback_report(session: Session, reason: str) -> FairReport:
    """
    Deterministic report: per candidate scenario, the agent's latest run if it
    made one, otherwise the baseline. Always marked partial.
    """
    session.record("fallback", f"agent did not finish ({reason}); using baseline")
    latest = session.ledger.latest_by_scenario()
    financials = await session.provider.financials()
    final: list[SimulationResult] = []
    notes = [f"The agent did not finish: {reason}."]
    for scenario in session.context.candidate_scenarios:
        if scenario in latest:
            final.append(latest[scenario])
            notes.append(
                f"{scenario}: the agent's last run ({latest[scenario].run_id}) is used."
            )
            continue
        try:
            inputs = baseline_inputs(scenario, session.context, financials)
        except NoBaseline:
            notes.append(f"{scenario}: no baseline available; not quantified.")
            continue
        final.append(session.ledger.run(inputs))
        notes.append(f"{scenario}: benchmark baseline used.")
    if not final:
        raise RuntimeError("no scenario could be quantified")

    agent_runs = sum(1 for r in final if r.inputs.label != "Baseline from benchmarks")
    narrative = fallback_narrative(
        session.context.subject, combine(final).distribution, agent_runs, len(final)
    )
    return assemble(
        session,
        final,
        status="partial",
        generated_by="fallback",
        narrative=narrative,
        notes=notes,
    )


def fallback_narrative(subject: str, total, agent_runs: int, n_runs: int) -> str:
    """Plain words for a fallback report; correct for rare events too."""
    if total.p90 > 0:
        tail = f"a 1-in-10 bad year (P90) costs {format_usd(total.p90)}"
    elif total.prob_any_loss > 0:
        # Rare events: most simulated years lose nothing, so P90 is $0 and
        # would read as "no risk". Describe how often a loss happens instead.
        tail = (
            f"a loss occurs in {total.prob_any_loss:.1%} of years, and a 1-in-100 year "
            f"(P99) costs {format_usd(total.p99)}"
        )
    else:
        tail = "no loss occurred in any simulated year"
    if agent_runs == 0:
        basis = "Built from published benchmarks and the client profile, not vendor evidence"
    elif agent_runs == n_runs:
        basis = "Built from the agent's evidence-based inputs, but its review was not finished"
    else:
        basis = (
            f"{agent_runs} of {n_runs} scenarios use the agent's evidence-based inputs; "
            "the rest use benchmark baselines"
        )
    return (
        f"Estimate for {subject}: expected annual loss {format_usd(total.ale_mean)}; "
        f"{tail}. {basis}, so treat it as indicative."
    )


def _missing_scenario_notes(
    session: Session, final: list[SimulationResult]
) -> list[str]:
    done = {r.inputs.scenario for r in final}
    return [
        f"{s} was a candidate scenario but has no final run."
        for s in session.context.candidate_scenarios
        if s not in done
    ]


def _dumps(payload: Any) -> str:
    """Compact JSON: no spaces after separators, so fewer tokens per call."""
    return json.dumps(payload, default=str, separators=(",", ":"), ensure_ascii=False)


def _compacted(
    messages: list[BaseMessage],
    results: dict[str, tuple[str, dict]],
    sent: set[str],
    session: Session,
) -> list[BaseMessage]:
    """The conversation to send: tool results already seen, in short form."""
    out: list[BaseMessage] = []
    for m in messages:
        if isinstance(m, ToolMessage) and m.tool_call_id in sent:
            name, payload = results.get(m.tool_call_id, ("", {}))
            short = compact(name, payload, session)
            if short is not None:
                m = ToolMessage(content=_dumps(short), tool_call_id=m.tool_call_id)
        out.append(m)
    return out


class _CallEstimator:
    """
    Predicts the next call's tokens for pacing. Before any call it counts
    characters; afterwards it scales the last call's real input count by how
    much the conversation grew, and uses the average real output so far.
    Over-estimating makes the pacer wait longer than it needs to.
    """

    def __init__(self, schema_tokens: int) -> None:
        self.schema_tokens = schema_tokens
        self.last_input: int | None = None
        self.last_chars = 0
        self.outputs: list[int] = []

    def next_call(self, outgoing: list[BaseMessage]) -> int:
        chars = _estimate_tokens(outgoing) * CHARS_PER_TOKEN
        if self.last_input and self.last_chars:
            tokens_in = int(self.last_input * chars / self.last_chars)
        else:
            tokens_in = chars // CHARS_PER_TOKEN + self.schema_tokens
        tokens_out = (
            int(sum(self.outputs) / len(self.outputs))
            if self.outputs
            else EXPECTED_OUTPUT_TOKENS
        )
        return tokens_in + tokens_out

    def observe(self, outgoing: list[BaseMessage], reply: AIMessage) -> None:
        usage: dict[str, Any] = dict(reply.usage_metadata or {})
        if usage.get("input_tokens"):
            self.last_input = int(usage["input_tokens"])
            self.last_chars = _estimate_tokens(outgoing) * CHARS_PER_TOKEN
        if usage.get("output_tokens"):
            self.outputs.append(int(usage["output_tokens"]))


def _estimate_tokens(messages: list[BaseMessage]) -> int:
    chars = 0
    for m in messages:
        chars += (
            len(m.content) if isinstance(m.content, str) else len(_dumps(m.content))
        )
        if isinstance(m, AIMessage) and m.tool_calls:
            chars += len(_dumps(m.tool_calls))
    return chars // CHARS_PER_TOKEN


def _reply_tokens(reply: AIMessage) -> int:
    usage: dict[str, Any] = dict(reply.usage_metadata or {})
    return int(usage.get("total_tokens", 0))


def _is_daily_limit(e: Exception) -> bool:
    text = str(e).lower()
    return _is_rate_limit(e) and (
        "per day" in text or "(tpd)" in text or "(rpd)" in text
    )


def _retry_after(e: Exception) -> str:
    match = re.search(r"try again in ([\dhms.]+)", str(e))
    return f"; the provider says retry in {match.group(1)}" if match else ""


def _clean(text: str) -> str:
    """Provider error text for the trace: no account ids, and short."""
    text = re.sub(r"\b(org|user|proj|key)_[A-Za-z0-9]+", r"\1_<redacted>", text)
    return text[:300]


def _is_rate_limit(e: Exception) -> bool:
    return (
        "RateLimit" in type(e).__name__
        or getattr(e, "status_code", None) == 429
        or "rate limit" in str(e).lower()
    )


def _validation_message(e: ValidationError) -> str:
    parts = [
        f"{'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}"
        for err in e.errors()[:6]
    ]
    more = len(e.errors()) - 6
    return "; ".join(parts) + (f"; and {more} more" if more > 0 else "")


def _summarise(name: str, args: dict, payload: dict) -> str:
    errors = (
        "; errors: " + "; ".join(payload["errors"]) if payload.get("errors") else ""
    )
    if name == "run_simulation":
        return (
            "; ".join(
                f"{r['run_id']} {r['scenario']}: expected {r['expected_annual_loss']}, "
                f"P90 {r['p90']}"
                for r in payload.get("runs", [])
            )
            + errors
        )
    if name == "get_agent_findings":
        return (
            ", ".join(
                f"{r['domain']}: {len(r['evidence'])} evidence items"
                for r in payload.get("results", [])
            )
            + errors
        )
    if name == "get_financials":
        z = payload.get("altman") or {}
        if not payload.get("available"):
            return "no financial statements"
        if z.get("status") != "ok":
            return f"financials; Altman Z not computable ({z.get('status')})"
        return f"financials; Altman {z.get('model')} = {z.get('z')} ({z.get('zone')})"
    if name == "search_sec_filings":
        if "note" in payload:
            return payload["note"]
        return ", ".join(
            f"{r['topic']}: {len(r.get('filings', []))} filings"
            if "error" not in r
            else f"{r['topic']}: {r['error']}"
            for r in payload.get("results", [])
        )
    if name == "sensitivity_analysis":
        return "; ".join(
            f"{r['run_id']}: top driver {r['drivers'][0]['input']}"
            for r in payload.get("results", [])
            if r.get("drivers")
        )
    if name == "test_control":
        return (
            "; ".join(
                f"{r['run_id']} {r['control_id']} on {r['base_run_id']}: "
                f"expected {r['expected_annual_loss']}"
                for r in payload.get("results", [])
            )
            + errors
        )
    if name == "lookup_benchmark":
        return f"{args.get('topic')}: {len(payload.get('benchmarks', []))} benchmarks"
    if name == "get_baseline_inputs":
        return (
            "baselines for "
            + ", ".join(b["inputs"]["scenario"] for b in payload.get("baselines", []))
            + errors
        )
    return name
