"""
Sampling for three-point estimates.

FAIR practitioners use the Beta-PERT distribution: it needs only min / most
likely / max (what an analyst can actually estimate), stays inside min..max,
and puts most of its weight near the most likely value. Its mean is
(min + 4*mode + max) / 6.
"""

import numpy as np

from fair.model.taxonomy import Range

# The standard PERT shape parameter; higher means more weight near the mode.
PERT_LAMBDA = 4.0


def sample_pert(r: Range, n: int, rng: np.random.Generator) -> np.ndarray:
    """n samples from the Beta-PERT distribution for r."""
    spread = r.max - r.min
    if spread == 0:
        return np.full(n, r.min, dtype=float)
    alpha = 1 + PERT_LAMBDA * (r.mode - r.min) / spread
    beta = 1 + PERT_LAMBDA * (r.max - r.mode) / spread
    return r.min + rng.beta(alpha, beta, size=n) * spread


def pert_mean(r: Range) -> float:
    return (r.min + PERT_LAMBDA * r.mode + r.max) / (PERT_LAMBDA + 2)
