"""Statistics for the harness: descriptives, intervals, effect sizes, agreement, reliability.

Pure functions, standard library only. When a statistic is undefined for the data it was given
(too few values, zero variance, an empty arm) the function returns None: never NaN, never an
exception, so a sparse run still aggregates and reports "n/a". Each docstring says when. Arguments
that no data could make valid (a level outside (0, 1), an unknown metric, paired sequences of
different lengths, a non-integer seed) raise ValueError instead, and a non-numeric value raises
TypeError: those are caller bugs.

Missing data: None, NaN and +/-inf in the inputs are treated as missing and dropped (none of them
can be written to JSON, and any of them would poison the statistic); `n` counts what was used.
A result that would not fit in a float (only with inputs near the float limit) is None too.

Determinism: committed summaries are recomputed and byte-compared in CI, on other operating
systems and newer Pythons, so results must not depend on either:
- Means, variances and correlations use exact integer/rational arithmetic and round once at the
  end (every finite float is an integer over a power of two), never the builtin sum() of floats,
  whose algorithm changed in Python 3.12. As a bonus, constant data has exactly zero variance.
- Resampling draws only from random.Random(seed).random(), the one sequence Python guarantees to
  stay the same across versions, mapped to an index as int(random() * n).
- Percentile intervals use one fixed rule: linear interpolation between order statistics.
"""

import bisect
import math
import random
from fractions import Fraction
from functools import lru_cache
from statistics import NormalDist

_NORMAL = NormalDist()


# --- input handling and exact arithmetic ----------------------------------------------------

def _number(x):
    """`x` as an int or float, or None if it is missing (None, NaN, +/-inf). bool counts as 0/1."""
    if x is None:
        return None
    if isinstance(x, bool):
        return int(x)
    if isinstance(x, int):
        return x
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    raise TypeError(f"expected a number or None, got {x!r}")


def _values(xs):
    """The non-missing numbers in `xs`, in order."""
    return [v for v in map(_number, xs) if v is not None]


def _pairs(xs, ys):
    """Complete (x, y) pairs of two equally long sequences; pairs with a missing side drop out."""
    xs, ys = list(xs), list(ys)
    if len(xs) != len(ys):
        raise ValueError(f"paired data must have equal lengths, got {len(xs)} and {len(ys)}")
    out_x, out_y = [], []
    for x, y in zip(xs, ys):
        x, y = _number(x), _number(y)
        if x is not None and y is not None:
            out_x.append(x)
            out_y.append(y)
    return out_x, out_y


def _dyadic(values):
    """Integers z and a shift k with values[i] == z[i] / 2**k exactly, for one common k."""
    ratios = [v.as_integer_ratio() for v in values]
    shifts = [den.bit_length() - 1 for _, den in ratios]  # every denominator is a power of two
    k = max(shifts, default=0)
    return [num << (k - s) for (num, _), s in zip(ratios, shifts)], k


def _moments(values):
    """(n, exact mean, exact sum of squared deviations) of a non-empty list, as Fractions."""
    z, k = _dyadic(values)
    n, s1, s2 = len(z), sum(z), sum(x * x for x in z)
    return n, Fraction(s1, n << k), Fraction(n * s2 - s1 * s1, n << (2 * k))


def _sqrt(exact):
    """Square root of an exact non-negative Fraction, rounded; None if beyond float range."""
    try:
        return math.sqrt(exact)
    except OverflowError:
        return None


def _standardized(diff, var):
    """diff / sqrt(var) for exact diff and var > 0, from the exact square; None on overflow."""
    if diff == 0:
        return 0.0
    size = _sqrt(diff * diff / var)
    if size is None:
        return None
    return size if diff > 0 else -size


def mean(xs):
    """Arithmetic mean, computed exactly and rounded once. None if there are no values."""
    v = _values(xs)
    if not v:
        return None
    z, k = _dyadic(v)
    return sum(z) / (len(z) << k)  # int / int is correctly rounded


def _median(v):
    s = sorted(v)
    mid = len(s) // 2
    if len(s) % 2:
        return s[mid]
    return float((Fraction(s[mid - 1]) + Fraction(s[mid])) / 2)


