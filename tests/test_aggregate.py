"""Tests for eval/dfa_eval/aggregate.py: summary.json from raw run records.

Fixture runs are written by tests/fixtures/runbuilder.py into temp dirs. Expected numbers are
worked out by hand in the tests (closed-form arithmetic, Student-t table values), not by calling
stats.py, so a wrong formula or a wrong degrees-of-freedom cannot hide behind itself.
Standard library only:  python -m unittest discover -s tests -v
"""

import contextlib
import hashlib
import json
import math
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from statistics import NormalDist
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
from dfa_eval import aggregate, records, schema, schemas  # noqa: E402
import runbuilder as rb  # noqa: E402
from fixture_runs import (BASELINE_P1_R1, EQ_CONTROL, EQ_TREATMENT,  # noqa: E402
                          contrast_run, rich_run, small_run)

# Student-t 0.975 quantiles (scipy.stats.t.ppf), indexed by degrees of freedom.
T975 = {1: 12.706204736174698, 2: 4.302652729911275}


def r4(x):
    return round(x, 4)


def find(items, **keys):
    hits = [i for i in items if all(i.get(k) == v for k, v in keys.items())]
    if len(hits) != 1:
        raise AssertionError(f"expected one entry for {keys}, found {len(hits)}")
    return hits[0]


def summary_errors(summary):
    errors = schema.validate(summary, schemas.SUMMARY)
    for cell in summary["cells"]:
        for stat in cell["metrics"].values():
            errors += schema.validate(stat, schemas.STAT)
    return errors


def left_out_note(rubric, treatment=None, control=None):
    """The contrast note for arms that lost generations to incomplete judging; an arm is
    (generations left out, ok generations), or None if it lost none."""
    parts = [f"{lost[0]} of {lost[1]} {arm}" for arm, lost in (("treatment", treatment),
                                                               ("control", control)) if lost]
    return (f"incomplete judging left out {' and '.join(parts)} generation(s): not every "
            f"configured judge x repeat scored them ({rubric})")


def left_out_flag(rubric, condition, n, n_ok, lacking, gen_ids, generator="g1"):
    lacking = ", ".join(f"lacking {judge}: {k}" for judge, k in lacking)
    return (f"incomplete judging: {rubric} metrics leave out {n} of {n_ok} ok {condition} "
            f"generation(s) (generator {generator}) that not every configured judge x repeat "
            f"scored ok ({lacking}): {', '.join(gen_ids)}")


@contextlib.contextmanager
def blind_sha_field():
    """JUDGMENT and PROBE with their optional blind_sha256 field (written by judge.py). Patched in
    only while schemas.py does not declare it, and restored afterwards."""
    with contextlib.ExitStack() as stack:
        for record_schema in (schemas.JUDGMENT, schemas.PROBE):
            if "blind_sha256" not in record_schema["properties"]:
                stack.enter_context(mock.patch.dict(record_schema["properties"],
                                                    {"blind_sha256": schemas.SHA256}))
        yield


class FixtureTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def summarize(self, run_dir):
        self.assertEqual(rb.validate_run_dir(run_dir), [])
        summary = aggregate.aggregate(run_dir)
        self.assertEqual(summary_errors(summary), [])
        return summary


class SharedRun:
    """Mixin: one fixture run, built and aggregated once per class (the bootstrap is slow)."""

    build = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.addClassCleanup(shutil.rmtree, cls.tmp)
        cls.run_dir = cls.build(cls.tmp)
        cls.summary = aggregate.aggregate(cls.run_dir)

    def test_fixture_records_and_summary_are_valid(self):
        self.assertEqual(rb.validate_run_dir(self.run_dir), [])
        self.assertEqual(summary_errors(self.summary), [])


# --- a contrast worked out by hand --------------------------------------------------------

