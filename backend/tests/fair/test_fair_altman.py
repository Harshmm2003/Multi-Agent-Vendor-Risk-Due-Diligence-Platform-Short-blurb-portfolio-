"""Altman Z-score: hand-computed values, zone boundaries and missing data."""

import pytest

from fair.model import FinancialStatement, Zone, altman_z
from fair.model.altman import AltmanModel

# Ratios chosen so the arithmetic is easy to check by hand:
# A = 0.10, B = 0.20, C = 0.05, D (market) = 1.00, D'' (book) = 0.50, E = 0.80
STATEMENT = FinancialStatement(
    working_capital=100,
    retained_earnings=200,
    ebit=50,
    total_assets=1000,
    total_liabilities=600,
    sales=800,
    market_value_equity=600,
    book_equity=300,
)


def test_original_z_by_hand():
    # 1.2(0.10) + 1.4(0.20) + 3.3(0.05) + 0.6(1.00) + 1.0(0.80)
    # = 0.12 + 0.28 + 0.165 + 0.60 + 0.80 = 1.965  → grey (1.81..2.99)
    r = altman_z(STATEMENT, manufacturer=True)
    assert r.status == "ok" and r.model is AltmanModel.ORIGINAL
    assert r.z == pytest.approx(1.965)
    assert r.zone is Zone.GREY
    assert r.components == pytest.approx(
        {"A": 0.1, "B": 0.2, "C": 0.05, "D": 1.0, "E": 0.8}
    )


def test_z_double_prime_by_hand():
    # 6.56(0.10) + 3.26(0.20) + 6.72(0.05) + 1.05(0.50)
    # = 0.656 + 0.652 + 0.336 + 0.525 = 2.169  → grey (1.10..2.60)
    r = altman_z(STATEMENT)
    assert r.model is AltmanModel.NON_MANUFACTURING
    assert r.z == pytest.approx(2.169)
    assert r.zone is Zone.GREY


@pytest.mark.parametrize(
    "retained, zone",
    [(600, Zone.SAFE), (-300, Zone.DISTRESS)],
)
def test_zones(retained, zone):
    # B moves Z'' by 3.26 x ΔB: +0.4 → 3.47 (safe); -0.5 → 0.54 (distress)
    s = STATEMENT.model_copy(update={"retained_earnings": retained})
    assert altman_z(s).zone is zone


def test_default_probability_is_labelled_uncalibrated():
    r = altman_z(STATEMENT)
    pd = r.annual_default_probability
    assert pd is not None
    assert pd.source == "assumption"
    assert "uncalibrated" in pd.rationale
    assert 0 <= pd.min <= pd.mode <= pd.max <= 1


def test_riskier_zone_has_higher_default_probability():
    safe = altman_z(STATEMENT.model_copy(update={"retained_earnings": 600}))
    distress = altman_z(STATEMENT.model_copy(update={"retained_earnings": -300}))
    assert safe.annual_default_probability and distress.annual_default_probability
    assert (
        safe.annual_default_probability.mode < distress.annual_default_probability.mode
    )


def test_missing_fields_reported_not_guessed():
    r = altman_z(FinancialStatement(working_capital=1, total_assets=10))
    assert r.status == "insufficient_data"
    assert r.z is None and r.annual_default_probability is None
    assert "retained_earnings" in r.missing and "book_equity" in r.missing


def test_non_positive_denominators_rejected():
    r = altman_z(STATEMENT.model_copy(update={"total_liabilities": 0}))
    assert r.status == "insufficient_data"
    assert "total_liabilities (must be > 0)" in r.missing


def test_manufacturer_needs_market_value():
    r = altman_z(
        STATEMENT.model_copy(update={"market_value_equity": None}), manufacturer=True
    )
    assert r.status == "insufficient_data"
    assert r.missing == ["market_value_equity"]
