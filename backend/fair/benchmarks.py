"""
Cited benchmark figures (fair/data/benchmarks.json).

An input range may cite one as `benchmark:<id>`. The agent's run_simulation
tool refuses a benchmark id that is not in this file, so a citation always
points at a real, dated source.
"""

import json
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_FILE = Path(__file__).parent / "data" / "benchmarks.json"


class Benchmark(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[\w.-]+$")
    topics: list[str]
    value: float = Field(gt=0)
    unit: str
    description: str
    source: str
    url: str = Field(pattern=r"^https://")
    year: int = Field(ge=2000)
    # "loss_events": a frequency that already counts successful events, so a
    # range using it as threat_event_frequency needs vulnerability fixed at 1
    measures: Literal["loss_events"] | None = None

    @property
    def ref(self) -> str:
        """The value to put in a Range's source field."""
        return f"benchmark:{self.id}"


@cache
def all_benchmarks() -> dict[str, Benchmark]:
    raw = json.loads(_FILE.read_text())["benchmarks"]
    items = [Benchmark.model_validate(b) for b in raw]
    by_id = {b.id: b for b in items}
    if len(by_id) != len(items):
        raise ValueError("duplicate benchmark id in benchmarks.json")
    return by_id


def get(benchmark_id: str) -> Benchmark:
    return all_benchmarks()[benchmark_id]


def exists(source: str) -> bool:
    """True when source is benchmark:<id> for a known id."""
    return (
        source.startswith("benchmark:")
        and source[len("benchmark:") :] in all_benchmarks()
    )


def lookup(topic: str) -> list[Benchmark]:
    """Benchmarks tagged with topic, or whose id or description mentions it."""
    t = topic.lower().strip()
    return [
        b
        for b in all_benchmarks().values()
        if t in b.topics or t in b.id.lower() or t in b.description.lower()
    ]


def measures_loss_events(source: str) -> bool:
    return exists(source) and get(source[len("benchmark:") :]).measures == "loss_events"


def topics() -> list[str]:
    return sorted({t for b in all_benchmarks().values() for t in b.topics})