class ContrastByHandTest(SharedRun, unittest.TestCase):
    build = staticmethod(contrast_run)

    def contrast(self, metric):
        return find(self.summary["contrasts"], treatment="dfa", control="baseline", metric=metric)

    def test_eq_contrast(self):
        c = self.contrast("eq_total")
        # Per-prompt cell means: dfa 32, 27, 37; baseline 21, 22, 29.
        self.assertEqual([row["prompt"] for row in c["per_prompt"]], ["P1", "P2", "P3"])
        self.assertEqual([row["diff"] for row in c["per_prompt"]], [11.0, 5.0, 8.0])
        self.assertEqual(c["mean_diff"], 8.0)
        # sd of (11, 5, 8): sqrt(((11-8)^2 + (5-8)^2 + 0) / (3 - 1)) = 3.
        self.assertEqual(c["diff"]["sd"], 3.0)
        # t-interval across prompts, df = n_prompts - 1 = 2: 8 +/- t(0.975, 2) * 3 / sqrt(3).
        half = T975[2] * 3 / math.sqrt(3)
        self.assertEqual(c["diff"]["ci95"], [r4(8 - half), r4(8 + half)])
        self.assertEqual(c["diff"]["ci95"], [0.5476, 15.4524])
        self.assertEqual(c["dz"], r4(8 / 3))
        # Hedges g within prompts: t = (30, 34), (28, 26), (36, 38), c = (20, 22), (24, 20),
        # (30, 28); within-cell SS 8+2, 2+8, 2+2 = 24 over df = 3 x (2 + 2 - 2) = 6, so the
        # pooled within-prompt SD is 2; d = 8 / 2; J = 1 - 3 / (4 x 6 - 1).
        self.assertEqual(c["hedges_g"], r4(4 * (1 - 3 / 23)))
        # Cliff's delta over same-prompt pairs only: all 12 have t > c. (Pooled over prompts it
        # would be (31 - 3) / 36: prompt difficulty would leak in.)
        self.assertEqual(c["cliffs_delta"], 1.0)
        self.assertEqual((c["n_prompts"], c["n_treatment"], c["n_control"]), (3, 6, 6))
        self.assertEqual(c["notes"], ["n_prompts < 5: interval is wide"])

    def test_bootstrap_interval(self):
        c = self.contrast("eq_total")
        lo, hi = c["cluster_bootstrap_ci"]
        # Any resample's per-prompt difference lies in [min t - max c, max t - min c]:
        # P1 [8, 14], P2 [2, 8], P3 [6, 10]; so the interval lies in [2, 14] around the mean.
        self.assertTrue(2 <= lo <= c["mean_diff"] <= hi <= 14, (lo, hi))
        self.assertEqual(c["bootstrap"]["resamples"], 10000)
        expected_seed = int(hashlib.sha256(b"11:contrast:dfa:baseline:g1").hexdigest()[:8], 16)
        self.assertEqual(c["bootstrap"]["seed"], expected_seed)

    def test_eq_core_and_words_follow_the_same_design(self):
        core = {p: [rb.eq_core(rb.spread(t, 11, 4)) for t in EQ_TREATMENT[p]] for p in EQ_TREATMENT}
        ctrl = {p: [rb.eq_core(rb.spread(t, 11, 4)) for t in EQ_CONTROL[p]] for p in EQ_CONTROL}
        diffs = [sum(core[p]) / 2 - sum(ctrl[p]) / 2 for p in ("P1", "P2", "P3")]
        c = self.contrast("eq_core")
        self.assertEqual([row["diff"] for row in c["per_prompt"]], [r4(x) for x in diffs])
        self.assertEqual(c["mean_diff"], r4(sum(diffs) / 3))
        words = self.contrast("words")  # words are 10x the EQ totals: sd 30, same df
        self.assertEqual([row["diff"] for row in words["per_prompt"]], [110.0, 50.0, 80.0])
        half = T975[2] * 30 / math.sqrt(3)
        self.assertEqual(words["diff"]["ci95"], [r4(80 - half), r4(80 + half)])

    def test_ceiling_and_zero_variance(self):
        adherence = self.contrast("adherence_total")
        self.assertEqual(adherence["mean_diff"], 8.0)
        self.assertIn("ceiling: 100% of treatment generations at max", adherence["notes"])
        self.assertIn("no variation in the per-prompt differences: dz undefined",
                      adherence["notes"])
        self.assertIn("no variation within prompts: g undefined", adherence["notes"])
        self.assertIsNone(adherence["dz"])
        self.assertIsNone(adherence["hedges_g"])
        self.assertEqual(adherence["cliffs_delta"], 1.0)
        lint_score = self.contrast("lint_score")
        self.assertEqual(lint_score["mean_diff"], 0.75)
        self.assertIn("ceiling: 100% of treatment generations at max", lint_score["notes"])
        self.assertIn("no variation in the per-prompt differences: dz undefined",
                      lint_score["notes"])
        eq = self.contrast("eq_total")
        self.assertFalse(any(n.startswith("ceiling") for n in eq["notes"]))

    def test_conditions_weight_each_prompt_equally(self):
        dfa = find(self.summary["conditions"], condition="dfa", generator="g1")
        base = find(self.summary["conditions"], condition="baseline", generator="g1")
        self.assertEqual(dfa["metrics"]["eq_total"]["prompt_means"]["mean"], 32.0)
        self.assertEqual(dfa["metrics"]["eq_total"]["prompt_means"]["sd"], 5.0)  # 32, 27, 37
        self.assertEqual(dfa["metrics"]["eq_total"]["prompt_means"]["n"], 3)
        self.assertEqual(dfa["metrics"]["eq_total"]["pooled"]["n"], 6)
        self.assertEqual(base["metrics"]["eq_total"]["prompt_means"]["sd"], r4(math.sqrt(19)))
        self.assertEqual((dfa["n_prompts"], dfa["n_generations"]), (3, 6))
        self.assertIn("not independent", dfa["note"])

    def test_leakage_auc_and_accuracy(self):
        (entry,) = self.summary["leakage"]
        d = entry["skill_vs_plain"]
        # 36 (skill, plain) pairs: 30 skill-higher, 2 ties -> AUC (30 + 2/2) / 36.
        self.assertEqual(d["auc"], r4(31 / 36))
        # p > 0.5 guesses "methodology": 4 of 6 skill plans and 5 of 6 plain plans are right.
        self.assertEqual((d["n"], d["n_positive"], d["n_negative"]), (12, 6, 6))
        self.assertEqual(d["accuracy"], 0.75)
        z = NormalDist().inv_cdf(0.975)
        k, n = 9, 12
        center = (k / n + z * z / (2 * n)) / (1 + z * z / n)
        half = z * math.sqrt(k / n * (1 - k / n) / n + z * z / (4 * n * n)) / (1 + z * z / n)
        self.assertEqual(d["accuracy_ci95"], [r4(center - half), r4(center + half)])
        self.assertEqual(entry["contrasts"][0]["auc"], d["auc"])
        self.assertIn("0.5 = judges cannot tell", entry["note"])

    def test_cost_and_registry(self):
        cost = self.summary["cost"]
        self.assertEqual(cost["generation"]["cost_usd"], r4(6 * 0.10 + 6 * 0.05))
        self.assertEqual(cost["generation"]["n_calls"], 12)
        self.assertEqual(cost["judging"]["n_calls"], 24)
        self.assertEqual(cost["probe"]["n_calls"], 12)
        self.assertEqual((cost["superseded"]["n_calls"], cost["superseded"]["cost_usd"]), (0, 0.0))
        self.assertEqual(cost["total"]["n_calls"], 12 + 24 + 12)
        self.assertEqual(cost["total"]["n_unknown_cost"], 0)
        metrics = self.summary["metrics"]
        self.assertEqual(metrics["adherence_total"]["scale"], {"min": 0, "max": 20})
        self.assertEqual(metrics["eq_total"]["scale"], {"min": 0, "max": 44})
        self.assertEqual(metrics["eq_core"]["scale"], {"min": 0, "max": 36})
        self.assertEqual(metrics["lint_score"]["category"], "structure")
        self.assertIn("eq_dim_validation-quality", metrics)
        for entry in metrics.values():
            self.assertIn(entry["category"], ("methodology-adherence", "engineering-quality",
                                              "structure", "length", "cost"))

    def test_deterministic_and_location_independent(self):
        first = records.dumps(aggregate.aggregate(self.run_dir))
        self.assertEqual(records.dumps(aggregate.aggregate(self.run_dir)), first)
        moved = self.tmp / "deeper" / "elsewhere" / self.run_dir.name
        shutil.copytree(self.run_dir, moved)
        self.assertEqual(records.dumps(aggregate.aggregate(moved)), first)
        self.assertNotIn(str(self.tmp), first)
        self.assertNotIn("NaN", first)
        self.assertNotIn("Infinity", first)


