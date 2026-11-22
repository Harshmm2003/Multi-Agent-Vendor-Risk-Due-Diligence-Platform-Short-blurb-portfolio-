"""
The CLI end to end, the dollar formatter, and the package boundary: fair/ is
standalone and must never import the VendorIQ application (app/).
"""

import ast
import json
from pathlib import Path

import pytest

from fair.__main__ import main
from fair.format import format_usd

FAIR_DIR = Path(__file__).resolve().parents[2] / "fair"
EXAMPLE = FAIR_DIR / "examples" / "vendor_breach_healthcare.json"
FINANCIALS = FAIR_DIR / "examples" / "vendor_financials.json"


def test_fair_never_imports_app():
    offenders = []
    for path in FAIR_DIR.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            offenders += [
                f"{path.name}: {n}" for n in names if n == "app" or n.startswith("app.")
            ]
    assert offenders == []


@pytest.mark.parametrize(
    "value, text",
    [
        (0, "$0"),
        (950, "$950"),
        (999.4, "$999"),
        (999.6, "$1K"),
        (12_345, "$12.3K"),
        (410_000, "$410K"),
        (999_400, "$999K"),
        (999_600, "$1M"),
        (1_250_000, "$1.25M"),
        (2_400_000, "$2.4M"),
        (3_000_000_000, "$3B"),
        (-50_000, "-$50K"),
    ],
)
def test_format_usd(value, text):
    assert format_usd(value) == text


def test_cli_simulate_summary(capsys):
    assert (
        main(
            ["simulate", str(EXAMPLE), "--iterations", "2000", "--tolerance", "1000000"]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "Vendor data breach" in out and "Vendor insolvency" in out
    assert "All 2 scenarios combined" in out
    assert "P10 / P50 / P90 / P99" in out
    assert "Chance > $1M" in out


def test_cli_simulate_is_reproducible_json(capsys):
    args = [
        "simulate",
        str(EXAMPLE),
        "--iterations",
        "2000",
        "--json",
        "--tolerance",
        "1e6",
    ]
    main(args)
    first = json.loads(capsys.readouterr().out)
    main(args)
    second = json.loads(capsys.readouterr().out)
    assert first == second
    assert set(first["prob_exceeds_tolerance"]) == {
        *(r["run_id"] for r in first["results"]),
        "combined",
    }


def test_cli_altman(capsys):
    assert main(["altman", str(FINANCIALS)]) == 0
    out = capsys.readouterr().out
    assert "z_double_prime" in out and "safe zone" in out


def test_cli_rejects_invalid_scenario(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"scenario": "vendor_breach"}))
    assert main(["simulate", str(bad)]) == 2
    assert "invalid input" in capsys.readouterr().err


def test_cli_missing_file(capsys):
    assert main(["simulate", "/nonexistent.json"]) == 2
    assert "cannot read" in capsys.readouterr().err
