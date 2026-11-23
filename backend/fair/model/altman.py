"""
Altman Z-score: a vendor's financial distress from its balance sheet.

Two published variants:

  Z   (Altman 1968, public manufacturers)
      1.2*A + 1.4*B + 3.3*C + 0.6*D + 1.0*E
      zones: > 2.99 safe, 1.81 - 2.99 grey, < 1.81 distress

  Z'' (Altman 1995, non-manufacturers and private firms)
      6.56*A + 3.26*B + 6.72*C + 1.05*D''
      zones: > 2.60 safe, 1.10 - 2.60 grey, < 1.10 distress

  A = working capital / total assets       B = retained earnings / total assets
  C = EBIT / total assets                  D = market value of equity / total liabilities
  E = sales / total assets                 D'' = book equity / total liabilities

The zone becomes the insolvency scenario's annual default probability, centred
on S&P long-run default rates for the rating band the zone roughly corresponds
to (safe ~ BBB, grey ~ speculative grade, distress ~ CCC/C). Treating a zone
as a rating band is an approximation, and the ranges are wide to reflect it;
the rationale says so wherever the figure is used.
"""

import math
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from fair.model.taxonomy import ProbabilityRange


class AltmanModel(StrEnum):
    ORIGINAL = "z"
    NON_MANUFACTURING = "z_double_prime"


class Zone(StrEnum):
    SAFE = "safe"
    GREY = "grey"
    DISTRESS = "distress"


# (distress below, safe above)
ZONE_CUTOFFS: dict[AltmanModel, tuple[float, float]] = {
    AltmanModel.ORIGINAL: (1.81, 2.99),
    AltmanModel.NON_MANUFACTURING: (1.10, 2.60),
}

# Annual default probability per zone: (min, mode, max, benchmark id). The
# mode is the cited long-run rate; min and max widen it for the zone-to-band
# approximation.
ZONE_DEFAULT_PROBABILITY: dict[Zone, tuple[float, float, float, str]] = {
    Zone.SAFE: (0.0005, 0.0022, 0.01, "sp_default.bbb_us_longrun"),
    Zone.GREY: (0.01, 0.041, 0.10, "sp_default.spec_grade_us_longrun"),
    Zone.DISTRESS: (0.10, 0.283, 0.50, "sp_default.ccc_c_global_longrun"),
}

_REQUIRED: dict[AltmanModel, tuple[str, ...]] = {
    AltmanModel.ORIGINAL: (
        "working_capital",
        "retained_earnings",
        "ebit",
        "total_assets",
        "total_liabilities",
        "market_value_equity",
        "sales",
    ),
    AltmanModel.NON_MANUFACTURING: (
        "working_capital",
        "retained_earnings",
        "ebit",
        "total_assets",
        "total_liabilities",
        "book_equity",
    ),
}


class FinancialStatement(BaseModel):
    """Balance-sheet and income figures in one currency. Any may be missing."""

    model_config = ConfigDict(extra="forbid")

    working_capital: float | None = None
    retained_earnings: float | None = None
    ebit: float | None = None
    total_assets: float | None = None
    total_liabilities: float | None = None
    sales: float | None = None
    market_value_equity: float | None = None
    book_equity: float | None = None


class AltmanResult(BaseModel):
    status: Literal["ok", "insufficient_data"]
    model: AltmanModel
    z: float | None = None
    zone: Zone | None = None
    components: dict[str, float] = {}
    missing: list[str] = []
    annual_default_probability: ProbabilityRange | None = None


def altman_z(
    statement: FinancialStatement, *, manufacturer: bool = False
) -> AltmanResult:
    model = AltmanModel.ORIGINAL if manufacturer else AltmanModel.NON_MANUFACTURING
    values = statement.model_dump()

    missing = [
        f for f in _REQUIRED[model] if values[f] is None or not math.isfinite(values[f])
    ]
    for f in ("total_assets", "total_liabilities"):
        if f not in missing and values[f] <= 0:
            missing.append(f"{f} (must be > 0)")
    if missing:
        return AltmanResult(status="insufficient_data", model=model, missing=missing)

    ta, tl = values["total_assets"], values["total_liabilities"]
    a = values["working_capital"] / ta
    b = values["retained_earnings"] / ta
    c = values["ebit"] / ta
    if model is AltmanModel.ORIGINAL:
        d = values["market_value_equity"] / tl
        e = values["sales"] / ta
        components = {"A": a, "B": b, "C": c, "D": d, "E": e}
        z = 1.2 * a + 1.4 * b + 3.3 * c + 0.6 * d + 1.0 * e
    else:
        d = values["book_equity"] / tl
        components = {"A": a, "B": b, "C": c, "D": d}
        z = 6.56 * a + 3.26 * b + 6.72 * c + 1.05 * d

    zone = _zone(model, z)
    lo, mode, hi, benchmark_id = ZONE_DEFAULT_PROBABILITY[zone]
    pd = ProbabilityRange(
        min=lo,
        mode=mode,
        max=hi,
        source=f"benchmark:{benchmark_id}",
        rationale=(
            f"Altman {model} = {z:.2f} ({zone} zone), centred on the S&P long-run "
            "default rate of the rating band this zone approximates; treating a "
            "zone as a rating band is an approximation."
        ),
    )
    return AltmanResult(
        status="ok",
        model=model,
        z=z,
        zone=zone,
        components=components,
        annual_default_probability=pd,
    )


def _zone(model: AltmanModel, z: float) -> Zone:
    distress_below, safe_above = ZONE_CUTOFFS[model]
    if z > safe_above:
        return Zone.SAFE
    if z < distress_below:
        return Zone.DISTRESS
    return Zone.GREY