class WriteAndCheckTest(FixtureTestCase):
    def test_write_then_check(self):
        run_dir = small_run(self.tmp)
        path = run_dir / "summary.json"
        ok, diff = aggregate.check_summary(run_dir)  # not written yet
        self.assertFalse(ok)
        self.assertIn("missing", diff)
        summary = aggregate.write_summary(run_dir)
        self.assertEqual(records.read_json(path), summary)
        self.assertEqual(aggregate.check_summary(run_dir), (True, ""))
        raw = path.read_bytes()
        self.assertTrue(raw.endswith(b"\n") and b"\r\n" not in raw)
        path.write_bytes(raw.replace(b"\n", b"\r\n"))  # a CRLF checkout is not drift
        self.assertEqual(aggregate.check_summary(run_dir), (True, ""))
        edited = json.loads(raw)
        edited["contrasts"][0]["mean_diff"] = 99.0
        path.write_bytes(json.dumps(edited, indent=2).encode("utf-8") + b"\n")
        ok, diff = aggregate.check_summary(run_dir)
        self.assertFalse(ok)
        self.assertIn("summary.json (committed)", diff)
        self.assertIn("99.0", diff)

    def test_stale_records_are_caught(self):
        run_dir = small_run(self.tmp)
        aggregate.write_summary(run_dir)
        path = next((run_dir / "judgments").glob("*engineering-quality-v1.j1.k1.json"))
        record = records.read_json(path)
        record["scores"][0]["score"] = (record["scores"][0]["score"] + 1) % 5
        records.write_json(path, record)
        self.assertFalse(aggregate.check_summary(run_dir)[0])


# --- a run with failures, repeats, two judges, caps and a missing arm ----------------------