def _check_level(level):
    if isinstance(level, bool) or not isinstance(level, (int, float)) or not 0 < level < 1:
        raise ValueError(f"level must be strictly between 0 and 1, got {level!r}")


# --- Student t ------------------------------------------------------------------------------

def _betacf(a, b, x):
    """Continued fraction for the incomplete beta (modified Lentz, Numerical Recipes 6.4)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 10001):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            break
    return h


def _betainc(a, b, x, y):
    """Regularized incomplete beta I_x(a, b), a, b > 0, with y = 1 - x passed in separately so
    precision survives near x = 1."""
    if x <= 0.0:
        return 0.0
    if y <= 0.0:
        return 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                     + a * math.log(x) + b * math.log(y))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, y) / b


def _t_tail(t, df):
    """P(T > t) for t >= 0: I_x(df/2, 1/2) / 2 with x = df / (df + t^2)."""
    tt = t * t
    if tt == math.inf:
        return 0.0
    s = df + tt
    return 0.5 * _betainc(df / 2.0, 0.5, df / s, tt / s)


# From here up the Cornish-Fisher series (4 terms) is exact to double precision, while the
# lgamma() differences inside the incomplete beta start to lose digits (~1e-11 at 1e4).
_CORNISH_FISHER_DF = 1e4


@lru_cache(maxsize=4096)
def _t_upper(q, df):
    """The t > 0 with P(T > t) = q, for 0 < q < 0.5; None if it is beyond float range."""
    z = -_NORMAL.inv_cdf(q)
    if df >= _CORNISH_FISHER_DF:  # Abramowitz & Stegun 26.7.5
        z2 = z * z
        g1 = (z2 + 1) * z / 4
        g2 = ((5 * z2 + 16) * z2 + 3) * z / 96
        g3 = (((3 * z2 + 19) * z2 + 17) * z2 - 15) * z / 384
        g4 = ((((79 * z2 + 776) * z2 + 1482) * z2 - 1920) * z2 - 945) * z / 92160
        return z + (g1 + (g2 + (g3 + g4 / df) / df) / df) / df
    # The upper tail is decreasing and convex on t > 0, and the normal quantile never exceeds
    # the t quantile, so Newton's method from z climbs monotonically onto the root.
    log_norm = (math.lgamma((df + 1) / 2.0) - math.lgamma(df / 2.0)
                - 0.5 * math.log(df * math.pi))
    t = z
    for _ in range(1000):
        density = math.exp(log_norm - (df + 1) / 2.0 * math.log1p(t * t / df))
        if not density > 0.0:
            return None
        step = (_t_tail(t, df) - q) / density
        t += step
        if not math.isfinite(t):
            return None
        if abs(step) <= 1e-13 * t:
            break
    return t


def t_ppf(p, df):
    """Quantile of Student's t: the t with P(T <= t) = p, for 0 < p < 1 and df > 0 (real).

    The CDF comes from the regularized incomplete beta function (continued fraction), inverted
    with Newton's method; very large df use the Cornish-Fisher expansion. Matches printed tables,
    e.g. t_ppf(0.975, 10) = 2.2281. None outside that domain, where the quantile is undefined.
    """
    for arg in (p, df):
        if isinstance(arg, bool) or not isinstance(arg, (int, float)):
            return None
    if not (0 < p < 1 and df > 0):  # also rejects NaN
        return None
    if p == 0.5:
        return 0.0
    try:
        df = float(df)
    except OverflowError:  # an int beyond float range: the normal limit
        df = math.inf
    try:
        upper = _t_upper(float(min(p, 1.0 - p)), df)  # 1 - p is exact for p >= 0.5
    except ValueError:  # df so close to 0 that lgamma(df / 2) leaves its domain
        return None
    if upper is None:
        return None
    return upper if p > 0.5 else -upper


def _t_interval(m, sd, n, level):
    t = t_ppf(0.5 + level / 2, n - 1)
    if t is None or sd is None:
        return None
    half = t * sd / math.sqrt(n)
    lo, hi = m - half, m + half
    return (lo, hi) if math.isfinite(lo) and math.isfinite(hi) else None


# --- descriptives and intervals -------------------------------------------------------------

def describe(xs):
    """n, mean, median, sd (sample, ddof=1), min, max and ci95 (Student-t interval for the mean,
    as [lo, hi]) of the non-missing values.

    mean/median/min/max are None when n == 0; sd and ci95 are None when n < 2 (no spread can be
    estimated from one value). Zero spread gives sd 0.0 and ci95 [mean, mean].
    """
    v = _values(xs)
    n = len(v)
    out = {"n": n, "mean": None, "median": None, "sd": None, "min": None, "max": None,
           "ci95": None}
    if not n:
        return out
    _, exact_mean, ss = _moments(v)
    m = float(exact_mean)
    out.update(mean=m, median=_median(v), min=min(v), max=max(v))
    if n >= 2:
        sd = _sqrt(ss / (n - 1))
        out["sd"] = sd
        interval = _t_interval(m, sd, n, 0.95)
        out["ci95"] = list(interval) if interval else None
    return out


def mean_ci(xs, level=0.95):
    """Student-t confidence interval (lo, hi) for the mean. None when n < 2."""
    _check_level(level)
    v = _values(xs)
    if len(v) < 2:
        return None
    n, exact_mean, ss = _moments(v)
    return _t_interval(float(exact_mean), _sqrt(ss / (n - 1)), n, level)


def wilson_ci(k, n, level=0.95):
    """Wilson score interval (lo, hi) for a proportion of k successes in n trials.

    None when n == 0 (no trials, no proportion). Exactly 0.0 / 1.0 at the bounds k = 0 / k = n.
    """
    _check_level(level)
    for name, value in (("k", k), ("n", n)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer count, got {value!r}")
    if not 0 <= k <= n:
        raise ValueError(f"need 0 <= k <= n, got k={k}, n={n}")
    if n == 0:
        return None
    z = _NORMAL.inv_cdf(0.5 + level / 2)
    z2 = z * z
    p = k / n
    denom = 1.0 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z2 / (4 * n * n)) / denom
    lo = 0.0 if k == 0 else max(0.0, center - half)
    hi = 1.0 if k == n else min(1.0, center + half)
    return (lo, hi)


def ceiling_fraction(xs, max_value):
    """Fraction of the non-missing values at (or above) `max_value`. None if there are none."""
    v = _values(xs)
    if not v:
        return None
    return sum(1 for x in v if x >= max_value) / len(v)


# --- effect sizes ---------------------------------------------------------------------------

def _cohens(a, b):
    a, b = _values(a), _values(b)
    if len(a) < 2 or len(b) < 2:
        return None, len(a), len(b)
    n1, m1, ss1 = _moments(a)
    n2, m2, ss2 = _moments(b)
    pooled_var = (ss1 + ss2) / (n1 + n2 - 2)
    if pooled_var == 0:
        return None, n1, n2
    return _standardized(m1 - m2, pooled_var), n1, n2


def cohens_d(a, b):
    """(mean(a) - mean(b)) / pooled SD. None if either side has n < 2 or the pooled SD is 0
    (a standardized difference needs a spread to standardize by)."""
    return _cohens(a, b)[0]


def hedges_g(a, b):
    """Cohen's d times the small-sample correction J = 1 - 3 / (4 (n1 + n2) - 9).
    None exactly when cohens_d is None."""
    d, n1, n2 = _cohens(a, b)
    if d is None:
        return None
    return d * (1.0 - 3.0 / (4 * (n1 + n2) - 9))


def paired_dz(diffs):
    """Mean / SD of paired differences (Cohen's d_z). None if n < 2 or the SD is 0."""
    v = _values(diffs)
    if len(v) < 2:
        return None
    n, m, ss = _moments(v)
    if ss == 0:
        return None
    return _standardized(m, ss / (n - 1))


def _dominance(a, b):
    """(pairs with x > y, tied pairs) over all x in a, y in b, counted exactly."""
    ordered = sorted(b)
    greater = ties = 0
    for x in a:
        lo = bisect.bisect_left(ordered, x)
        hi = bisect.bisect_right(ordered, x)
        greater += lo
        ties += hi - lo
    return greater, ties


def cliffs_delta(a, b):
    """P(a > b) - P(a < b) over all pairs, in [-1, 1]. None if either side is empty."""
    a, b = _values(a), _values(b)
    if not a or not b:
        return None
    pairs = len(a) * len(b)
    greater, ties = _dominance(a, b)
    return (2 * greater + ties - pairs) / pairs


def stratified_hedges_g(groups):
    """Hedges' g of a treatment-control difference measured within clusters (e.g. prompts).

    `groups` maps cluster -> (treatment_values, control_values). g = (unweighted mean over clusters
    of mean(t) - mean(c)) / pooled within-cluster SD, times J = 1 - 3 / (4 df - 1) with
    df = sum(n_t + n_c - 2). Clusters missing an arm are skipped. Because the SD is pooled within
    clusters and the difference is taken within clusters, unequal cell sizes cannot let
    differences between clusters leak in (they can with a plain two-sample g). None if no cluster
    has both arms, df < 1, or the pooled within-cluster SD is 0.
    """
    diffs, ss, df = [], Fraction(0), 0
    for key in sorted(groups):
        t, c = (_values(v) for v in groups[key])
        if not t or not c:
            continue
        nt, mt, sst = _moments(t)
        nc, mc, ssc = _moments(c)
        diffs.append(mt - mc)
        ss += sst + ssc
        df += nt + nc - 2
    if not diffs or df < 1 or ss == 0:
        return None
    d = _standardized(sum(diffs, Fraction(0)) / len(diffs), ss / df)
    return None if d is None else d * (1.0 - 3.0 / (4 * df - 1))


def stratified_cliffs_delta(groups):
    """Cliff's delta over within-cluster pairs only: P(t > c) - P(t < c) across every
    (treatment, control) pair drawn from the same cluster. None if no cluster has both arms."""
    greater = ties = pairs = 0
    for key in sorted(groups):
        t, c = (_values(v) for v in groups[key])
        if not t or not c:
            continue
        g, tied = _dominance(t, c)
        greater, ties, pairs = greater + g, ties + tied, pairs + len(t) * len(c)
    if not pairs:
        return None
    return (2 * greater + ties - pairs) / pairs


def auc(pos, neg):
    """Mann-Whitney AUC: P(pos > neg) with ties counted 0.5. None if either side is empty."""
    pos, neg = _values(pos), _values(neg)
    if not pos or not neg:
        return None
    greater, ties = _dominance(pos, neg)
    return (2 * greater + ties) / (2 * len(pos) * len(neg))


# --- correlation ----------------------------------------------------------------------------

def _correlation(x, y):
    """Pearson r computed exactly and rounded once; None if either side is constant."""
    zx, _ = _dyadic(x)
    zy, _ = _dyadic(y)
    n, sx, sy = len(zx), sum(zx), sum(zy)
    cxx = n * sum(a * a for a in zx) - sx * sx
    cyy = n * sum(b * b for b in zy) - sy * sy
    if cxx == 0 or cyy == 0:
        return None
    cxy = n * sum(a * b for a, b in zip(zx, zy)) - sx * sy
    if cxy == 0:
        return 0.0
    r = math.sqrt(Fraction(cxy * cxy, cxx * cyy))  # <= 1 exactly, by Cauchy-Schwarz
    return r if cxy > 0 else -r


def _doubled_ranks(v):
    """Twice the 1-based average ranks (ties share their mean rank), as integers."""
    order = sorted(range(len(v)), key=v.__getitem__)
    ranks = [0] * len(v)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
            j += 1
        for position in order[i:j + 1]:
            ranks[position] = i + j + 2
        i = j + 1
    return ranks


def pearson(xs, ys):
    """Pearson correlation over complete pairs. None if n < 3 or either side has zero variance."""
    x, y = _pairs(xs, ys)
    if len(x) < 3:
        return None
    return _correlation(x, y)


def spearman(xs, ys):
    """Spearman rank correlation (Pearson on average ranks, so ties are handled) over complete
    pairs. None if n < 3 or either side is constant."""
    x, y = _pairs(xs, ys)
    if len(x) < 3:
        return None
    return _correlation(_doubled_ranks(x), _doubled_ranks(y))


# --- resampling -----------------------------------------------------------------------------

def _check_resampling(n_resamples, level, seed):
    _check_level(level)
    if isinstance(n_resamples, bool) or not isinstance(n_resamples, int) or n_resamples < 1:
        raise ValueError(f"n_resamples must be a positive integer, got {n_resamples!r}")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError(f"seed must be an integer so resampling is reproducible, got {seed!r}")


def _quantile(ordered, q):
    """Linear interpolation between order statistics (Hyndman-Fan type 7)."""
    h = (len(ordered) - 1) * q
    i = int(h)
    frac = h - i
    if frac == 0 or i + 1 >= len(ordered):
        return ordered[min(i, len(ordered) - 1)]
    return ordered[i] + (ordered[i + 1] - ordered[i]) * frac


def _percentile_interval(samples, level):
    samples.sort()
    lo, hi = _quantile(samples, 0.5 - level / 2), _quantile(samples, 0.5 + level / 2)
    return (lo, hi) if math.isfinite(lo) and math.isfinite(hi) else None


def bootstrap_ci(values, stat=mean, n_resamples=10000, level=0.95, seed=0):
    """Percentile bootstrap interval (lo, hi) of stat(values), resampling with replacement.

    None if fewer than 2 values (nothing to resample), or if `stat` is undefined (None or not
    finite) on any resample, since an interval over the remaining resamples would be biased.
    The same seed always gives the same interval.
    """
    _check_resampling(n_resamples, level, seed)
    v = _values(values)
    n = len(v)
    if n < 2:
        return None
    draw = random.Random(seed).random
    if stat is mean:  # same draws and the same exact mean, without re-converting every resample
        z, k = _dyadic(v)
        scale = n << k
        samples = [sum([z[int(draw() * n)] for _ in range(n)]) / scale
                   for _ in range(n_resamples)]
    else:
        samples = []
        for _ in range(n_resamples):
            value = stat([v[int(draw() * n)] for _ in range(n)])
            if value is None or (isinstance(value, float) and not math.isfinite(value)):
                return None
            samples.append(value)
    return _percentile_interval(samples, level)


def cluster_bootstrap_diff(groups, n_resamples=10000, level=0.95, seed=0):
    """Two-stage percentile bootstrap interval (lo, hi) for a treatment-control difference.

    `groups` maps cluster id (e.g. prompt id) -> (treatment_values, control_values). Each resample
    draws as many clusters as there are, with replacement; then, for each drawn cluster in draw
    order, resamples its treatment values and then its control values with replacement; and
    records the mean over drawn clusters of (mean(treatment) - mean(control)). Clusters are taken
    in sorted id order, so the result depends only on the data and the seed.

    None if there are fewer than 2 clusters (no between-cluster variation to resample) or any
    cluster has an arm with no values.
    """
    _check_resampling(n_resamples, level, seed)
    clusters = []
    for cluster_id in sorted(groups):
        treatment, control = groups[cluster_id]
        arms = []
        for values in (treatment, control):
            v = _values(values)
            if not v:
                return None
            z, k = _dyadic(v)
            arms.append((z, len(z), len(z) << k))
        clusters.append(arms)
    if len(clusters) < 2:
        return None
    draw = random.Random(seed).random
    count = len(clusters)
    diffs = []
    for _ in range(n_resamples):
        drawn = [clusters[int(draw() * count)] for _ in range(count)]
        parts = []
        for (zt, nt, st), (zc, nc, sc) in drawn:
            mt = sum([zt[int(draw() * nt)] for _ in range(nt)]) / st
            mc = sum([zc[int(draw() * nc)] for _ in range(nc)]) / sc
            parts.append(mt - mc)
        try:
            diffs.append(math.fsum(parts) / count)
        except (OverflowError, ValueError):  # differences beyond float range (inputs near 1e308)
            return None
    return _percentile_interval(diffs, level)


def bootstrap_diff_ci(treatment, control, n_resamples=10000, level=0.95, seed=0):
    """Percentile bootstrap interval (lo, hi) for mean(treatment) - mean(control), two
    independent samples (e.g. outcome attempts per arm), each resampled with replacement.

    None if either side has fewer than 2 values (no spread to resample). Each resample draws the
    treatment values first, then the control values, from random.Random(seed).
    """
    _check_resampling(n_resamples, level, seed)
    arms = []
    for values in (treatment, control):
        v = _values(values)
        if len(v) < 2:
            return None
        z, k = _dyadic(v)
        arms.append((z, len(z), len(z) << k))
    draw = random.Random(seed).random
    (zt, nt, st), (zc, nc, sc) = arms
    diffs = []
    for _ in range(n_resamples):
        mt = sum([zt[int(draw() * nt)] for _ in range(nt)]) / st
        mc = sum([zc[int(draw() * nc)] for _ in range(nc)]) / sc
        diffs.append(mt - mc)
    return _percentile_interval(diffs, level)


# --- reliability ----------------------------------------------------------------------------

def _pair_disagreement(values, metric):
    """Sum of delta^2 over ordered pairs of distinct positions (exact integers)."""
    m = len(values)
    if metric == "nominal":
        counts = {}
        for value in values:
            counts[value] = counts.get(value, 0) + 1
        return m * m - sum(c * c for c in counts.values())
    s1 = sum(values)
    return 2 * (m * sum(x * x for x in values) - s1 * s1)


def krippendorff_alpha(units, metric="interval"):
    """Krippendorff's alpha for `units`: one list of ratings per unit (one slot per coder),
    with None for a missing rating. metric "nominal" or "interval".

    Units with fewer than 2 ratings are not pairable and are ignored. Computed exactly from the
    coincidence-matrix definition, alpha = 1 - (n - 1) * sum_u D_u / (m_u - 1) / D, where D_u
    and D sum delta^2 over the ordered pairs within unit u and over all n pairable values.
    Reproduces Krippendorff (2011), "Computing Krippendorff's Alpha-Reliability" (nominal
    0.743, interval 0.849). None if no unit is pairable, or if every pairable value is the same
    (zero expected disagreement: agreement cannot be distinguished from chance).
    """
    if metric not in ("nominal", "interval"):
        raise ValueError(f"metric must be 'nominal' or 'interval', got {metric!r}")
    rated = []
    for unit in units:
        if metric == "interval":
            ratings = _values(unit)
        else:
            ratings = [r for r in unit if r is not None
                       and not (isinstance(r, float) and not math.isfinite(r))]
        if len(ratings) >= 2:
            rated.append(ratings)
    if not rated:
        return None
    if metric == "interval":  # one common power-of-two scale; it cancels in the ratio
        z, _ = _dyadic([r for ratings in rated for r in ratings])
        scaled, start = [], 0
        for ratings in rated:
            scaled.append(z[start:start + len(ratings)])
            start += len(ratings)
        rated = scaled
    pooled = [r for ratings in rated for r in ratings]
    expected = _pair_disagreement(pooled, metric)
    if expected == 0:
        return None
    observed = sum((Fraction(_pair_disagreement(r, metric), len(r) - 1) for r in rated),
                   Fraction(0))
    return float(1 - (len(pooled) - 1) * observed / expected)


# --- output ---------------------------------------------------------------------------------

def round_or_none(x, ndigits=4):
    """round(x, ndigits) for JSON output. None for None, NaN and +/-inf; never -0.0."""
    if x is None or isinstance(x, bool):
        return x
    if isinstance(x, float) and not math.isfinite(x):
        return None
    r = round(x, ndigits)
    return 0.0 if isinstance(r, float) and r == 0 else r


def rounded(obj, ndigits=4):
    """A copy of `obj` with every float in nested dicts/lists/tuples passed through
    round_or_none. Tuples become lists, so the result is plain JSON data."""
    if isinstance(obj, float):
        return round_or_none(obj, ndigits)
    if isinstance(obj, dict):
        return {key: rounded(value, ndigits) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [rounded(value, ndigits) for value in obj]
    return obj
