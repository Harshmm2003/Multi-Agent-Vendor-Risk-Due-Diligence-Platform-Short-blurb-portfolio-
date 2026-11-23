"""
The tools the FAIR agent can call. All read-only towards the outside world:
they gather evidence and run calculations, and every result lands in the
session's evidence store or run ledger.

A handler raises ToolError (or GuardrailError) with a message the model can
act on; the loop sends it back as the tool result instead of failing.
"""

import copy
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from fair import benchmarks
from fair.agent.baseline import NoBaseline, baseline_inputs
from fair.agent.guardrails import (
    GuardrailError,
    check_not_double_counted,
    check_sources,
)
from fair.agent.report import run_figures
from fair.agent.session import RunMeta, Session
from fair.format import format_percent, format_usd
from fair.model.altman import altman_z
from fair.model.controls import apply_control
from fair.model.scenarios import TEMPLATES
from fair.model.sensitivity import drivers
from fair.model.taxonomy import Scenario, ScenarioInputs
from fair.ports import EvidenceDraft, FilingTopic

# Tool results are cut to this many characters before the model sees them.
MAX_RESULT_CHARS = 4_000


class ToolError(Exception):
    pass


Handler = Callable[[Any], Awaitable[dict]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Handler

    def schema(self) -> dict:
        """
        OpenAI-style function definition: $refs inlined (some models ignore
        $defs) and titles dropped. Every schema is resent on every model call,
        so its size counts against the provider's tokens-per-minute limit.
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": slim_schema(
                    inline_refs(self.args_model.model_json_schema())
                ),
            },
        }


# ── Argument models ────────────────────────────────────────────────────────


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


# The tools take lists because some models (gpt-oss on Groq) make only one
# tool call per reply: one call per list instead of one call per item saves
# whole turns, and every turn resends the full conversation.


class DomainsArgs(_Args):
    domains: list[str] = Field(
        min_length=1, description="All the domains you need, from the brief"
    )


class AltmanArgs(_Args):
    manufacturer: bool | None = Field(
        default=None,
        description="Use the original Z model (manufacturers). Default: what the data says",
    )


class TopicArgs(_Args):
    topic: str = Field(
        description="e.g. breach_cost, breach_frequency, default_rate, fines"
    )


class ScenariosArgs(_Args):
    scenarios: list[Scenario] = Field(
        min_length=1, description="All candidate scenarios at once"
    )


class RunArgs(_Args):
    runs: list[ScenarioInputs] = Field(
        min_length=1,
        max_length=4,
        description="One entry per scenario to simulate, all in one call",
    )


class SubmitArgs(_Args):
    final_run_ids: list[str] = Field(
        description="Exactly one run id per scenario analysed"
    )
    narrative: str = Field(
        description="3-6 sentences for a risk committee. Only use figures exactly as tools showed them"
    )
    key_findings: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    recommended_control_runs: list[str] = Field(
        default_factory=list,
        description="Run ids from test_control whose control you recommend",
    )


class FilingArgs(_Args):
    topics: list[FilingTopic] = Field(
        min_length=1,
        description="cyber_incident (8-K Item 1.05), going_concern, privacy_order",
    )


class RunIdsArgs(_Args):
    run_ids: list[str] = Field(
        min_length=1, max_length=4, description="Scenario run ids"
    )


class ControlTest(_Args):
    run_id: str = Field(description="The scenario run to apply the control to")
    control_id: str = Field(description="A control id from the brief")
    parameter: float | None = Field(
        default=None, description="Only for controls that take one (see the brief)"
    )


class ControlArgs(_Args):
    tests: list[ControlTest] = Field(min_length=1, max_length=6)


class AskArgs(_Args):
    question: str = Field(
        description="One specific question, e.g. how many records are shared"
    )


# ── Tools ──────────────────────────────────────────────────────────────────


def build_tools(session: Session) -> dict[str, Tool]:
    ctx = session.context

    async def get_agent_findings(args: DomainsArgs) -> dict:
        known = session.provider.finding_domains()
        results, errors = [], []
        for domain in dict.fromkeys(args.domains):  # de-duplicated, in order
            if domain not in known:
                errors.append(
                    f"unknown domain {domain!r}; available: {', '.join(known)}"
                )
                continue
            items = [
                session.evidence.add(d)
                for d in await session.provider.agent_findings(domain)
            ]
            results.append(
                {
                    "domain": domain,
                    "evidence": [
                        i.model_dump(include={"id", "title", "detail", "citation"})
                        for i in items
                    ],
                }
            )
        if not results:
            raise ToolError("; ".join(errors))
        return {"results": results, **({"errors": errors} if errors else {})}

    async def get_financials(args: AltmanArgs) -> dict:
        """The statement and its Altman Z in one call (one turn, not two)."""
        data = await session.provider.financials()
        if data is None:
            return {
                "available": False,
                "note": "No financial statements for this vendor.",
            }
        statement_ev = session.evidence.add(
            EvidenceDraft(
                kind="financial",
                title="Vendor financial statement",
                detail=data.statement.model_dump_json(exclude_none=True),
                citation=data.citation,
            )
        )
        out: dict[str, Any] = {
            "available": True,
            "evidence_id": statement_ev.id,
            "statement": data.statement.model_dump(exclude_none=True),
            "citation": data.citation,
        }
        manufacturer = (
            data.manufacturer if args.manufacturer is None else args.manufacturer
        )
        z = altman_z(data.statement, manufacturer=manufacturer)
        if z.status != "ok" or z.z is None or z.annual_default_probability is None:
            out["altman"] = {"status": z.status, "missing": z.missing}
            return out
        z_ev = session.evidence.add(
            EvidenceDraft(
                kind="financial",
                title=f"Altman {z.model} = {z.z:.2f} ({z.zone} zone)",
                detail=f"components {z.components}",
                citation=f"Computed from: {data.citation}",
            )
        )
        out["altman"] = {
            "status": "ok",
            "evidence_id": z_ev.id,
            "model": str(z.model),
            "z": round(z.z, 3),
            "zone": str(z.zone),
            "annual_default_probability": z.annual_default_probability.model_dump(),
            "note": "The insolvency baseline already uses this default probability.",
        }
        return out

    async def lookup_benchmark(args: TopicArgs) -> dict:
        found = benchmarks.lookup(args.topic)
        if not found:
            raise ToolError(
                f"no benchmark for {args.topic!r}; topics: {', '.join(benchmarks.topics())}"
            )
        return {
            "benchmarks": [
                {
                    "source": b.ref,
                    "value": b.value,
                    "unit": b.unit,
                    "description": b.description,
                    "citation": f"{b.source} ({b.year})",
                }
                for b in found
            ]
        }

    async def get_baseline_inputs(args: ScenariosArgs) -> dict:
        financials = await session.provider.financials()
        baselines, errors = [], []
        for scenario in dict.fromkeys(args.scenarios):
            try:
                inputs = baseline_inputs(scenario, ctx, financials)
            except NoBaseline as e:
                errors.append(str(e))
                continue
            baselines.append(
                {
                    "inputs": inputs.model_dump(mode="json", exclude_defaults=True),
                    "guidance": TEMPLATES[scenario].guidance,
                }
            )
        if not baselines:
            raise ToolError("; ".join(errors))
        return {
            "baselines": baselines,
            "note": "Starting points. Refine ranges with evidence, then run_simulation.",
            **({"errors": errors} if errors else {}),
        }

    async def run_simulation(args: RunArgs) -> dict:
        runs, errors = [], []
        for i, inputs in enumerate(args.runs):
            try:
                check_sources(inputs, session)
                check_not_double_counted(inputs)
                inputs = await _with_rationales(inputs)
            except (GuardrailError, ToolError) as e:
                errors.append(f"runs[{i}] {inputs.scenario}: {e}")
                continue
            runs.append(run_figures(session.ledger.run(inputs), ctx.tolerance()))
        if not runs:
            raise ToolError("; ".join(errors))
        return {"runs": runs, **({"errors": errors} if errors else {})}

    async def _with_rationales(inputs: ScenarioInputs) -> ScenarioInputs:
        """
        Every range must say why. A range the model left unexplained keeps the
        baseline's rationale if it still cites the baseline's source; any other
        unexplained range is refused.
        """
        blank = [path for path, r in inputs.ranges() if not r.rationale.strip()]
        if not blank:
            return inputs
        try:
            base = baseline_inputs(
                inputs.scenario, ctx, await session.provider.financials()
            )
            base_ranges = dict(base.ranges())
        except NoBaseline:
            base_ranges = {}
        data = inputs.model_dump()
        missing = []
        for path in blank:
            ref = base_ranges.get(path)
            current = dict(inputs.ranges())[path]
            if ref is not None and ref.source == current.source and ref.rationale:
                node = data
                *parents, leaf = path.split(".")
                for key in parents:
                    node = node[key]
                node[leaf]["rationale"] = ref.rationale
            else:
                missing.append(path)
        if missing:
            raise ToolError(
                "give a rationale (why this range, from which evidence) for: "
                + ", ".join(missing)
            )
        return ScenarioInputs.model_validate(data)

    async def search_sec_filings(args: FilingArgs) -> dict:
        results: list[dict[str, Any]] = []
        for topic in dict.fromkeys(args.topics):
            try:
                drafts = await session.provider.search_filings(topic)
            except Exception as e:  # noqa: BLE001 - a failed search is reported, not fatal
                results.append(
                    {"topic": topic, "error": f"search failed: {type(e).__name__}"}
                )
                continue
            if drafts is None:
                return {
                    "note": "The vendor is not a registrant whose filings can be searched."
                }
            items = [session.evidence.add(d) for d in drafts]
            results.append(
                {
                    "topic": topic,
                    "filings": [
                        i.model_dump(include={"id", "title", "detail", "citation"})
                        for i in items
                    ],
                }
            )
        return {"results": results}

    def _scenario_run(run_id: str):
        if not session.ledger.has(run_id):
            raise ToolError(f"unknown run id {run_id}")
        if session.ledger.meta(run_id).kind != "scenario":
            raise ToolError(
                f"{run_id} is a control what-if; use the scenario run it was based on"
            )
        return session.ledger.get(run_id)

    async def sensitivity_analysis(args: RunIdsArgs) -> dict:
        tol = ctx.tolerance()
        results = []
        for run_id in dict.fromkeys(args.run_ids):
            base = _scenario_run(run_id)
            results.append(
                {
                    "run_id": run_id,
                    "drivers": [
                        {
                            "input": d.path,
                            "expected_loss_at_min": format_usd(d.low_ale),
                            "expected_loss_at_max": format_usd(d.high_ale),
                            "share_of_swing": format_percent(d.share),
                        }
                        for d in drivers(base)[:5]
                    ],
                }
            )
        return {
            "results": results,
            **({"tolerance": format_usd(tol.value_usd)} if tol else {}),
        }

    async def test_control(args: ControlArgs) -> dict:
        tol = ctx.tolerance()
        results, errors = [], []
        for i, t in enumerate(args.tests):
            try:
                base = _scenario_run(t.run_id)
                changed = apply_control(t.control_id, base.inputs, t.parameter)
            except (ToolError, KeyError, ValueError) as e:
                errors.append(f"tests[{i}]: {e}")
                continue
            run = session.ledger.run(
                changed,
                RunMeta(
                    kind="control",
                    base_run_id=base.run_id,
                    control_id=t.control_id,
                    parameter=t.parameter,
                ),
            )
            session.agent_control_runs.add(run.run_id)
            before, after = base.distribution, run.distribution
            row = {
                "run_id": run.run_id,
                "control_id": t.control_id,
                "base_run_id": base.run_id,
                "expected_annual_loss": f"{format_usd(before.ale_mean)} -> {format_usd(after.ale_mean)}",
                "p99": f"{format_usd(before.p99)} -> {format_usd(after.p99)}",
            }
            if tol:
                row["chance_reaches_tolerance"] = (
                    f"{format_percent(before.prob_exceeds(tol.value_usd))} -> "
                    f"{format_percent(after.prob_exceeds(tol.value_usd))}"
                )
            results.append(row)
        if not results:
            raise ToolError("; ".join(errors))
        return {"results": results, **({"errors": errors} if errors else {})}

    async def submit_report(args: SubmitArgs) -> dict:
        # The loop checks and accepts the submission (it needs the final runs)
        return {"received": True}

    tools = [
        Tool(
            "get_agent_findings",
            "Findings (scores, red flags, evidence) about the vendor for one or more "
            "domains, as evidence items with ids you can cite as a range's source.",
            DomainsArgs,
            get_agent_findings,
        ),
        Tool(
            "get_financials",
            "The vendor's financial statement and its Altman Z-score with the annual "
            "default probability, as citable evidence.",
            AltmanArgs,
            get_financials,
        ),
        Tool(
            "lookup_benchmark",
            "Published benchmark figures (breach cost, breach frequency, default rates, fines) "
            "with their benchmark:<id> source.",
            TopicArgs,
            lookup_benchmark,
        ),
        Tool(
            "get_baseline_inputs",
            "Default FAIR inputs for one or more scenarios from benchmarks and the client "
            "profile, with guidance on each scenario; a starting point to refine.",
            ScenariosArgs,
            get_baseline_inputs,
        ),
        Tool(
            "run_simulation",
            "Run the Monte Carlo for one or more scenarios' FAIR inputs. Returns a run_id "
            "and the loss figures for each. Every range needs a source (client, "
            "assumption, ev_<id>, benchmark:<id> or derived:<id>; several ids may be "
            "comma-separated) and a rationale.",
            RunArgs,
            run_simulation,
        ),
        Tool(
            "search_sec_filings",
            "Search the vendor's SEC filings for a disclosed cyber incident (8-K Item "
            "1.05), going-concern doubt, or a privacy consent order. Hits become citable "
            "evidence; no hits is evidence too.",
            FilingArgs,
            search_sec_filings,
        ),
        Tool(
            "sensitivity_analysis",
            "Which inputs drive the expected loss of one or more scenario runs: each "
            "input pinned at its min and max, ranked by the swing.",
            RunIdsArgs,
            sensitivity_analysis,
        ),
        Tool(
            "test_control",
            "What-if: apply one or more controls (from the brief) to scenario runs and "
            "see the expected loss, P99 and chance of reaching the tolerance before and after.",
            ControlArgs,
            test_control,
        ),
        Tool(
            "submit_report",
            "Finish: name the final run per scenario and write the narrative. Checked "
            "before it is accepted.",
            SubmitArgs,
            submit_report,
        ),
    ]

    if session.ask_user is not None:
        ask = session.ask_user

        async def ask_user(args: AskArgs) -> dict:
            answer = (await ask(args.question)).strip()
            if not answer:
                return {
                    "answer": None,
                    "note": "No answer; carry on with an assumption.",
                }
            ev = session.evidence.add(
                EvidenceDraft(
                    kind="client", title=args.question, detail=answer, citation="Client"
                )
            )
            return {"answer": answer, "evidence_id": ev.id}

        tools.append(
            Tool(
                "ask_user",
                "Ask the client one question when a missing fact would change the result a "
                "lot (e.g. records shared, risk tolerance). Use sparingly.",
                AskArgs,
                ask_user,
            )
        )
    return {t.name: t for t in tools}


def slim_schema(node: Any, field_map: bool = False) -> Any:
    """
    Remove what the model does not need: "title" keys, class docstrings
    (repeated for every Range) and empty defaults. Field descriptions stay.
    A key inside "properties" is a field name, so a field called title is kept.
    """
    if isinstance(node, dict):
        is_class = "properties" in node and not field_map
        out = {}
        for k, v in node.items():
            if not field_map:
                if k == "title" or (k == "description" and is_class):
                    continue
                if k == "default" and v in ("", None, [], {}):
                    continue
            out[k] = slim_schema(v, field_map=(k == "properties" and not field_map))
        return out
    if isinstance(node, list):
        return [slim_schema(v) for v in node]
    return node


def inline_refs(schema: dict) -> dict:
    """Replace $ref pointers with their definitions; some models ignore $defs."""
    defs = schema.get("$defs", {})

    def resolve(node: Any, seen: tuple[str, ...]) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                name = ref.split("/")[-1]
                if name in seen:
                    raise ValueError(f"recursive schema at {name}")
                merged = {
                    **copy.deepcopy(defs[name]),
                    **{k: v for k, v in node.items() if k != "$ref"},
                }
                return resolve(merged, (*seen, name))
            return {k: resolve(v, seen) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(v, seen) for v in node]
        return node

    return resolve(schema, ())