class RichRunTest(SharedRun, unittest.TestCase):
    build = staticmethod(rich_run)

    def cell(self, prompt, condition):
        return find(self.summary["cells"], prompt=prompt, condition=condition)

    def test_counts_by_status(self):
        origin = self.summary["generated_from"]
        self.assertEqual(origin["generations"], {"ok": 17, "error": 3, "not_a_valid_record": 0})
        # 17 ok plans x 2 rubrics x (2 judges x 2 repeats) records, one error, one invalid.
        self.assertEqual(origin["judgments"], {"ok": 134, "error": 1, "invalid": 1,
                                               "not_a_valid_record": 0})
        self.assertEqual(origin["probes"], {"ok": 16, "error": 1, "invalid": 0,
                                            "not_a_valid_record": 0})
        self.assertEqual(origin["judgments_used"], 134)
        self.assertEqual(origin["blind_key_entries"], 34)

    def test_per_dimension_median_with_two_judges_and_two_repeats(self):
        # P1 baseline r1: four coders; the median of four is the mean of the middle two.
        medians = [(sorted(column)[1] + sorted(column)[2]) / 2
                   for column in zip(*BASELINE_P1_R1.values())]
        self.assertEqual(medians, [2, 1.5, 1, 1, 1, 1, 1, 1, 0.5, 0])
        self.assertEqual(sum(sum(s) for s in BASELINE_P1_R1.values()) / 4, 10.5)
        # r2: j1's first repeat errored, so r2 lacks one of the four configured judge x repeat
        # judgments and is left out of adherence (complete-case); its other three 12s are unused.
        adherence = self.cell("P1", "baseline")["metrics"]["adherence_total"]
        self.assertEqual((adherence["n"], adherence["min"], adherence["max"]),
                         (1, sum(medians), sum(medians)))
        mean = self.cell("P1", "baseline")["metrics"]["adherence_mean"]
        self.assertEqual((mean["n"], mean["min"], mean["max"]), (1, 10.5, 10.5))
        # EQ: r1's j1#2 judgment is invalid, so r1 is left out (averaging the other three, 24,
        # would weight j2 twice as much as j1); r2 = 20 from all four.
        eq = self.cell("P1", "baseline")["metrics"]["eq_total"]
        self.assertEqual((eq["n"], eq["min"], eq["max"]), (1, 20, 20))
        self.assertEqual(self.cell("P1", "baseline")["metrics"]["eq_dim_validation-quality"]["n"],
                         1)

    def test_incomplete_judging_is_flagged_noted_and_kept_per_judge(self):
        flags = self.summary["flags"]
        self.assertIn(left_out_flag(rb.ADHERENCE, "baseline", 1, 4, [("j1", 1)],
                                    ["P1.baseline.g1.r02"]), flags)
        self.assertIn(left_out_flag(rb.EQ, "baseline", 1, 4, [("j1", 1)],
                                    ["P1.baseline.g1.r01"]), flags)
        self.assertEqual(sum(f.startswith("incomplete judging") for f in flags), 2)
        self.assertEqual(self.summary["generated_from"]["incomplete_judging"], [
            {"rubric": rb.ADHERENCE, "condition": "baseline", "generator": "g1", "n_ok": 4,
             "n_left_out": 1, "lacking_judge": {"j1": 1}},
            {"rubric": rb.EQ, "condition": "baseline", "generator": "g1", "n_ok": 4,
             "n_left_out": 1, "lacking_judge": {"j1": 1}}])
        # Every contrast with baseline as an arm says so for the judged metrics, and only there.
        for treatment, control, arm in (("dfa", "baseline", "control"),
                                        ("generic-control", "baseline", "control"),
                                        ("dfa", "generic-control", None)):
            for metric, rubric in (("adherence_total", rb.ADHERENCE), ("eq_total", rb.EQ),
                                   ("eq_core", rb.EQ), ("eq_per_1k_tokens", rb.EQ),
                                   ("words", None), ("eq_total@j1", None)):
                notes = find(self.summary["contrasts"], treatment=treatment, control=control,
                             metric=metric)["notes"]
                lost = [n for n in notes if n.startswith("incomplete judging")]
                if arm and rubric:
                    self.assertEqual(lost, [left_out_note(rubric, control=(1, 4))],
                                     (treatment, control, metric))
                else:
                    self.assertEqual(lost, [], (treatment, control, metric))
        # The per-judge metric keeps every ok judgment of its judge: P1 baseline r1 has j1's
        # first repeat only (24), r2 both (20, 20); j2 scored r1 26 and 22, r2 20 and 20.
        for judge in ("j1", "j2"):
            stat = self.cell("P1", "baseline")["metrics"][f"eq_total@{judge}"]
            self.assertEqual((stat["n"], stat["min"], stat["max"]), (2, 20, 24))

    def test_failed_generations_are_counted_not_used(self):
        cell = self.cell("P2", "generic-control")
        self.assertEqual((cell["n_generations"], cell["n_failed"]), (0, 2))
        self.assertEqual(cell["metrics"]["eq_total"]["n"], 0)
        self.assertIsNone(cell["metrics"]["eq_total"]["mean"])
        dfa = self.cell("P2", "dfa")
        self.assertEqual((dfa["n_generations"], dfa["n_failed"]), (1, 1))
        self.assertIsNone(dfa["metrics"]["eq_total"]["sd"])  # one run: no spread

    def test_contrast_with_a_missing_arm(self):
        c = find(self.summary["contrasts"], treatment="dfa", control="generic-control",
                 metric="eq_total")
        self.assertEqual(c["n_prompts"], 1)
        self.assertEqual([row["prompt"] for row in c["per_prompt"]], ["P1"])
        # P1 dfa: r1 mean(40, 38, 42, 40) = 40, r2 mean(36, 36, 38, 38) = 37; control (30 + 32) / 2.
        self.assertEqual(c["mean_diff"], (40 + 37) / 2 - 31.0)
        self.assertIsNone(c["diff"]["ci95"])
        self.assertIsNone(c["dz"])
        self.assertIsNone(c["cluster_bootstrap_ci"])  # one cluster
        self.assertIn("missing an arm on: P2", c["notes"])
        self.assertIn("n_prompts < 5: interval is wide", c["notes"])

    def test_judge_on_the_generator_model_is_noted(self):
        adherence = find(self.summary["contrasts"], treatment="dfa", control="baseline",
                         metric="adherence_total")
        self.assertIn("judge model also generated these plans", adherence["notes"])
        self.assertIn("ceiling: 100% of treatment generations at max", adherence["notes"])
        words = find(self.summary["contrasts"], treatment="dfa", control="baseline",
                     metric="words")
        self.assertNotIn("judge model also generated these plans", words["notes"])
        self.assertTrue(any(f.startswith("judge j2 uses the generator's model gen-model")
                            for f in self.summary["flags"]))

    def test_flags_name_what_was_excluded_or_missing(self):
        flags = "\n".join(self.summary["flags"])
        self.assertIn("excluded 3 generation(s) with status error: P2.dfa.g1.r02, "
                      "P2.generic-control.g1.r01, P2.generic-control.g1.r02", flags)
        self.assertIn("excluded 1 judgment(s) with status error", flags)
        self.assertIn("excluded 1 judgment(s) with status invalid", flags)
        self.assertIn("excluded 1 probe(s) with status error", flags)
        self.assertIn("missing judgments: methodology-adherence-v1 by j1: 33 of 34", flags)
        self.assertIn("missing judgments: engineering-quality-v1 by j1: 33 of 34", flags)
        self.assertNotIn("by j2", flags)
        self.assertIn("missing probes: j1: 16 of 17", flags)
        self.assertIn("small n: 2 prompt(s)", flags)
        self.assertIn("small n: 2 run(s) per cell", flags)

    def test_cap_compliance(self):
        dfa = find(self.summary["length"], condition="dfa-capped")
        base = find(self.summary["length"], condition="baseline-capped")
        # cap 100 words, +10%: 100, 110, 90 are within; 111 is not. 120 and 115 are not.
        self.assertEqual((dfa["within_cap"], dfa["n"], dfa["cap_compliance"]), (3, 4, 0.75))
        self.assertEqual((base["within_cap"], base["n"], base["cap_compliance"]), (2, 4, 0.5))
        plain = find(self.summary["length"], condition="baseline")
        self.assertIsNone(plain["length_cap_words"])
        self.assertIsNone(plain["cap_compliance"])

    def test_cost_totals_with_unknown_costs(self):
        cost = self.summary["cost"]
        # Generations: 0.20 + 2 x 0.05 + 15 x 0.01 (default), two unknown (P1 dfa r2, P2 dfa r2).
        self.assertEqual(cost["generation"]["cost_usd"], r4(0.20 + 0.10 + 15 * 0.01))
        self.assertEqual((cost["generation"]["n_calls"], cost["generation"]["n_unknown_cost"]),
                         (20, 2))
        # Judgments: 136 records at 0.002 each, one with unknown cost.
        self.assertEqual(cost["judging"]["cost_usd"], r4(135 * 0.002))
        self.assertEqual(cost["judging"]["n_unknown_cost"], 1)
        self.assertEqual(cost["probe"]["cost_usd"], r4(17 * 0.001))
        self.assertEqual(cost["total"]["n_unknown_cost"], 3)
        self.assertEqual(cost["total"]["cost_usd"], r4(0.45 + 135 * 0.002 + 17 * 0.001))
        self.assertEqual(cost["total"]["n_calls"], 20 + 136 + 17)

    def test_reliability_has_four_coders(self):
        rel = find(self.summary["judge_reliability"], rubric="methodology-adherence-v1")
        self.assertEqual(rel["coders"], ["j1#1", "j1#2", "j2#1", "j2#2"])
        self.assertEqual((rel["n_coders"], rel["n_units"]), (4, 17))
        self.assertEqual([j["judge"] for j in rel["per_judge"]], ["j1", "j2"])
        self.assertEqual([j["n"] for j in rel["per_judge"]], [33, 34])
        self.assertTrue(-1 <= rel["alpha_interval"] <= 1)

    def test_leakage_counts_skill_and_plain_conditions(self):
        (entry,) = self.summary["leakage"]
        d = entry["skill_vs_plain"]
        # Skill: dfa (2 ok probes), generic-control (2), dfa-capped (4); plain: 8.
        self.assertEqual((d["n_positive"], d["n_negative"]), (8, 8))
        self.assertEqual((d["auc"], d["accuracy"]), (1.0, 1.0))
        self.assertEqual((entry["n_probes"], entry["n_ok"]), (17, 16))
        generic = find(entry["contrasts"], treatment="dfa", control="generic-control")
        self.assertEqual((generic["n_positive"], generic["n_negative"]), (2, 2))


# --- reliability worked out by hand -------------------------------------------------------

