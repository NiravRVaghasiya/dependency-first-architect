"""The README evidence block is generated from committed run summaries and checked in CI.
Standard library only:  python -m unittest discover -s tests -v
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))
from dfa_eval import evidence  # noqa: E402


def contrast(metric, mean, lo, hi, n=8):
    return {"treatment": "dfa", "control": "baseline", "generator": "opus-5.5", "metric": metric,
            "n_prompts": n, "n_treatment": 5, "n_control": 5, "mean_diff": mean,
            "diff": {"ci95": [lo, hi]}}


SUMMARY = {
    "run": {"config_name": "pilot-core", "runs_per_cell": 5,
            "generators": [{"model_requested": "claude-opus-5-5", "effort": "high"}],
            "judges": [{"model_requested": "claude-sonnet-5-5"}]},
    "contrasts": [contrast("adherence_total", 7.25, 5.5, 9.0),
                  dict(contrast("eq_total", -0.5, -2.0, 1.0),
                       notes=["judge model also generated these plans"]),
                  contrast("words", 2800.0, 2100.0, 3500.0)],
    "flags": ["small n: 1 run(s) per cell (< 3)"],
    "leakage": [{"judge": "sonnet-5.5", "contrasts": [
        {"treatment": "dfa", "control": "baseline", "auc": 0.97}]}],
    "judge_reliability": [{"rubric": "engineering-quality-v1", "n_coders": 2,
                           "alpha_interval": 0.41}],
    "cost": {"total": {"cost_usd": 123.456}},
}
OUTCOMES = {
    "task": "eval/outcomes/tasks/webhook-ledger", "planner": {"model": "claude-opus-5-5"},
    "implementer": {"model": "claude-sonnet-5-5"}, "attempts": {"ok": 20},
    "contrasts": [{"treatment": "dfa-plan", "control": "baseline-plan",
                   "metric": "round1_pass_rate", "n_treatment": 5, "n_control": 5,
                   "mean_diff": 0.06, "bootstrap_ci95": [-0.02, 0.15]}],
}


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.results = results = self.root / "eval" / "results"
        plans = results / "2026-10-03-pilot-core"
        plans.mkdir(parents=True)
        (plans / "manifest.json").write_text("{}", encoding="utf-8")
        (plans / "summary.json").write_text(json.dumps(SUMMARY), encoding="utf-8")
        outcomes = results / "2026-10-03-outcomes"
        (outcomes / "outcomes").mkdir(parents=True)
        (outcomes / "outcomes" / "config.json").write_text("{}", encoding="utf-8")
        (outcomes / "outcomes-summary.json").write_text(json.dumps(OUTCOMES), encoding="utf-8")
        (self.root / "README.md").write_text(
            f"# Title\n\nIntro.\n\n{evidence.BEGIN}\nstale\n{evidence.END}\n\nAfter.\n",
            encoding="utf-8")

    def test_block_quotes_the_summaries(self):
        block = evidence.render(self.root)
        self.assertIn("| dfa − baseline | opus-5.5 | 8 | 5 vs 5 | +7.2 [+5.5, +9.0] | "
                      "−0.5 [−2.0, +1.0] |", block)
        self.assertIn("- caveats, dfa − baseline (opus-5.5): judge model also generated these "
                      "plans (EQ)", block)
        self.assertIn("- flag: small n: 1 run(s) per cell (< 3)", block)
        self.assertIn("+2800 [+2100, +3500]", block)
        self.assertIn("dfa vs baseline 0.97", block)
        self.assertIn("(Krippendorff α, 2 coders): 0.41", block)
        self.assertIn("recorded cost $123.46", block)
        self.assertIn("| dfa-plan − baseline-plan | 5 vs 5 | +0.06 [−0.02, +0.15] |", block)
        for word in ("winner", "best", "beats", "proves"):
            self.assertNotIn(word, block.lower())

    def test_write_then_check(self):
        ok, _ = evidence.write_or_check(self.root, check=True)
        self.assertFalse(ok)  # the block says "stale"
        ok, _ = evidence.write_or_check(self.root)
        self.assertTrue(ok)
        text = (self.root / "README.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Title\n\nIntro.\n\n"))
        self.assertTrue(text.endswith(f"{evidence.END}\n\nAfter.\n"))
        self.assertTrue(evidence.write_or_check(self.root, check=True)[0])
        # A changed summary makes the committed block stale.
        changed = dict(SUMMARY, contrasts=[contrast("adherence_total", 3.0, 1.0, 5.0)])
        (self.root / "eval" / "results" / "2026-10-03-pilot-core" / "summary.json").write_text(
            json.dumps(changed), encoding="utf-8")
        self.assertFalse(evidence.write_or_check(self.root, check=True)[0])

    def test_only_committed_runs_are_quoted(self):
        # A summary with no manifest is not a committed run, whatever its numbers say.
        loose = self.results / "2026-10-04-loose"
        loose.mkdir()
        (loose / "summary.json").write_text(json.dumps(SUMMARY), encoding="utf-8")
        # An outcome run is quoted from its outcome summary, even beside a plan summary.
        (self.results / "2026-10-03-outcomes" / "summary.json").write_text(
            json.dumps(SUMMARY), encoding="utf-8")
        block = evidence.render(self.root)
        self.assertNotIn("2026-10-04-loose", block)
        self.assertEqual(block.count("| dfa − baseline | opus-5.5 |"), 1)
        self.assertIn("| dfa-plan − baseline-plan | 5 vs 5 |", block)

    def test_missing_markers_fail_loudly(self):
        (self.root / "README.md").write_text("# no markers\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            evidence.write_or_check(self.root, check=True)

    def test_no_runs(self):
        shutil.rmtree(self.root / "eval" / "results")
        self.assertIn("No committed runs yet.", evidence.render(self.root))


if __name__ == "__main__":
    unittest.main()
