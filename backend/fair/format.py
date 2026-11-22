"""
The one way dollar figures are written.

Every figure a person sees goes through format_usd, so the agent's guardrail
(fair/agent/guardrails.py) can check a figure in the narrative by comparing it
with format_usd of the simulated values.
"""

_UNITS = ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K"))


def format_usd(value: float) -> str:
    """$950, $12.3K, $410K, $1.25M — three significant figures above $1,000."""
    sign = "-" if value < 0 else ""
    amount = abs(value)
    for divisor, suffix in _UNITS:
        scaled = amount / divisor
        if scaled >= 0.9995:  # would round to at least 1.00 of this unit
            return f"{sign}${float(f'{scaled:.3g}'):g}{suffix}"
    return f"{sign}${amount:.0f}"


def format_percent(probability: float) -> str:
    return f"{probability * 100:.1f}%"