class ReliabilityByHandTest(FixtureTestCase):
    def test_krippendorff_alpha_two_judges_with_a_missing_rating(self):
        gens = [rb.gen("P1", "baseline", 1, adherence={"j1": 10, "j2": 12}),
                rb.gen("P1", "baseline", 2, adherence={"j1": 14, "j2": 14}),
                rb.gen("P1", "baseline", 3, adherence={"j1": 18, "j2": 15}),
                rb.gen("P1", "baseline", 4, adherence={"j1": 20, "j2": "invalid"})]
        run_dir = rb.make_run(
            self.tmp, gens, prompts=("P1",), conditions=("baseline",), runs_per_cell=4,
            rubrics=(rb.ADHERENCE,), contrasts=(),
            judges=[{"id": "j1", "provider": "fake", "model": "judge-a"},
                    {"id": "j2", "provider": "fake", "model": "judge-b"}])
        summary = self.summarize(run_dir)
        (rel,) = summary["judge_reliability"]
        # Units (j1, j2): (10, 12), (14, 14), (18, 15), (20, missing: not pairable).
        values = [10, 12, 14, 14, 18, 15]
        n = len(values)
        observed = (2 * (10 - 12) ** 2 + 0 + 2 * (18 - 15) ** 2) / n  # m_u - 1 = 1 per unit
        expected = sum((a - b) ** 2 for a in values for b in values) / (n * (n - 1))
        alpha = 1 - observed / expected
        self.assertAlmostEqual(alpha, 12 / 17, places=12)
        self.assertEqual(rel["alpha_interval"], r4(alpha))
        self.assertEqual(rel["mean_abs_diff"], r4((2 + 0 + 3) / 3))
        self.assertEqual((rel["n_units"], rel["n_coders"], rel["coders"]), (3, 2, ["j1#1", "j2#1"]))
        self.assertEqual(rel["per_judge"], [{"judge": "j1", "n": 4, "mean_total": 15.5},
                                            {"judge": "j2", "n": 3, "mean_total": r4(41 / 3)}])
        # Two judges per plan: the per-dimension median is the mean of the two totals: 11, 14,
        # 16.5. The fourth plan has no ok j2 judgment, so it is left out of adherence_total
        # (complete-case; its j1 rating of 20 alone would count j1 only), though its j1 rating
        # still counts for reliability above.
        adherence = summary["cells"][0]["metrics"]["adherence_total"]
        self.assertEqual((adherence["n"], adherence["min"], adherence["median"], adherence["max"]),
                         (3, 11.0, 14.0, 16.5))
        self.assertIn(left_out_flag(rb.ADHERENCE, "baseline", 1, 4, [("j2", 1)],
                                    ["P1.baseline.g1.r04"]), summary["flags"])
        # Zero probes and no probe configured: no leakage section, no probe flag.
        self.assertEqual(summary["leakage"], [])
        self.assertFalse(any("probe" in f for f in summary["flags"]))
        self.assertEqual(summary["contrasts"], [])
        self.assertNotIn("eq_total", summary["metrics"])


class SparseRunsTest(FixtureTestCase):
    def test_single_run_per_cell(self):
        gens = [rb.gen("P1", "dfa", 1, adherence={"j1": 20}, eq={"j1": 30}),
                rb.gen("P1", "baseline", 1, adherence={"j1": 12}, eq={"j1": 20}),
                rb.gen("P2", "dfa", 1, adherence={"j1": 19}, eq={"j1": 34}),
                rb.gen("P2", "baseline", 1, adherence={"j1": 10}, eq={"j1": 22})]
        run_dir = rb.make_run(self.tmp, gens, runs_per_cell=1, probe_judges=["j1"],
                              judges=[{"id": "j1", "provider": "fake", "model": "gen-model"}])
        summary = self.summarize(run_dir)
        cell = find(summary["cells"], prompt="P1", condition="dfa")
        self.assertEqual(cell["metrics"]["eq_total"]["n"], 1)
        self.assertIsNone(cell["metrics"]["eq_total"]["sd"])
        self.assertIsNone(cell["metrics"]["eq_total"]["ci95"])
        c = find(summary["contrasts"], treatment="dfa", control="baseline", metric="eq_total")
        # diffs 10, 12: mean 11, sd sqrt(2), df = 1.
        self.assertEqual(c["diff"]["ci95"], [r4(11 - T975[1]), r4(11 + T975[1])])
        self.assertEqual(c["dz"], r4(11 / math.sqrt(2)))
        # One plan per cell: no spread within a prompt, so the within-prompt g is undefined.
        self.assertIsNone(c["hedges_g"])
        self.assertIn("no variation within prompts: g undefined", c["notes"])
        self.assertIn("judge model also generated these plans", c["notes"])
        # Probe enabled, but no probe ran: the section says so instead of failing.
        (entry,) = summary["leakage"]
        self.assertEqual(entry["n_probes"], 0)
        self.assertIsNone(entry["skill_vs_plain"]["auc"])
        self.assertIsNone(entry["skill_vs_plain"]["accuracy"])
        self.assertIn("missing probes: j1: 0 of 4 expected are ok", summary["flags"])

    def test_run_with_no_judgments_lint_or_blind_key(self):
        gens = [rb.gen("P1", "dfa", 1), rb.gen("P1", "baseline", 1)]
        run_dir = rb.make_run(self.tmp, gens, prompts=("P1",), runs_per_cell=1, write_lint=False)
        shutil.rmtree(run_dir / "blind")
        summary = self.summarize(run_dir)
        c = find(summary["contrasts"], treatment="dfa", control="baseline",
                 metric="adherence_total")
        self.assertEqual(c["n_prompts"], 0)
        self.assertIsNone(c["mean_diff"])
        self.assertIn("no prompt has data for both arms", c["notes"])
        words = find(summary["contrasts"], treatment="dfa", control="baseline", metric="words")
        self.assertEqual(words["mean_diff"], 0.0)
        flags = summary["flags"]
        self.assertIn("no ok judgments: every judge-based metric is n/a", flags)
        self.assertIn("lint missing for 2 ok generation(s)", flags)
        self.assertEqual(summary["generated_from"]["blind_key_entries"], 0)
        rel = find(summary["judge_reliability"], rubric="methodology-adherence-v1")
        self.assertEqual((rel["n_coders"], rel["n_units"]), (0, 0))
        self.assertIsNone(rel["alpha_interval"])

    def test_orphan_and_unreadable_records_are_skipped_and_flagged(self):
        gens = [rb.gen("P1", "dfa", 1, adherence={"j1": 20}),
                rb.gen("P1", "baseline", 1, adherence={"j1": 12})]
        run_dir = rb.make_run(self.tmp, gens, prompts=("P1",), runs_per_cell=1,
                              rubrics=(rb.ADHERENCE,))
        (run_dir / "judgments" / "broken.json").write_bytes(b"{not json")
        (run_dir / "probes").mkdir(exist_ok=True)
        (run_dir / "probes" / "wrong-shape.json").write_bytes(b'{"schema": "dfa-eval/probe@1"}\n')
        key = records.read_json(run_dir / "blind" / "key.json")
        dropped = next(e for e in key["entries"] if e["gen_id"] == "P1.dfa.g1.r01"
                       and e["variant"] == "plain")
        key["entries"].remove(dropped)
        records.write_json(run_dir / "blind" / "key.json", key)
        summary = aggregate.aggregate(run_dir)
        self.assertEqual(schema.validate(summary, schemas.SUMMARY), [])
        flags = "\n".join(summary["flags"])
        self.assertIn("skipped 1 judgments file(s) that are not valid records: broken.json", flags)
        self.assertIn("skipped 1 probes file(s) that are not valid records: wrong-shape.json",
                      flags)
        self.assertIn("whose blind id does not lead to an ok generation", flags)
        self.assertEqual(summary["generated_from"]["judgments"]["not_a_valid_record"], 1)
        cell = find(summary["cells"], prompt="P1", condition="dfa")
        self.assertEqual(cell["metrics"]["adherence_total"]["n"], 0)
        # The orphan is an ok judgment, but it is not used.
        (judge,) = summary["run"]["judges"]
        self.assertEqual((judge["n_ok"], judge["n_used"], judge["n_records"]), (2, 1, 2))


