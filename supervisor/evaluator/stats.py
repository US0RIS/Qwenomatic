"""Small statistics helpers (normal-normal shrinkage, ranks)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True)
class Posterior:
    mean: float
    sd: float
    n: int


def sample_variance(values: Sequence[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mu = sum(values) / n
    return sum((v - mu) ** 2 for v in values) / (n - 1)


def shrink(total: float, n: int, values: Sequence[float], *, prior_mean: float, prior_strength: float,
           variance_floor: float) -> Posterior:
    """Posterior for the mean per-step outcome, shrunk toward a prior.

    `total / n` is the observed mean. The prior acts like `prior_strength`
    pseudo-observations at `prior_mean`, so a handful of lucky steps cannot
    produce an extreme estimate.
    """
    k0 = max(prior_strength, 1e-9)
    mean = (k0 * prior_mean + total) / (k0 + n)
    var = max(sample_variance(values), variance_floor)
    return Posterior(mean=mean, sd=math.sqrt(var / (k0 + n)), n=n)


def ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return out


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else None


def mean(values: Iterable[float]) -> float | None:
    vals = list(values)
    return sum(vals) / len(vals) if vals else None
