"""
One analysis's working memory: the evidence the agent has seen, every
simulation it has run, and the trace of what it did.

Ids are handed out here (ev_1, run_01, ...) and nowhere else. Input ranges
cite evidence ids, the report cites run ids, and the guardrails check both
against these stores, so nothing the model writes can point at a fact or a
number that does not exist.
"""

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

from fair.context import AnalysisContext
from fair.model.simulation import SimulationResult, derive_seed, simulate
from fair.model.taxonomy import Scenario, ScenarioInputs
from fair.ports import AskUser, Evidence, EvidenceDraft, EvidenceProvider


class EvidenceStore:
    def __init__(self) -> None:
        self._items: dict[str, Evidence] = {}

    def add(self, draft: EvidenceDraft) -> Evidence:
        item = Evidence(id=f"ev_{len(self._items) + 1}", **draft.model_dump())
        self._items[item.id] = item
        return item

    def has(self, evidence_id: str) -> bool:
        return evidence_id in self._items

    def items(self) -> list[Evidence]:
        return list(self._items.values())


class RunMeta(BaseModel):
    """What a run is: the scenario as it stands, or a what-if with a control."""

    kind: Literal["scenario", "control"] = "scenario"
    base_run_id: str | None = None
    control_id: str | None = None
    parameter: float | None = None


class RunLedger:
    """Every simulation, by run id. The only place dollar figures come from."""

    def __init__(self, seed_base: str, iterations: int) -> None:
        self._runs: dict[str, SimulationResult] = {}
        self._meta: dict[str, RunMeta] = {}
        self._seed_base = seed_base
        self._iterations = iterations

    def run(
        self, inputs: ScenarioInputs, meta: RunMeta | None = None
    ) -> SimulationResult:
        # One seed per scenario (not per run): re-runs of a scenario with
        # changed inputs use the same random draws, so differences between
        # them come from the inputs, not from sampling noise.
        result = simulate(
            inputs,
            seed=derive_seed(self._seed_base, inputs.scenario),
            iterations=self._iterations,
            run_id=f"run_{len(self._runs) + 1:02d}",
        )
        self._runs[result.run_id] = result
        self._meta[result.run_id] = meta or RunMeta()
        return result

    def meta(self, run_id: str) -> RunMeta:
        return self._meta[run_id]

    def control_runs_of(self, base_run_id: str) -> list[SimulationResult]:
        return [
            self._runs[rid]
            for rid, m in self._meta.items()
            if m.kind == "control" and m.base_run_id == base_run_id
        ]

    def get(self, run_id: str) -> SimulationResult:
        return self._runs[run_id]

    def has(self, run_id: str) -> bool:
        return run_id in self._runs

    def results(self) -> list[SimulationResult]:
        return list(self._runs.values())

    def latest_by_scenario(self) -> dict[Scenario, SimulationResult]:
        latest: dict[Scenario, SimulationResult] = {}
        for r in self._runs.values():
            if self._meta[r.run_id].kind == "scenario":
                latest[r.inputs.scenario] = r
        return latest


StepKind = Literal[
    "thought", "tool_call", "tool_error", "guardrail", "fallback", "error"
]


class TraceStep(BaseModel):
    """One entry in the investigation timeline shown in the report."""

    step: int
    kind: StepKind
    tool: str | None = None
    summary: str
    detail: dict[str, Any] = {}


@dataclass
class Session:
    context: AnalysisContext
    provider: EvidenceProvider
    iterations: int
    ask_user: AskUser | None = None
    evidence: EvidenceStore = field(default_factory=EvidenceStore)
    ledger: RunLedger = field(init=False)
    trace: list[TraceStep] = field(default_factory=list)
    llm_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    paced_seconds: float = 0.0  # waited to stay under the tokens-per-minute limit
    agent_control_runs: set[str] = field(
        default_factory=set
    )  # controls the agent tested

    def __post_init__(self) -> None:
        self.ledger = RunLedger(self.context.seed_base, self.iterations)

    def record(
        self,
        kind: StepKind,
        summary: str,
        tool: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.trace.append(
            TraceStep(
                step=len(self.trace) + 1,
                kind=kind,
                tool=tool,
                summary=summary[:500],
                detail=detail or {},
            )
        )