# --- complete-case judged metrics -----------------------------------------------------------

TWO_JUDGES = [{"id": "ja", "provider": "fake", "model": "judge-a"},
              {"id": "jb", "provider": "fake", "model": "judge-b"}]


class CompleteCaseTest(FixtureTestCase):
    def test_judge_failures_on_one_arm_do_not_shift_its_headline_mean(self):
        # No true effect: judge ja scores every plan 33 on EQ (3 per dimension), the stricter jb
        # 22 (2 per dimension). jb's EQ judgment of runs 1-2 of the four dfa plans per prompt is
        # invalid. Averaging whichever judges succeeded scored those plans 33 instead of 27.5:
        # dfa - baseline = (33 + 33 + 27.5 + 27.5) / 4 - 27.5 = +2.75 on every prompt.
        gens = []
        for p in ("P1", "P2"):
            for r in (1, 2, 3, 4):
                gens.append(rb.gen(p, "dfa", r, adherence={"ja": 20, "jb": 18},
                                   eq={"ja": 33, "jb": "invalid" if r <= 2 else 22}))
                gens.append(rb.gen(p, "baseline", r, adherence={"ja": 10, "jb": 12},
                                   eq={"ja": 33, "jb": 22}))
        run_dir = rb.make_run(self.tmp, gens, runs_per_cell=4, judges=TWO_JUDGES)
        summary = self.summarize(run_dir)

        def contrast(metric):
            return find(summary["contrasts"], treatment="dfa", control="baseline", metric=metric)

        # Complete-case: dfa runs 3-4 only, (33 + 22) / 2 = 27.5 like every baseline plan.
        eq = contrast("eq_total")
        self.assertEqual([row["diff"] for row in eq["per_prompt"]], [0.0, 0.0])
        self.assertEqual((eq["mean_diff"], eq["n_treatment"], eq["n_control"]), (0.0, 4, 8))
        self.assertIn(left_out_note(rb.EQ, treatment=(4, 8)), eq["notes"])
        for metric in ("eq_core", "eq_per_1k_tokens", "checklist_coverage", "traps_present"):
            self.assertEqual(contrast(metric)["n_treatment"], 4, metric)
            self.assertIn(left_out_note(rb.EQ, treatment=(4, 8)), contrast(metric)["notes"])
        self.assertEqual(contrast("eq_core")["mean_diff"], 0.0)
        # Each judge alone, over every plan it scored: no difference either, and nothing lost.
        for judge, n_treatment in (("ja", 8), ("jb", 4)):
            alone = contrast(f"eq_total@{judge}")
            self.assertEqual((alone["mean_diff"], alone["n_treatment"], alone["n_control"]),
                             (0.0, n_treatment, 8))
            self.assertFalse(any(n.startswith("incomplete") for n in alone["notes"]))
        # Adherence was scored by both judges everywhere: (20 + 18) / 2 - (10 + 12) / 2.
        adherence = contrast("adherence_total")
        self.assertEqual((adherence["mean_diff"], adherence["n_treatment"]), (8.0, 8))
        self.assertFalse(any(n.startswith("incomplete") for n in adherence["notes"]))
        self.assertFalse(any(n.startswith("incomplete") for n in contrast("words")["notes"]))

        cell = find(summary["cells"], prompt="P1", condition="dfa")["metrics"]
        self.assertEqual((cell["eq_total"]["n"], cell["eq_total"]["mean"]), (2, 27.5))
        self.assertEqual((cell["eq_total@ja"]["n"], cell["eq_total@jb"]["n"]), (4, 2))
        self.assertEqual((cell["adherence_total"]["n"], cell["words"]["n"]), (4, 4))
        dfa = find(summary["conditions"], condition="dfa")
        self.assertEqual((dfa["n_generations"], dfa["metrics"]["eq_total"]["pooled"]["n"]), (8, 4))
        self.assertEqual(summary["generated_from"]["incomplete_judging"], [
            {"rubric": rb.EQ, "condition": "dfa", "generator": "g1", "n_ok": 8, "n_left_out": 4,
             "lacking_judge": {"jb": 4}}])
        flags = summary["flags"]
        self.assertIn(left_out_flag(rb.EQ, "dfa", 4, 8, [("jb", 4)],
                                    ["P1.dfa.g1.r01", "P1.dfa.g1.r02", "P2.dfa.g1.r01",
                                     "P2.dfa.g1.r02"]), flags)
        self.assertEqual(sum(f.startswith("incomplete judging") for f in flags), 1)
        self.assertIn("missing judgments: engineering-quality-v1 by jb: 12 of 16 expected "
                      "(ok generations x repeats) are ok", flags)
        # Every usable ok judgment still counts as used (per-judge metrics, reliability).
        self.assertEqual(summary["generated_from"]["judgments_used"], 16 * 2 + 16 + 12)
        rel = find(summary["judge_reliability"], rubric=rb.EQ)
        self.assertEqual([j["n"] for j in rel["per_judge"]], [16, 12])
        self.assertEqual(records.dumps(aggregate.aggregate(run_dir)), records.dumps(summary))

    def test_rubric_subsets_and_repeats_set_the_judgments_a_plan_needs(self):
        # j2 scores engineering quality only (like fable-5 in pilot-core); one repeat per judge.
        gens = [
            rb.gen("P1", "dfa", 1, adherence={"j1": 20}, eq={"j1": 30, "j2": 34}),
            # A stray j2 adherence judgment: j2 is not asked for that rubric, so it is not used.
            rb.gen("P1", "baseline", 1, adherence={"j1": 12, "j2": 0}, eq={"j1": 20, "j2": 24}),
            # No j2 judgment of engineering quality: left out of EQ, still counted for adherence.
            rb.gen("P2", "dfa", 1, adherence={"j1": 18}, eq={"j1": 32}),
            # A second repeat although judge_repeats is 1: not used either.
            rb.gen("P2", "baseline", 1, adherence={"j1": 10, "j1#2": 20}, eq={"j1": 22, "j2": 26}),
        ]
        run_dir = rb.make_run(self.tmp, gens, runs_per_cell=1, judges=[
            {"id": "j1", "provider": "fake", "model": "judge-a"},
            {"id": "j2", "provider": "fake", "model": "judge-b", "rubrics": [rb.EQ]}])
        summary = self.summarize(run_dir)

        def contrast(metric):
            return find(summary["contrasts"], treatment="dfa", control="baseline", metric=metric)

        # j1 alone on adherence: 20 - 12 and 18 - 10. The stray 0 and the extra repeat's 20
        # would have made the baselines (12 + 0) / 2 = 6 and (10 + 20) / 2 = 15.
        adherence = contrast("adherence_total")
        self.assertEqual([row["diff"] for row in adherence["per_prompt"]], [8.0, 8.0])
        self.assertFalse(any(n.startswith("incomplete") for n in adherence["notes"]))
        # EQ needs j1 and j2: P1 (30 + 34) / 2 - (20 + 24) / 2 = 10; P2 has no complete dfa plan.
        eq = contrast("eq_total")
        self.assertEqual(([row["prompt"] for row in eq["per_prompt"]], eq["mean_diff"]),
                         (["P1"], 10.0))
        self.assertIn("missing an arm on: P2", eq["notes"])
        self.assertIn(left_out_note(rb.EQ, treatment=(1, 2)), eq["notes"])
        # j1's own EQ keeps P2's dfa plan: (30 - 20 + 32 - 22) / 2.
        self.assertEqual((contrast("eq_total@j1")["n_prompts"], contrast("eq_total@j1")["mean_diff"]),
                         (2, 10.0))
        flags = summary["flags"]
        unasked = sorted(p.stem for p in (run_dir / "judgments").glob("*.json")
                         if p.stem.endswith((f".{rb.ADHERENCE}.j2.k1", f".{rb.ADHERENCE}.j1.k2")))
        self.assertEqual(len(unasked), 2)
        self.assertIn("excluded 2 judgment(s) from a rubric, judge or repeat that the config does "
                      f"not ask for: {', '.join(unasked)}", flags)
        self.assertIn(left_out_flag(rb.EQ, "dfa", 1, 2, [("j2", 1)], ["P2.dfa.g1.r01"]), flags)
        self.assertEqual(sum(f.startswith("incomplete judging") for f in flags), 1)
        self.assertIn("missing judgments: engineering-quality-v1 by j2: 3 of 4", "\n".join(flags))
        self.assertFalse(any(f"{rb.ADHERENCE} by j2" in f for f in flags))
        self.assertEqual(find(summary["judge_reliability"], rubric=rb.ADHERENCE)["coders"],
                         ["j1#1"])
        j2 = find(summary["run"]["judges"], id="j2")
        self.assertEqual((j2["n_ok"], j2["n_used"], j2["n_records"]), (4, 3, 4))


