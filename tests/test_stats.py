"""Tests for eval/dfa_eval/stats.py against known reference values and hand-computed cases.

Reference values: Student-t tables, closed-form quantiles and the exact finite-series CDF
(Abramowitz & Stegun 26.7.3-4), and the Krippendorff (2011) worked example. Also every
None-returning edge case, seeded-bootstrap determinism, and that nothing ever returns NaN.
Pure computation, nothing is written. Standard library only:
    python -m unittest discover -s tests -v
"""

import json
import math
import sys
import unittest
from pathlib import Path
from statistics import NormalDist

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))
from dfa_eval import schema, schemas, stats  # noqa: E402

NAN, INF = float("nan"), float("inf")
Z975 = NormalDist().inv_cdf(0.975)


def t_cdf_series(t, df):
    """Exact Student-t CDF for integer df as a finite trigonometric series (A&S 26.7.3-4)."""
    theta = math.atan(t / math.sqrt(df))
    s, c = math.sin(theta), math.cos(theta)
    term = total = 1.0
    if df % 2 == 0:
        for k in range(1, df // 2):
            term *= (2 * k - 1) / (2 * k) * c * c
            total += term
        a = s * total
    elif df == 1:
        a = 2 * theta / math.pi
    else:
        for k in range(1, (df - 1) // 2):
            term *= (2 * k) / (2 * k + 1) * c * c
            total += term
        a = 2 / math.pi * (theta + s * c * total)
    return 0.5 + 0.5 * a


def walk_floats(obj):
    """Every float inside nested dicts/lists/tuples."""
    if isinstance(obj, float):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from walk_floats(value)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            yield from walk_floats(value)


class StudentTTest(unittest.TestCase):
    def test_matches_the_tables_to_three_decimals(self):
        for df, want in ((1, 12.7062), (2, 4.3027), (4, 2.7764), (10, 2.2281), (30, 2.0423)):
            self.assertAlmostEqual(stats.t_ppf(0.975, df), want, places=3, msg=df)

    def test_matches_closed_forms(self):
        def df1(p):  # Cauchy, from the upper tail so the float evaluation itself stays exact
            return math.copysign(1 / math.tan(math.pi * min(p, 1 - p)), p - 0.5)

        def df2(p):
            return (2 * p - 1) / math.sqrt(2 * p * (1 - p))

        def df4(p):
            a = 4 * p * (1 - p)
            q = math.cos(math.acos(math.sqrt(a)) / 3) / math.sqrt(a)
            return math.copysign(2 * math.sqrt(q - 1), p - 0.5)

        for p in (1e-9, 0.001, 0.01, 0.025, 0.05, 0.1, 0.25, 0.4, 0.6, 0.75, 0.9, 0.95, 0.975,
                  0.99, 0.995, 0.999):
            for df, exact in ((1, df1), (2, df2), (4, df4)):
                if df == 4 and not 0.001 <= p <= 0.999:
                    continue  # acos() near 1 loses digits in the reference formula itself
                want = exact(p)
                self.assertLess(abs(stats.t_ppf(p, df) - want), 1e-10 * abs(want), (p, df))

    def test_inverts_the_exact_finite_series_cdf(self):
        for df in range(1, 61):
            for p in (0.001, 0.025, 0.1, 0.3, 0.6, 0.9, 0.95, 0.975, 0.99, 0.999):
                residual = abs(t_cdf_series(stats.t_ppf(p, df), df) - p)
                self.assertLess(residual, 1e-13, (p, df))

    def test_median_symmetry_and_monotonicity(self):
        for df in (1, 3, 7.5, 50):
            self.assertEqual(stats.t_ppf(0.5, df), 0.0)
            self.assertAlmostEqual(stats.t_ppf(0.1, df), -stats.t_ppf(0.9, df), places=12)
            quantiles = [stats.t_ppf(p, df) for p in (0.01, 0.2, 0.45, 0.55, 0.8, 0.99)]
            self.assertEqual(quantiles, sorted(quantiles))
        dfs = (0.5, 1, 1.5, 2, 3, 10, 100, 1e4 - 1, 1e4, 1e6)
        by_df = [stats.t_ppf(0.975, df) for df in dfs]
        self.assertEqual(by_df, sorted(by_df, reverse=True))

    def test_near_one_half_is_accurate_in_absolute_terms(self):
        self.assertLess(abs(stats.t_ppf(0.5 + 1e-7, 1) - math.tan(math.pi * 1e-7)), 1e-15)

    def test_large_df_tends_to_the_normal_and_is_continuous_at_the_series_switch(self):
        self.assertEqual(stats.t_ppf(0.975, INF), Z975)
        self.assertEqual(stats.t_ppf(0.975, 10 ** 400), Z975)  # an int beyond float range
        self.assertAlmostEqual(stats.t_ppf(0.975, 1e9), Z975, places=8)
        below, at = stats.t_ppf(0.975, 1e4 - 1), stats.t_ppf(0.975, 1e4)
        self.assertGreater(below, at)
        self.assertLess(below - at, 1e-7)

    def test_undefined_inputs_return_none(self):
        for p, df in ((0, 5), (1, 5), (-0.1, 5), (1.1, 5), (NAN, 5), (None, 5), ("0.5", 5),
                      (True, 5), (0.9, 0), (0.9, -1), (0.9, NAN), (0.9, None), (0.9, True),
                      (0.975, 5e-324), (0.975, 1e-20)):  # quantile beyond float range
            self.assertIsNone(stats.t_ppf(p, df), (p, df))

    def test_incomplete_beta_closed_forms(self):
        for x in (1e-6, 0.01, 0.3, 0.5, 0.77, 0.999):
            y = 1 - x
            self.assertAlmostEqual(stats._betainc(1, 1, x, y), x, places=14)
            self.assertAlmostEqual(stats._betainc(3.5, 1, x, y), x ** 3.5, places=14)
            self.assertAlmostEqual(stats._betainc(1, 2.5, x, y), 1 - y ** 2.5, places=14)
            self.assertAlmostEqual(stats._betainc(0.5, 0.5, x, y),
                                   2 / math.pi * math.asin(math.sqrt(x)), places=13)
            self.assertAlmostEqual(stats._betainc(2, 7, x, y), 1 - stats._betainc(7, 2, y, x),
                                   places=14)


class DescribeTest(unittest.TestCase):
    def test_known_values(self):
        d = stats.describe([4, 1, 3, 2])
        sd = math.sqrt(5 / 3)
        half = 3.182446305284263 * sd / 2  # t(0.975, 3)
        self.assertEqual((d["n"], d["mean"], d["median"], d["min"], d["max"]), (4, 2.5, 2.5, 1, 4))
        self.assertAlmostEqual(d["sd"], sd, places=15)
        self.assertAlmostEqual(d["ci95"][0], 2.5 - half, places=12)
        self.assertAlmostEqual(d["ci95"][1], 2.5 + half, places=12)
        self.assertEqual(stats.describe([5, 1, 3])["median"], 3)

    def test_empty_and_single_value(self):
        self.assertEqual(stats.describe([]), {"n": 0, "mean": None, "median": None, "sd": None,
                                              "min": None, "max": None, "ci95": None})
        self.assertEqual(stats.describe([5]), {"n": 1, "mean": 5.0, "median": 5, "sd": None,
                                               "min": 5, "max": 5, "ci95": None})

    def test_missing_values_are_dropped_and_not_counted(self):
        d = stats.describe([None, NAN, 3, INF, 1, -INF])
        self.assertEqual((d["n"], d["mean"], d["min"], d["max"]), (2, 2.0, 1, 3))
        self.assertAlmostEqual(d["ci95"][1], 2 + 12.706204736174705, places=10)
        self.assertEqual(stats.describe([None, NAN])["n"], 0)

    def test_constant_data_has_exactly_zero_spread(self):
        # A naive float mean of three 0.1s is 0.10000000000000002 and its "SD" is not zero.
        d = stats.describe([0.1, 0.1, 0.1])
        self.assertEqual((d["mean"], d["sd"], d["ci95"]), (0.1, 0.0, [0.1, 0.1]))
        self.assertEqual(stats.mean([0.1, 0.1, 0.1]), 0.1)

    def test_output_is_valid_summary_json(self):
        for xs in ([], [2], [1, 2, 3, 4], [0.1] * 3, [7, None, 9.5]):
            d = stats.describe(xs)
            self.assertEqual(schema.validate(d, schemas.STAT), [], xs)
            self.assertEqual(schema.validate(stats.rounded(d), schemas.STAT), [], xs)
            json.dumps(stats.rounded(d), allow_nan=False)

    def test_mean_ci(self):
        xs = [1, 2, 3, 4]
        self.assertEqual(list(stats.mean_ci(xs)), stats.describe(xs)["ci95"])
        lo90, hi90 = stats.mean_ci(xs, level=0.9)
        self.assertAlmostEqual(hi90 - 2.5, 2.3533634348018264 * math.sqrt(5 / 3) / 2, places=12)
        self.assertLess(hi90, stats.mean_ci(xs)[1])
        self.assertIsNone(stats.mean_ci([1]))
        self.assertIsNone(stats.mean_ci([None, 1]))
        for level in (0, 1, 1.5, -0.5, True, None):
            with self.assertRaises(ValueError):
                stats.mean_ci(xs, level=level)

    def test_mean(self):
        self.assertIsNone(stats.mean([]))
        self.assertIsNone(stats.mean([None, NAN]))
        self.assertEqual(stats.mean([1, None, NAN, 2]), 1.5)
        self.assertEqual(stats.mean([True, False]), 0.5)
        self.assertEqual(stats.mean([2 ** 63 + 1, 2 ** 63 + 3]), float(2 ** 63 + 2))
        with self.assertRaises(TypeError):
            stats.mean(["1"])


class EffectSizeTest(unittest.TestCase):
    def test_cohens_d_and_hedges_g_hand_computed(self):
        self.assertEqual(stats.cohens_d([1, 2, 3], [0, 1, 2]), 1.0)  # pooled SD 1
        self.assertAlmostEqual(stats.hedges_g([1, 2, 3], [0, 1, 2]), 0.8, places=15)  # 1 - 3/15
        # Unequal n: means 5 and 2, pooled variance (20 + 2) / 4 = 5.5, J = 1 - 3/(4*6 - 9).
        d = 3 / math.sqrt(5.5)
        self.assertAlmostEqual(stats.cohens_d([2, 4, 6, 8], [1, 3]), d, places=15)
        self.assertAlmostEqual(stats.hedges_g([2, 4, 6, 8], [1, 3]), 0.8 * d, places=15)
        self.assertAlmostEqual(stats.hedges_g([1, 3], [2, 4, 6, 8]), -0.8 * d, places=15)
        self.assertEqual(stats.cohens_d([1, 2], [2, 1]), 0.0)

    def test_hedges_correction_at_the_smallest_sizes(self):
        d = stats.cohens_d([1, 3], [0, 1])
        self.assertAlmostEqual(stats.hedges_g([1, 3], [0, 1]), d * (1 - 3 / 7), places=15)

    def test_undefined_effect_sizes_return_none(self):
        cases = [([1], [1, 2, 3]), ([1, 2, 3], [4]), ([], [1, 2]), ([1, 2], []),
                 ([3, None, NAN], [1, 2]),
                 ([1, 1], [2, 2]),  # different means, but no spread to standardize by
                 ([0.1] * 3, [0.1] * 3), ([5, 5, 5], [5, 5])]
        for a, b in cases:
            self.assertIsNone(stats.cohens_d(a, b), (a, b))
            self.assertIsNone(stats.hedges_g(a, b), (a, b))

    def test_paired_dz(self):
        self.assertEqual(stats.paired_dz([1, 2, 3]), 2.0)
        self.assertEqual(stats.paired_dz([-1, -2, -3]), -2.0)
        self.assertAlmostEqual(stats.paired_dz([2, 4]), 3 / math.sqrt(2), places=15)
        self.assertEqual(stats.paired_dz([1, -1]), 0.0)
        for diffs in ([5], [], [None, 3], [0.1] * 4, [2, 2, None]):
            self.assertIsNone(stats.paired_dz(diffs), diffs)

    def test_cliffs_delta_with_ties(self):
        self.assertEqual(stats.cliffs_delta([3, 4], [1, 2]), 1.0)
        self.assertEqual(stats.cliffs_delta([1, 2], [3, 4]), -1.0)
        self.assertEqual(stats.cliffs_delta([1, 2, 3], [1, 2, 3]), 0.0)
        # [1,2,2] vs [2,3]: 0 pairs greater, 4 smaller, 2 tied -> (0 - 4) / 6
        self.assertAlmostEqual(stats.cliffs_delta([1, 2, 2], [2, 3]), -2 / 3, places=15)
        # [5,3,3] vs [3,1]: 4 greater, 0 smaller, 2 tied
        self.assertAlmostEqual(stats.cliffs_delta([5, 3, 3], [3, 1]), 2 / 3, places=15)
        self.assertEqual(stats.cliffs_delta([2.5, None], [2, 3]), 0.0)
        for a, b in (([], [1]), ([1], []), ([None], [1]), ([], [])):
            self.assertIsNone(stats.cliffs_delta(a, b), (a, b))

    def test_auc_counts_ties_as_half(self):
        self.assertAlmostEqual(stats.auc([1, 2, 2], [2, 3]), 1 / 6, places=15)  # 2 ties x 0.5
        self.assertEqual(stats.auc([2], [2]), 0.5)
        self.assertEqual(stats.auc([0.9, 0.8], [0.1, 0.7]), 1.0)
        self.assertEqual(stats.auc([0.1, 0.2], [0.7, 0.8]), 0.0)
        self.assertEqual(stats.auc([0.5, 0.5], [0.5]), 0.5)
        for pos, neg in (([1, 2, 2], [2, 3]), ([5, 3, 3], [3, 1]), ([0.2, 0.9, 0.4], [0.4, 0.1])):
            self.assertAlmostEqual(stats.auc(pos, neg), (1 + stats.cliffs_delta(pos, neg)) / 2,
                                   places=15)
        for pos, neg in (([], [1]), ([1], []), ([NAN], [0.5])):
            self.assertIsNone(stats.auc(pos, neg), (pos, neg))


class CorrelationTest(unittest.TestCase):
    def test_spearman_with_ties(self):
        # ranks y = [1, 2.5, 2.5, 4]: r = 4.5 / sqrt(5 * 4.5) = sqrt(0.9)
        self.assertAlmostEqual(stats.spearman([1, 2, 3, 4], [1, 2, 2, 3]), math.sqrt(0.9),
                               places=15)
        # ties on both sides: ranks [1.5,1.5,3,4] and [1,2.5,2.5,4] give 3.75 / 4.5
        self.assertAlmostEqual(stats.spearman([1, 1, 2, 3], [1, 2, 2, 3]), 5 / 6, places=15)

    def test_spearman_is_rank_based(self):
        xs, cubes = [1, 2, 3, 4, 5], [1, 8, 27, 64, 125]
        self.assertEqual(stats.spearman(xs, cubes), 1.0)
        self.assertEqual(stats.spearman(xs, cubes[::-1]), -1.0)
        self.assertLess(stats.pearson(xs, cubes), 1.0)

    def test_pearson_known_values(self):
        self.assertAlmostEqual(stats.pearson([1, 2, 3], [1, 3, 2]), 0.5, places=15)
        self.assertEqual(stats.pearson([1, 2, 3], [2, 4, 6]), 1.0)
        self.assertEqual(stats.pearson([1, 2, 3], [3, 2, 1]), -1.0)
        self.assertEqual(stats.pearson([1, 2, 3], [1, 0, 1]), 0.0)
        self.assertEqual(stats.pearson([0.1, 0.2, 0.3], [0.2, 0.4, 0.6]), 1.0)

    def test_undefined_correlations_return_none(self):
        for f in (stats.pearson, stats.spearman):
            self.assertIsNone(f([1, 2], [1, 2]))
            self.assertIsNone(f([], []))
            self.assertIsNone(f([1, 2, 3], [5, 5, 5]))
            self.assertIsNone(f([0.1, 0.1, 0.1], [1, 2, 3]))
            self.assertIsNone(f([1, 2, None, 4], [1, None, 3, 4]))  # 2 complete pairs
        self.assertAlmostEqual(stats.pearson([1, 2, None, 3], [1, 3, 9, 2]), 0.5, places=15)

    def test_unequal_lengths_are_a_caller_bug(self):
        with self.assertRaises(ValueError):
            stats.spearman([1, 2, 3], [1, 2])
        with self.assertRaises(ValueError):
            stats.pearson([1, 2], [1, 2, 3])


class ProportionTest(unittest.TestCase):
    def test_wilson_published_values(self):
        for k, n, lo, hi in ((5, 10, 0.2366, 0.7634), (1, 10, 0.0179, 0.4042),
                             (0, 10, 0.0, 0.2775), (10, 10, 0.7225, 1.0)):
            got = stats.wilson_ci(k, n)
            self.assertAlmostEqual(got[0], lo, places=4, msg=(k, n))
            self.assertAlmostEqual(got[1], hi, places=4, msg=(k, n))

    def test_wilson_exact_bounds(self):
        z2 = Z975 ** 2
        self.assertEqual(stats.wilson_ci(0, 10)[0], 0.0)
        self.assertAlmostEqual(stats.wilson_ci(0, 10)[1], z2 / (10 + z2), places=15)
        self.assertEqual(stats.wilson_ci(10, 10)[1], 1.0)
        self.assertAlmostEqual(stats.wilson_ci(10, 10)[0], 10 / (10 + z2), places=15)
        lo, hi = stats.wilson_ci(5, 10)
        self.assertAlmostEqual(lo + hi, 1.0, places=15)
        for k in range(8):
            lo, hi = stats.wilson_ci(k, 7)
            self.assertTrue(0.0 <= lo <= k / 7 <= hi <= 1.0, k)

    def test_wilson_level_and_edges(self):
        lo90, hi90 = stats.wilson_ci(3, 12, level=0.9)
        lo95, hi95 = stats.wilson_ci(3, 12)
        self.assertTrue(lo95 < lo90 < hi90 < hi95)
        self.assertIsNone(stats.wilson_ci(0, 0))
        for k, n in ((3, 2), (-1, 5), (1.5, 3), (True, 3)):
            with self.assertRaises(ValueError):
                stats.wilson_ci(k, n)

    def test_ceiling_fraction(self):
        self.assertAlmostEqual(stats.ceiling_fraction([20, 20, 19, None], 20), 2 / 3, places=15)
        self.assertEqual(stats.ceiling_fraction([21, 3], 20), 0.5)
        self.assertEqual(stats.ceiling_fraction([0.5], 1), 0.0)
        self.assertIsNone(stats.ceiling_fraction([], 20))
        self.assertIsNone(stats.ceiling_fraction([None, NAN], 20))


class BootstrapTest(unittest.TestCase):
    DATA = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]

    def test_seeded_bootstrap_is_deterministic(self):
        first = stats.bootstrap_ci(self.DATA, n_resamples=2000, seed=0)
        self.assertEqual(first, stats.bootstrap_ci(list(self.DATA), n_resamples=2000, seed=0))
        self.assertNotEqual(first, stats.bootstrap_ci(self.DATA, n_resamples=2000, seed=1))

    def test_pinned_values(self):
        # Pinned so committed summaries stay reproducible: random() is the one sequence Python
        # promises to keep across versions. Change these only with a harness version bump.
        self.assertEqual(stats.bootstrap_ci(self.DATA, n_resamples=2000, seed=0), (3.8, 7.4))
        self.assertEqual(stats.bootstrap_ci([0.5, 1.25, 3, 7.75], n_resamples=500, level=0.9,
                                            seed=7), (0.875, 5.375))

    def test_percentiles_interpolate_linearly_between_order_statistics(self):
        counter = iter(range(11))  # the "statistic" of the 11 resamples is 0, 1, ..., 10
        interval = stats.bootstrap_ci([1, 2], stat=lambda sample: next(counter),
                                      n_resamples=11, level=0.5)
        self.assertEqual(interval, (2.5, 7.5))  # quantiles 0.25 and 0.75 of 0..10

    def test_interval_brackets_the_estimate_and_shrinks_with_level(self):
        lo, hi = stats.bootstrap_ci(self.DATA)
        self.assertTrue(lo < 5.5 < hi)
        lo50, hi50 = stats.bootstrap_ci(self.DATA, level=0.5)
        self.assertTrue(lo < lo50 < 5.5 < hi50 < hi)

    def test_default_mean_equals_calling_mean_on_each_resample(self):
        data = [0.1, 0.7, 2.25, 3.3, 9.0]
        self.assertEqual(stats.bootstrap_ci(data, n_resamples=300, seed=5),
                         stats.bootstrap_ci(data, stat=lambda s: stats.mean(s), n_resamples=300,
                                            seed=5))

    def test_custom_statistic(self):
        def median(sample):
            return sorted(sample)[len(sample) // 2]

        lo, hi = stats.bootstrap_ci(self.DATA, stat=median, n_resamples=500, seed=3)
        self.assertTrue(lo <= 6 <= hi)
        self.assertEqual((lo, hi),
                         stats.bootstrap_ci(self.DATA, stat=median, n_resamples=500, seed=3))

    def test_undefined_cases_return_none(self):
        def undefined_on_ties(sample):
            return None if len(set(sample)) == 1 else stats.mean(sample)

        self.assertIsNone(stats.bootstrap_ci([]))
        self.assertIsNone(stats.bootstrap_ci([3]))
        self.assertIsNone(stats.bootstrap_ci([3, None, NAN]))
        self.assertIsNone(stats.bootstrap_ci([1, 2], stat=undefined_on_ties, n_resamples=200))
        self.assertIsNone(stats.bootstrap_ci([1, 2], stat=lambda s: NAN, n_resamples=10))
        self.assertEqual(stats.bootstrap_ci([4, 4, 4], n_resamples=50), (4.0, 4.0))

    def test_invalid_arguments_raise(self):
        for kwargs in ({"n_resamples": 0}, {"n_resamples": 1.5}, {"seed": None}, {"seed": 1.5},
                       {"seed": True}, {"level": 1}, {"level": 0}):
            with self.assertRaises(ValueError, msg=kwargs):
                stats.bootstrap_ci(self.DATA, **kwargs)


class ClusterBootstrapTest(unittest.TestCase):
    GROUPS = {"P1": ([14, 16, 15], [12, 13, 12]), "P2": ([18, 17], [17, 15, 16]),
              "P3": ([10, 12, 11], [11, 10])}

    def test_pinned_value_and_determinism(self):
        got = stats.cluster_bootstrap_diff(self.GROUPS, n_resamples=2000, seed=0)
        self.assertEqual(got, (0.3888888888888887, 2.722222222222222))  # pinned, as above
        other_seed = stats.cluster_bootstrap_diff(self.GROUPS, n_resamples=2000, seed=1)
        self.assertNotEqual(got, other_seed)

    def test_cluster_order_in_the_mapping_does_not_matter(self):
        reordered = dict(reversed(list(self.GROUPS.items())))
        self.assertEqual(stats.cluster_bootstrap_diff(reordered, n_resamples=500, seed=4),
                         stats.cluster_bootstrap_diff(self.GROUPS, n_resamples=500, seed=4))

    def test_resamples_clusters_and_averages_their_differences(self):
        # Constant arms: cluster differences 0 and 2, so a resample's mean over 2 drawn clusters
        # is 0, 1 or 2 with probability 1/4, 1/2, 1/4.
        groups = {"A": ([1, 1], [1, 1]), "B": ([3, 3], [1, 1])}
        self.assertEqual(stats.cluster_bootstrap_diff(groups), (0.0, 2.0))
        self.assertEqual(stats.cluster_bootstrap_diff(groups, level=0.4), (1.0, 1.0))

    def test_resamples_values_within_each_drawn_cluster(self):
        # Without within-cluster resampling every difference would be exactly 5. With it, a
        # cluster's mean is 0, 5 or 10, and both clusters at 0 (or at 10) has probability 1/16.
        groups = {"A": ([0, 10], [0]), "B": ([0, 10], [0])}
        self.assertEqual(stats.cluster_bootstrap_diff(groups), (0.0, 10.0))

    def test_constant_difference_gives_a_point_interval(self):
        groups = {"A": ([2, 2], [1]), "B": ([5], [4, 4])}
        self.assertEqual(stats.cluster_bootstrap_diff(groups), (1.0, 1.0))

    def test_interval_contains_the_observed_difference(self):
        lo, hi = stats.cluster_bootstrap_diff(self.GROUPS, n_resamples=3000, seed=2)
        observed = stats.mean([stats.mean(t) - stats.mean(c) for t, c in self.GROUPS.values()])
        self.assertTrue(lo < observed < hi)

    def test_undefined_cases_return_none(self):
        self.assertIsNone(stats.cluster_bootstrap_diff({}))
        self.assertIsNone(stats.cluster_bootstrap_diff({"P1": ([1, 2], [0, 1])}))  # one cluster
        self.assertIsNone(stats.cluster_bootstrap_diff({"P1": ([1, 2], []), "P2": ([1], [2])}))
        self.assertIsNone(stats.cluster_bootstrap_diff({"P1": ([1, 2], [3]),
                                                        "P2": ([None, NAN], [2])}))

    def test_invalid_arguments_raise(self):
        with self.assertRaises(ValueError):
            stats.cluster_bootstrap_diff(self.GROUPS, seed=None)
        with self.assertRaises(ValueError):
            stats.cluster_bootstrap_diff(self.GROUPS, n_resamples=0)


class BootstrapDiffTest(unittest.TestCase):
    """Two independent samples (outcome attempts per arm)."""

    def test_constant_arms_give_a_point_interval(self):
        self.assertEqual(stats.bootstrap_diff_ci([3, 3, 3], [1, 1]), (2.0, 2.0))

    def test_bounds_follow_from_the_resampling(self):
        # Treatment {0, 10} resamples to means 0, 5 or 10; control is constant 0. So the 95%
        # interval runs from 0 to 10, and an 0.4-level interval sits on 5.
        self.assertEqual(stats.bootstrap_diff_ci([0, 10], [0, 0]), (0.0, 10.0))
        self.assertEqual(stats.bootstrap_diff_ci([0, 10], [0, 0], level=0.4), (5.0, 5.0))

    def test_deterministic_for_a_seed_and_contains_the_observed_difference(self):
        t, c = [0.9, 0.8, 1.0, 0.7], [0.6, 0.7, 0.5, 0.8]
        first = stats.bootstrap_diff_ci(t, c, n_resamples=3000, seed=7)
        self.assertEqual(first, stats.bootstrap_diff_ci(t, c, n_resamples=3000, seed=7))
        self.assertNotEqual(first, stats.bootstrap_diff_ci(t, c, n_resamples=3000, seed=8))
        lo, hi = first
        self.assertTrue(lo < stats.mean(t) - stats.mean(c) < hi)

    def test_undefined_cases_return_none(self):
        self.assertIsNone(stats.bootstrap_diff_ci([1], [1, 2]))
        self.assertIsNone(stats.bootstrap_diff_ci([1, 2], []))
        self.assertIsNone(stats.bootstrap_diff_ci([1, NAN], [1, 2]))

    def test_invalid_arguments_raise(self):
        with self.assertRaises(ValueError):
            stats.bootstrap_diff_ci([1, 2], [1, 2], seed=None)
        with self.assertRaises(ValueError):
            stats.bootstrap_diff_ci([1, 2], [1, 2], level=1.5)


class KrippendorffTest(unittest.TestCase):
    # Krippendorff (2011), "Computing Krippendorff's Alpha-Reliability": 4 coders x 12 units.
    _ = None
    CODERS = [
        [1, 2, 3, 3, 2, 1, 4, 1, 2, _, _, _],  # A
        [1, 2, 3, 3, 2, 2, 4, 1, 2, 5, _, 3],  # B
        [_, 3, 3, 3, 2, 3, 4, 2, 2, 5, 1, _],  # C
        [1, 2, 3, 3, 2, 4, 4, 1, 2, 5, 1, _],  # D
    ]
    UNITS = [list(unit) for unit in zip(*CODERS)]

    def test_reproduces_the_published_example(self):
        self.assertAlmostEqual(stats.krippendorff_alpha(self.UNITS, "nominal"), 0.743, places=3)
        self.assertAlmostEqual(stats.krippendorff_alpha(self.UNITS, "interval"), 0.849, places=3)
        self.assertEqual(stats.krippendorff_alpha(self.UNITS),
                         stats.krippendorff_alpha(self.UNITS, "interval"))

    def test_units_with_fewer_than_two_ratings_are_ignored(self):
        self.assertEqual(self.UNITS[11], [None, 3, None, None])
        for metric in ("nominal", "interval"):
            full = stats.krippendorff_alpha(self.UNITS, metric)
            self.assertEqual(stats.krippendorff_alpha(self.UNITS[:11], metric), full)
            padded = self.UNITS + [[None] * 4, [NAN, 7]]
            self.assertEqual(stats.krippendorff_alpha(padded, metric), full)

    def test_hand_computed_cases(self):
        # pooled {1,2,3,3}; within-unit disagreement 2 in unit 1 and 0 in unit 2;
        # total over all pairs 22 (interval) or 16 - 6 = 10 (nominal): alpha = 1 - 3 * 2 / total
        self.assertAlmostEqual(stats.krippendorff_alpha([[1, 2], [3, 3]], "interval"), 8 / 11,
                               places=15)
        self.assertAlmostEqual(stats.krippendorff_alpha([[1, 2], [3, 3]], "nominal"), 0.4,
                               places=15)
        # systematic disagreement is worse than chance
        self.assertEqual(stats.krippendorff_alpha([[1, 2], [1, 2]], "nominal"), -0.5)
        self.assertEqual(stats.krippendorff_alpha([[1, 2], [1, 2]], "interval"), -0.5)
        labels = [["a", "a"], ["b", "b"], ["a", "b"]]
        self.assertAlmostEqual(stats.krippendorff_alpha(labels, "nominal"), 4 / 9, places=15)

    def test_perfect_agreement(self):
        units = [[1, 1, 1], [2, 2, None], [3.5, 3.5]]
        self.assertEqual(stats.krippendorff_alpha(units, "nominal"), 1.0)
        self.assertEqual(stats.krippendorff_alpha(units, "interval"), 1.0)

    def test_undefined_cases_return_none(self):
        for units in ([], [[3]], [[1, None], [None, None]], [[2, 2], [2, 2, 2]]):
            self.assertIsNone(stats.krippendorff_alpha(units, "nominal"), units)
            self.assertIsNone(stats.krippendorff_alpha(units, "interval"), units)

    def test_unknown_metric_raises(self):
        with self.assertRaises(ValueError):
            stats.krippendorff_alpha(self.UNITS, "ordinal")


class RoundingTest(unittest.TestCase):
    def test_round_or_none(self):
        self.assertEqual(stats.round_or_none(1.23456), 1.2346)
        self.assertEqual(stats.round_or_none(1.23456, 1), 1.2)
        for missing in (None, NAN, INF, -INF):
            self.assertIsNone(stats.round_or_none(missing))
        zero = stats.round_or_none(-0.00001)
        self.assertEqual((zero, math.copysign(1, zero)), (0.0, 1.0))  # never -0.0
        self.assertIs(stats.round_or_none(True), True)
        self.assertEqual(stats.round_or_none(7), 7)
        self.assertIsInstance(stats.round_or_none(7), int)

    def test_rounded_recurses_and_returns_plain_json(self):
        data = {"a": 1.23456, "b": [0.11111, (2.22222, None)],
                "c": {"d": NAN, "e": -0.00001, "f": "x", "g": True, "h": 3}}
        out = stats.rounded(data)
        self.assertEqual(out, {"a": 1.2346, "b": [0.1111, [2.2222, None]],
                               "c": {"d": None, "e": 0.0, "f": "x", "g": True, "h": 3}})
        self.assertEqual(list(out["c"]), ["d", "e", "f", "g", "h"])
        self.assertEqual(json.dumps(out, allow_nan=False),
                         '{"a": 1.2346, "b": [0.1111, [2.2222, null]], '
                         '"c": {"d": null, "e": 0.0, "f": "x", "g": true, "h": 3}}')
        self.assertTrue(math.isnan(data["c"]["d"]))  # input untouched
        self.assertIsInstance(data["b"][1], tuple)


class NeverNaNTest(unittest.TestCase):
    BIG = 1.7976931348623157e308
    SAMPLES = [[], [None], [NAN], [INF, -INF], [1], [1, 1], [0.1] * 3, [1, 2], [3, 1, 2],
               [1, NAN, 3, None, 3], [1e15, -1e15, 3], [2 ** 63 + 1, 1], [BIG, -BIG],
               [BIG, BIG, -BIG], [5e-324, 0, -5e-324], [0, 1, 0, 1, 1]]

    def assertNoNaN(self, value, label):
        for x in walk_floats(value):
            self.assertTrue(math.isfinite(x), (label, value))

    def test_no_function_returns_nan_or_infinity(self):
        for xs in self.SAMPLES:
            ys = list(reversed(xs))
            results = {
                "describe": stats.describe(xs), "mean": stats.mean(xs),
                "mean_ci": stats.mean_ci(xs), "paired_dz": stats.paired_dz(xs),
                "ceiling": stats.ceiling_fraction(xs, 1),
                "spearman": stats.spearman(xs, ys), "pearson": stats.pearson(xs, ys),
                "bootstrap": stats.bootstrap_ci(xs, n_resamples=50),
                "rounded": stats.rounded(stats.describe(xs)),
                "alpha_interval": stats.krippendorff_alpha([xs, ys]),
                "alpha_nominal": stats.krippendorff_alpha([xs, ys], "nominal"),
            }
            for other in self.SAMPLES:
                results[f"cohens_d {other}"] = stats.cohens_d(xs, other)
                results[f"hedges_g {other}"] = stats.hedges_g(xs, other)
                results[f"cliffs {other}"] = stats.cliffs_delta(xs, other)
                results[f"auc {other}"] = stats.auc(xs, other)
                results[f"cluster {other}"] = stats.cluster_bootstrap_diff(
                    {"a": (xs, other), "b": (other, xs)}, n_resamples=30)
            for label, value in results.items():
                self.assertNoNaN(value, (label, xs))
        for n in range(6):
            for k in range(n + 1):
                self.assertNoNaN(stats.wilson_ci(k, n), (k, n))
        for p in (1e-300, 1e-12, 0.3, 0.5, 0.97, 1 - 1e-16):
            for df in (5e-324, 1e-3, 0.5, 1, 2.5, 30, 1e4, 1e12, INF, 10 ** 400):
                self.assertNoNaN(stats.t_ppf(p, df), (p, df))


if __name__ == "__main__":
    unittest.main()
