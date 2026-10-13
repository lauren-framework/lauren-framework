"""The two statistics this benchmark reports, and why it reports these two.

**The median, not the mean.**  A single unlucky repeat — a scheduler hiccup, a
GC pause, another process waking up — shifts a mean and leaves a median alone.
Ten thousand requests do not make the mean trustworthy here either: the
distribution of request latencies has a long right tail by construction, so the
mean reports the tail rather than the typical request.

**The spread next to it, always.**  A throughput number without a spread invites
the reader to compare two frameworks whose difference is smaller than the noise
between two runs of the *same* framework.  Every reported statistic therefore
carries ``min``, ``max``, ``stdev`` and ``spread_pct``
(``(max - min) / median * 100``) across the repeats, so "lauren is faster than
X" can be checked against "by more than the spread?" before it is believed.

Both live in one importable module because both sides of the harness need them:
the child process computes percentiles over the requests of a single repeat, and
the parent summarises those repeats into the published statistic.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = ["Stat", "percentile"]


def percentile(values: Sequence[float], q: float) -> float:
    """Return the ``q``-th percentile (0-100) of ``values`` by interpolation.

    Interpolating rather than picking the nearest sample matters because the
    tails are the interesting part: p99 over a few thousand requests should not
    jump by a whole request just because the sample count moved slightly between
    repeats.

    Raises:
        ValueError: ``values`` is empty.
    """
    if not values:
        raise ValueError("percentile() of an empty sequence")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * (q / 100.0)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[int(position)]
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


@dataclass(frozen=True)
class Stat:
    """One statistic across the repeats: the median, and how much it moved."""

    median: float
    """The headline number."""

    minimum: float
    maximum: float
    spread_pct: float
    """``(max - min) / median * 100`` — the full range, relative to the median."""

    stdev: float
    """Sample standard deviation; ``0.0`` for a single repeat."""

    @classmethod
    def of(cls, values: Sequence[float]) -> Stat:
        """Summarise one value per repeat.

        Raises:
            ValueError: ``values`` is empty.
        """
        if not values:
            raise ValueError("cannot summarise an empty sequence")
        median = statistics.median(values)
        spread = 0.0 if median == 0 else (max(values) - min(values)) / median * 100.0
        return cls(
            median=median,
            minimum=min(values),
            maximum=max(values),
            spread_pct=spread,
            stdev=statistics.stdev(values) if len(values) > 1 else 0.0,
        )

    def as_dict(self, digits: int = 6) -> dict[str, float]:
        """Render for JSON, rounded to ``digits`` decimals."""
        return {
            "median": round(self.median, digits),
            "min": round(self.minimum, digits),
            "max": round(self.maximum, digits),
            "spread_pct": round(self.spread_pct, 2),
            "stdev": round(self.stdev, digits),
        }