# --- judgments and probes of an earlier blind copy -------------------------------------------

class OutdatedBlindCopyTest(FixtureTestCase):
    def setUp(self):
        super().setUp()
        field = blind_sha_field()
        field.__enter__()
        self.addCleanup(field.__exit__, None, None, None)
        gens = [rb.gen(p, c, 1, adherence={"j1": a}, eq={"j1": e}, probes={"j1": q})
                for p, c, a, e, q in (("P1", "dfa", 20, 30, 0.9), ("P1", "baseline", 12, 21, 0.2),
                                      ("P2", "dfa", 19, 33, 0.8), ("P2", "baseline", 10, 25, 0.3))]
        self.run_dir = rb.make_run(self.tmp, gens, runs_per_cell=1, probe_judges=["j1"])
        self.key = records.read_json(self.run_dir / "blind" / "key.json")["entries"]

    def blind_id(self, gid, variant):
        (entry,) = [e for e in self.key if e["gen_id"] == gid and e["variant"] == variant]
        return entry["blind_id"]

    def stamp(self, earlier=()):
        """Record blind_sha256 in every judgment and probe: the key's sha256, except for the ids
        in `earlier`, which get the sha256 of some other (earlier) text."""
        sha = {e["blind_id"]: e["sha256"] for e in self.key}
        for folder in ("judgments", "probes"):
            for path in sorted((self.run_dir / folder).glob("*.json")):
                record = records.read_json(path)
                record["blind_sha256"] = (hashlib.sha256(b"an earlier blind copy").hexdigest()
                                          if path.stem in earlier else sha[record["blind_id"]])
                records.write_json(path, record)

    def test_records_of_the_current_blind_copy_are_used_as_before(self):
        before = records.dumps(aggregate.aggregate(self.run_dir))
        self.stamp()
        self.assertEqual(records.dumps(self.summarize(self.run_dir)), before)

    def test_records_of_an_earlier_blind_copy_are_excluded_and_flagged(self):
        bid = self.blind_id("P1.dfa.g1.r01", "neutralized")
        judgment, probe = f"{bid}.{rb.EQ}.j1.k1", f"{bid}.probe.j1"
        self.assertTrue((self.run_dir / "judgments" / f"{judgment}.json").is_file())
        self.assertTrue((self.run_dir / "probes" / f"{probe}.json").is_file())
        self.stamp(earlier={judgment, probe})
        summary = self.summarize(self.run_dir)
        flags = summary["flags"]
        self.assertIn("excluded 1 ok judgment(s) of an earlier version of their blind copy "
                      "(blind_sha256 differs from the sha256 in blind/key.json; judge them "
                      f"again): {judgment}", flags)
        self.assertIn("excluded 1 ok probe(s) of an earlier version of their blind copy "
                      "(blind_sha256 differs from the sha256 in blind/key.json; probe them "
                      f"again): {probe}", flags)
        # P1's dfa plan lost its only EQ judgment: only P2 pairs, 33 - 25.
        eq = find(summary["contrasts"], treatment="dfa", control="baseline", metric="eq_total")
        self.assertEqual(([row["prompt"] for row in eq["per_prompt"]], eq["mean_diff"]),
                         (["P2"], 8.0))
        self.assertIn(left_out_flag(rb.EQ, "dfa", 1, 2, [("j1", 1)], ["P1.dfa.g1.r01"]), flags)
        # Its adherence judgment scored the current plain copy: 20 - 12 and 19 - 10.
        adherence = find(summary["contrasts"], treatment="dfa", control="baseline",
                         metric="adherence_total")
        self.assertEqual((adherence["n_prompts"], adherence["mean_diff"]), (2, 8.5))
        # Leakage without that probe: skill 0.8 against plain 0.2 and 0.3.
        (entry,) = summary["leakage"]
        self.assertEqual((entry["n_probes"], entry["n_ok"]), (3, 3))
        d = entry["skill_vs_plain"]
        self.assertEqual((d["n_positive"], d["n_negative"], d["auc"]), (1, 2, 1.0))
        self.assertIn("missing probes: j1: 3 of 4 expected are ok", flags)
        # Still counted as ok records, but not used anywhere.
        origin = summary["generated_from"]
        self.assertEqual((origin["judgments"]["ok"], origin["judgments_used"]), (8, 7))
        self.assertEqual(origin["probes"]["ok"], 4)
        (judge,) = summary["run"]["judges"]
        self.assertEqual((judge["n_ok"], judge["n_used"]), (8, 7))
        self.assertEqual(find(summary["judge_reliability"], rubric=rb.EQ)["per_judge"][0]["n"], 3)
        self.assertEqual(records.dumps(aggregate.aggregate(self.run_dir)), records.dumps(summary))


