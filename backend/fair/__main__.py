"""
Command line for the FAIR engine.

    python -m fair simulate FILE [--iterations N] [--seed-key KEY] [--tolerance USD] [--json]
    python -m fair altman FILE [--manufacturer] [--json]

simulate: FILE holds one scenario, or {"scenarios": [...]}. Several scenarios
are also reported combined. The seed comes from --seed-key (default: the file
name) and each scenario's position, so a re-run gives the same numbers.

altman: FILE holds a FinancialStatement.
"""

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from fair.format import format_percent, format_usd
from fair.model import (
    DEFAULT_ITERATIONS,
    TEMPLATES,
    FinancialStatement,
    LossDistribution,
    ScenarioInputs,
    SimulationResult,
    altman_z,
    combine,
    derive_seed,
    simulate,
)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        data = json.loads(Path(args.file).read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: cannot read {args.file}: {e}", file=sys.stderr)
        return 2
    try:
        if args.command == "simulate":
            return _simulate(args, data)
        return _altman(args, data)
    except ValidationError as e:
        print(f"error: invalid input in {args.file}:\n{e}", file=sys.stderr)
        return 2


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m fair", description="FAIR risk quantification"
    )
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("simulate", help="Monte Carlo annual loss for FAIR scenarios")
    s.add_argument("file")
    s.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    s.add_argument(
        "--seed-key", help="text the seed is derived from (default: file name)"
    )
    s.add_argument("--tolerance", type=float, help="annual loss tolerance in USD")
    s.add_argument(
        "--json", action="store_true", help="print JSON instead of a summary"
    )

    a = sub.add_parser("altman", help="Altman Z-score from a financial statement")
    a.add_argument("file")
    a.add_argument(
        "--manufacturer", action="store_true", help="use the original Z model"
    )
    a.add_argument("--json", action="store_true")
    return p


def _simulate(args: argparse.Namespace, data: dict) -> int:
    raw = (
        data["scenarios"] if isinstance(data, dict) and "scenarios" in data else [data]
    )
    scenarios = [ScenarioInputs.model_validate(item) for item in raw]
    seed_key = args.seed_key or Path(args.file).name
    results = [
        simulate(inputs, seed=derive_seed(seed_key, str(i)), iterations=args.iterations)
        for i, inputs in enumerate(scenarios)
    ]
    total = combine(results) if len(results) > 1 else None

    if args.json:
        out: dict = {"results": [r.model_dump(mode="json") for r in results]}
        if total:
            out["combined"] = total.model_dump(mode="json")
        if args.tolerance is not None:
            out["tolerance_usd"] = args.tolerance
            out["prob_exceeds_tolerance"] = {
                r.run_id: r.distribution.prob_exceeds(args.tolerance) for r in results
            }
            if total:
                out["prob_exceeds_tolerance"]["combined"] = (
                    total.distribution.prob_exceeds(args.tolerance)
                )
        print(json.dumps(out, indent=2))
        return 0

    for r in results:
        _print_result(r, args.tolerance)
    if total:
        print(f"\nAll {len(results)} scenarios combined (assumed independent)")
        _print_distribution(total.distribution, args.tolerance)
    return 0


def _print_result(r: SimulationResult, tolerance: float | None) -> None:
    title = TEMPLATES[r.inputs.scenario].title
    label = f" - {r.inputs.label}" if r.inputs.label else ""
    print(f"\n{title}{label}   [{r.run_id}]")
    print(f"  Loss events / year (mean) : {r.loss_event_frequency_mean:.3f}")
    _print_distribution(r.distribution, tolerance)
    parts = sorted(r.breakdown.items(), key=lambda kv: -kv[1])
    print(
        "  Breakdown (mean $/yr)     : "
        + " · ".join(f"{path.split('.')[-1]} {format_usd(v)}" for path, v in parts)
    )
    n_inputs = len(list(r.inputs.ranges()))
    print(
        f"  Inputs                    : {n_inputs} ({r.inputs.assumption_count} assumptions)"
    )


def _print_distribution(d: LossDistribution, tolerance: float | None) -> None:
    print(f"  Expected annual loss      : {format_usd(d.ale_mean)}")
    print(
        "  P10 / P50 / P90 / P99     : "
        + " / ".join(format_usd(v) for v in (d.p10, d.p50, d.p90, d.p99))
    )
    print(f"  Chance of any loss        : {format_percent(d.prob_any_loss)}")
    if tolerance is not None:
        print(
            f"  Chance > {format_usd(tolerance):<17}: "
            f"{format_percent(d.prob_exceeds(tolerance))}"
        )


def _altman(args: argparse.Namespace, data: dict) -> int:
    result = altman_z(
        FinancialStatement.model_validate(data), manufacturer=args.manufacturer
    )
    if args.json:
        print(result.model_dump_json(indent=2))
        return 0
    if result.status != "ok":
        print(
            f"Altman {result.model}: insufficient data, missing {', '.join(result.missing)}"
        )
        return 0
    assert result.z is not None and result.annual_default_probability is not None
    pd = result.annual_default_probability
    print(f"Altman {result.model} = {result.z:.2f}  ({result.zone} zone)")
    print(
        "  Components: "
        + ", ".join(f"{k}={v:.3f}" for k, v in result.components.items())
    )
    print(
        f"  Annual default probability: {format_percent(pd.min)} - {format_percent(pd.max)} "
        f"(most likely {format_percent(pd.mode)}; uncalibrated placeholder)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
