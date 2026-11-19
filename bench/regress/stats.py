"""Noise-tolerant comparison of two versions' samples.

Samples come grouped by repetition (one server start each, versions interleaved round-robin).
The statistic is the median of all samples; its uncertainty comes from a two-level bootstrap:
draw the repetitions with replacement, then each drawn repetition's samples with replacement.
Resampling whole repetitions keeps what varies between server starts (placement, caches, a
noisy neighbour) in the interval, instead of pretending every sample is independent.

The ratio candidate/baseline gets a percentile interval from independent bootstrap draws of
both sides. A change is flagged only when the whole interval is past the threshold.
"""

import math
import random
import statistics


def pooled(samples_by_rep: list[list[float]]) -> list[float]:
    return [value for rep in samples_by_rep for value in rep]


def median(samples_by_rep: list[list[float]]) -> float:
    return statistics.median(pooled(samples_by_rep=samples_by_rep))


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile (p99 of 50 values is the largest)."""
    ordered = sorted(values)
    rank = max(1, min(len(ordered), math.ceil(round(fraction * len(ordered), 9))))
    return ordered[rank - 1]


def _draw(samples_by_rep: list[list[float]], rng: random.Random) -> float:
    reps = [samples_by_rep[rng.randrange(len(samples_by_rep))] for _ in samples_by_rep]
    values = []
    for rep in reps:
        values.extend(rep[rng.randrange(len(rep))] for _ in rep)
    return statistics.median(values)


def ratio_interval(candidate: list[list[float]], baseline: list[list[float]], iterations: int = 2000,
                   confidence: float = 0.95, seed: int = 6) -> tuple[float, float, float]:
    """(point, low, high) of median(candidate) / median(baseline)."""
    candidate = [rep for rep in candidate if rep]
    baseline = [rep for rep in baseline if rep]
    base_point = median(samples_by_rep=baseline)
    point = median(samples_by_rep=candidate) / base_point if base_point else float("inf")
    rng = random.Random(seed)
    ratios = []
    for _ in range(iterations):
        b = _draw(samples_by_rep=baseline, rng=rng)
        ratios.append(_draw(samples_by_rep=candidate, rng=rng) / b if b else float("inf"))
    ratios.sort()
    tail = (1 - confidence) / 2
    low = ratios[int(tail * (iterations - 1))]
    high = ratios[int((1 - tail) * (iterations - 1))]
    return point, low, high


def judge(point: float, low: float, high: float, threshold: float, higher_is_better: bool,
          absolute_change: float | None = None, minimum_change: float = 0.0) -> str:
    """'regression', 'possible regression', 'improvement' or 'ok'.

    regression: the whole interval is worse than the threshold (and the absolute change is at
    least minimum_change, so a 0.3 ms statement taking 0.4 ms is not a finding). possible
    regression: the point estimate is past the threshold and the whole interval is worse than no
    change, but not all of it past the threshold. improvement: the whole interval is better than
    the threshold."""
    if higher_is_better:  # a throughput: express it as "times slower"
        point, low, high = 1 / point if point else float("inf"), 1 / high if high else float("inf"), \
            1 / low if low else float("inf")
    big_enough = absolute_change is None or abs(absolute_change) >= minimum_change
    if low > 1 + threshold and big_enough:
        return "regression"
    if point > 1 + threshold and low > 1 and big_enough:
        return "possible regression"
    if high < 1 / (1 + threshold) and big_enough:
        return "improvement"
    return "ok"