# --- the cost of calls that a retry replaced -------------------------------------------------

class SupersededCostTest(FixtureTestCase):
    def test_superseded_records_count_in_the_cost_block_only(self):
        run_dir = small_run(self.tmp)
        before = self.summarize(run_dir)  # no superseded/ directory: an empty block
        self.assertEqual(before["cost"]["superseded"], {
            "n_calls": 0, "cost_usd": 0.0, "n_unknown_cost": 0, "n_unknown_usage": 0,
            "input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})
        (run_dir / "superseded").mkdir()
        self.assertEqual(aggregate.aggregate(run_dir), before)  # an empty folder: nothing

        # A paid generation that failed, then an ok judgment with other scores (it must not be
        # used) and an errored one of unknown cost, both replaced by retries; a raw file; and
        # three JSON files that are not call records.
        gen = rb.supersede(run_dir, run_dir / "generations" / "P1.dfa.g1.r01.json",
                           status="error", error="fixture: the CLI exited 1", output_file=None,
                           output_sha256=None, metrics=None, cost_usd=0.5)
        jpath = next((run_dir / "judgments").glob(f"*.{rb.EQ}.j1.k1.json"))
        judged = rb.supersede(run_dir, jpath, scores=[
            dict(s, score=0) for s in records.read_json(jpath)["scores"]], cost_usd=0.25)
        rb.supersede(run_dir, jpath, n=2, status="error", error="fixture: timed out", scores=[],
                     checklist=None, traps=None, note=None, cost_usd=None, usage=None)
        records.write_text(run_dir / "superseded" / "raw" / "gen-P1.dfa.g1.r01.1.txt", "raw\n")
        (run_dir / "superseded" / "notes.json").write_text("{not json", encoding="utf-8")
        shutil.copyfile(run_dir / "lint" / "P1.dfa.g1.r01.json",
                        run_dir / "superseded" / "lint-copy.json")
        records.write_json(run_dir / "superseded" / "judgments" / "odd.json",
                           {"schema": [schemas.JUDGMENT["properties"]["schema"]["const"]],
                            "cost_usd": 9.0})

        summary = self.summarize(run_dir)
        cost = summary["cost"]
        replaced = cost["superseded"]
        self.assertEqual((replaced["n_calls"], replaced["cost_usd"], replaced["n_unknown_cost"],
                          replaced["n_unknown_usage"]), (3, 0.75, 1, 1))
        usage = [records.read_json(path)["usage"] for path in (gen, judged)]
        self.assertEqual((replaced["input_tokens"], replaced["output_tokens"]),
                         (sum(u["input_tokens"] for u in usage),
                          sum(u["output_tokens"] for u in usage)))
        for block in ("generation", "judging", "probe"):
            self.assertEqual(cost[block], before["cost"][block], block)
        total, was = cost["total"], before["cost"]["total"]
        self.assertEqual(total["cost_usd"], r4(4 * 0.01 + 8 * 0.002 + 0.75))
        self.assertEqual((total["n_calls"], total["n_unknown_cost"], total["input_tokens"]),
                         (was["n_calls"] + 3, was["n_unknown_cost"] + 1,
                          was["input_tokens"] + replaced["input_tokens"]))
        # Nothing else reads them: identical metrics, counts and statistics, one more flag (first,
        # with the other files skipped while loading).
        self.assertEqual({k: v for k, v in summary.items() if k not in ("cost", "flags")},
                         {k: v for k, v in before.items() if k not in ("cost", "flags")})
        self.assertEqual(summary["flags"], [
            "skipped 3 file(s) under superseded/ that are not valid generation, judgment or "
            "probe records (their cost is not counted): judgments/odd.json, lint-copy.json, "
            "notes.json"] + before["flags"])
        self.assertEqual(records.dumps(aggregate.aggregate(run_dir)), records.dumps(summary))
        # A superseded/ that is a file, not a folder, is no folder.
        shutil.rmtree(run_dir / "superseded")
        (run_dir / "superseded").write_text("not a folder\n", encoding="utf-8")
        self.assertEqual(aggregate.aggregate(run_dir), before)


if __name__ == "__main__":
    unittest.main()
