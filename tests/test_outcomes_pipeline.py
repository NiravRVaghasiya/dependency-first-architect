"""End-to-end test of the outcome-benchmark orchestration with fake models (no network, no LLM).

The fake implementer copies a known implementation into its sandbox, so the hidden tests'
results, the rework diff, and the summary are predictable. Standard library only:
    python -m unittest discover -s tests -v
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))
from dfa_eval import outcomes, providers, records, schema, schemas  # noqa: E402

TASK = "eval/outcomes/tasks/webhook-ledger"


def config(tmp, copy_from, runs=2):
    data = {
        "schema": "dfa-eval/outcomes-config@1",
        "name": "pipeline-test",
        "seed": 7,
        "task": TASK,
        "planner": {"id": "fake-planner", "provider": "fake"},
        "implementer": {"id": "fake-implementer", "provider": "fake",
                        "options": {"agent_copy_from": copy_from}},
        "conditions": {
            "baseline": {"kind": "plain", "description": "no instructions"},
            "dfa": {"kind": "skill", "description": "the skill", "skill_dir": ".",
                    "skill_name": "dependency-first-architect"},
        },
        "arms": {"no-plan": None, "baseline-plan": "baseline", "dfa-plan": "dfa"},
        "runs_per_arm": runs,
        # jobs=1 so this test does not spawn competing child interpreters of its own, and a
        # generous test_timeout_s so a child merely starved of CPU on a loaded CI runner still
        # finishes (the hidden tests run in well under a second): contention must not turn a
        # correct run into a spurious timeout and a sub-1.0 pass rate.
        "limits": {"jobs": 1, "test_timeout_s": 300},
    }
    path = Path(tmp) / "outcomes.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class OutcomePipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.run_dir = self.tmp / "run-outcomes"
        self.work = self.tmp / "work"
        self.log = []

    def run_all(self, copy_from, runs=2):
        path = config(self.tmp, copy_from, runs)
        ocfg = outcomes.load_outcomes_config(path, ROOT)
        outcomes.run_plan(self.run_dir, ocfg, path, repo_root=ROOT, work_root=self.work / "plan",
                          log=self.log.append)
        counts = outcomes.run_implement(self.run_dir, ocfg, allow_code_execution=True,
                                        repo_root=ROOT, work_root=self.work / "impl",
                                        log=self.log.append)
        return ocfg, counts

    def test_refuses_without_the_code_execution_flag(self):
        path = config(self.tmp, [f"{TASK}/reference/round1"])
        ocfg = outcomes.load_outcomes_config(path, ROOT)
        with self.assertRaises(outcomes.OutcomeError) as caught:
            outcomes.run_implement(self.run_dir, ocfg, repo_root=ROOT)
        self.assertIn("--allow-code-execution", str(caught.exception))
        self.assertFalse((self.run_dir / outcomes.OUTCOMES).exists())

    def test_reference_implementation_end_to_end(self):
        _, counts = self.run_all([f"{TASK}/reference/round1", f"{TASK}/reference/round2"])
        self.assertEqual(counts["ok"], 6)
        attempts = outcomes.load_attempts(self.run_dir)
        self.assertEqual(len(attempts), 6)
        for record in attempts:
            self.assertEqual(schema.validate(record, schemas.OUTCOME_ATTEMPT), [])
            self.assertEqual([r["status"] for r in record["rounds"]], ["ok", "ok"])
            snapshot = self.run_dir / outcomes.OUTCOMES / record["attempt_id"] / "round2"
            self.assertTrue((snapshot / "ledger" / "service.py").is_file())
            diff = record["rounds"][1]["diff_from_previous"]
            self.assertGreater(diff["lines_added"] + diff["lines_removed"], 0)
            if record["arm"] == "no-plan":
                self.assertIsNone(record["plan_gen_id"])
            else:
                self.assertTrue(record["plan_gen_id"].startswith("webhook-ledger."))
        summary = outcomes.aggregate(self.run_dir)
        for arm in summary["arms"]:
            self.assertEqual(arm["n"], 2)
            for metric in ("round1_pass_rate", "change_request_pass_rate", "regression_pass_rate"):
                self.assertEqual(arm["metrics"][metric]["mean"], 1.0, (arm["arm"], metric))
        pairs = {(c["treatment"], c["control"], c["metric"]) for c in summary["contrasts"]}
        self.assertIn(("dfa-plan", "baseline-plan", "round1_pass_rate"), pairs)
        self.assertTrue(all(c["notes"] for c in summary["contrasts"]))  # n=2: descriptive only

    def test_summary_and_report_are_deterministic_and_checked(self):
        self.run_all([f"{TASK}/reference/round1", f"{TASK}/reference/round2"], runs=1)
        self.assertTrue(outcomes.write_or_check(self.run_dir))
        first = (self.run_dir / outcomes.SUMMARY).read_bytes()
        self.assertTrue(outcomes.write_or_check(self.run_dir, check=True))
        self.assertEqual(first, (self.run_dir / outcomes.SUMMARY).read_bytes())
        report = (self.run_dir / outcomes.REPORT).read_text(encoding="utf-8")
        for word in ("winner", "best", "beats", "proves"):
            self.assertNotIn(word, report.lower())
        # An edited attempt record no longer matches the committed summary.
        record_path = next((self.run_dir / outcomes.OUTCOMES).glob("*.json"))
        if record_path.name == "config.json":
            record_path = sorted((self.run_dir / outcomes.OUTCOMES).glob("*.r01.json"))[0]
        record = records.read_json(record_path)
        record["rounds"][0]["tests"]["by_category"]["functional"]["passed"] -= 1
        records.write_json(record_path, record)
        self.assertFalse(outcomes.write_or_check(self.run_dir, check=True))

    def test_a_retry_keeps_the_failed_attempt_and_its_cost(self):
        copy_from = [f"{TASK}/reference/round1", f"{TASK}/reference/round2"]
        path = config(self.tmp, copy_from, runs=1)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["arms"] = {"no-plan": None, "baseline-plan": "baseline"}
        data["implementer"]["options"].update(cost_usd=0.5)
        path.write_text(json.dumps(data), encoding="utf-8")
        ocfg = outcomes.load_outcomes_config(path, ROOT)
        outcomes.run_plan(self.run_dir, ocfg, path, repo_root=ROOT, work_root=self.work / "plan",
                          log=self.log.append)
        # A transient failure: every round-2 session fails on the first pass only.
        real = providers.FakeProvider.agent

        def flaky(provider, prompt, *args, **kwargs):
            result = real(provider, prompt, *args, **kwargs)
            if "CHANGE_REQUEST.md" in prompt:
                result.error = "transient failure"
            return result

        with mock.patch.object(providers.FakeProvider, "agent", flaky):
            counts = outcomes.run_implement(self.run_dir, ocfg, allow_code_execution=True,
                                            repo_root=ROOT, work_root=self.work / "a",
                                            log=self.log.append)
        self.assertEqual(counts["error"], 2)  # round 2 failed in both arms
        attempt_id = outcomes.load_attempts(self.run_dir)[0]["attempt_id"]
        counts = outcomes.run_implement(self.run_dir, ocfg, allow_code_execution=True,
                                        retry_failed=True, repo_root=ROOT,
                                        work_root=self.work / "b", log=self.log.append)
        self.assertEqual(counts["ok"], 2)
        kept = self.run_dir / "superseded" / outcomes.OUTCOMES
        old = records.read_json(kept / f"{attempt_id}.1.json")
        self.assertEqual([r["status"] for r in old["rounds"]], ["ok", "error"])
        self.assertTrue((kept / f"{attempt_id}.1" / "round1" / "ledger" / "service.py").is_file())
        raw = old["rounds"][0]["implementer"]["raw_file"]
        self.assertTrue(raw.startswith("superseded/raw/") and (self.run_dir / raw).is_file(), raw)
        summary = outcomes.aggregate(self.run_dir)
        self.assertEqual(summary["attempts"]["superseded"], 2)
        self.assertEqual([arm["n_superseded"] for arm in summary["arms"]], [1, 1])
        self.assertTrue(any(f.startswith(f"re-run attempt {attempt_id}") for f in summary["flags"]))
        self.assertEqual(summary["cost"]["superseded_implementer_usd"], 2.0)  # 2 attempts x 2 calls
        self.assertEqual(summary["cost"]["implementer_usd"], 2.0)
        self.assertTrue(outcomes.write_or_check(self.run_dir))

    def test_implement_refuses_another_config_and_an_unpinned_run(self):
        self.run_all([f"{TASK}/reference/round1", f"{TASK}/reference/round2"], runs=1)
        path = config(self.tmp, [f"{TASK}/reference/round1"], runs=1)  # another implementer
        other = outcomes.load_outcomes_config(path, ROOT)
        with self.assertRaises(outcomes.OutcomeError) as caught:
            outcomes.run_implement(self.run_dir, other, allow_code_execution=True,
                                   repo_root=ROOT, work_root=self.work / "x")
        self.assertIn("different outcomes config", str(caught.exception))
        (self.run_dir / outcomes.OUTCOMES / outcomes.TASK_FILES).unlink()
        ocfg = records.read_json(self.run_dir / outcomes.OUTCOMES / "config.json")
        with self.assertRaises(outcomes.OutcomeError) as caught:
            outcomes.pin_task(records.RunDir(self.run_dir), ocfg, ROOT)
        self.assertIn("task version they ran against is unknown", str(caught.exception))

    def test_a_defect_shows_up_in_the_round1_category(self):
        self.run_all([f"{TASK}/mutants/non_atomic", f"{TASK}/reference/round2"], runs=1)
        summary = outcomes.aggregate(self.run_dir)
        for arm in summary["arms"]:
            self.assertLess(arm["metrics"]["round1_pass_rate"]["mean"], 1.0)
            self.assertEqual(arm["metrics"]["r1_failure-injection"]["mean"], 0.0)
            self.assertEqual(arm["metrics"]["r1_functional"]["mean"], 1.0)
        # The summary says which hidden tests failed, per round and arm (one attempt each).
        round1 = summary["failed_tests"]["1"]
        self.assertIn("test_atomicity.FailureInjection.test_failed_write_during_a_payment", round1)
        self.assertFalse([t for t in round1 if t.startswith("test_functional.")], round1)
        self.assertTrue(all(by_arm == {"no-plan": 1, "baseline-plan": 1, "dfa-plan": 1}
                            for by_arm in round1.values()), round1)
        self.assertNotIn("2", summary["failed_tests"])  # round 2 is the correct reference


class AttemptMetricsTest(unittest.TestCase):
    """Each round is scored on its own tests; a missing or refused round is not a zero."""

    @staticmethod
    def round(number, status="ok", tests_status="ok", passed=9, total=9, cr=None):
        by_category = {"functional": {"passed": passed, "total": total}}
        if cr is not None:
            by_category["change-request"] = {"passed": cr, "total": 7}
        tests = None if tests_status is None else {
            "status": tests_status, "error": None, "tests": [],
            "by_category": by_category if tests_status == "ok" else {}}
        return {"round": number, "status": status, "error": None,
                "implementer": {"cost_usd": 0.5, "usage": {"output_tokens": 100},
                                "latency_s": 10.0},
                "files": [], "tests": tests,
                "diff_from_previous": ({"files_changed": 1, "lines_added": 4, "lines_removed": 2}
                                       if number == 2 else None)}

    def record(self, r1, r2):
        return {"attempt_id": "t.a.r01", "arm": "a", "rounds": [r1, r2]}

    def test_a_hung_round_two_does_not_zero_round_one(self):
        m = outcomes.attempt_metrics(self.record(self.round(1), self.round(2, tests_status="timeout")),
                                     ["functional"])
        self.assertEqual(m["round1_pass_rate"], 1.0)
        self.assertEqual(m["r1_functional"], 1.0)
        self.assertEqual(m["change_request_pass_rate"], 0.0)  # hung code passes nothing
        self.assertEqual(m["rework_lines"], 6)
        self.assertEqual(m["cost_usd"], 1.0)

    def test_refused_and_missing_rounds_are_not_zeros(self):
        m = outcomes.attempt_metrics(self.record(self.round(1, tests_status="refused"),
                                                 self.round(2, status="error", tests_status=None)),
                                     ["functional"])
        self.assertIsNone(m["round1_pass_rate"])
        self.assertNotIn("r1_functional", m)
        self.assertIsNone(m["change_request_pass_rate"])
        self.assertIsNone(m["rework_lines"])

    def test_change_request_and_regression_come_from_round_two(self):
        m = outcomes.attempt_metrics(self.record(self.round(1, passed=8),
                                                 self.round(2, passed=9, cr=5)), ["functional"])
        self.assertAlmostEqual(m["round1_pass_rate"], 8 / 9)
        self.assertAlmostEqual(m["change_request_pass_rate"], 5 / 7)
        self.assertEqual(m["regression_pass_rate"], 1.0)

    def test_diff_counts_lines_that_look_like_headers(self):
        before = {"a.py": b"x = 1\n------\n"}
        after = {"a.py": b"x = 2\n+++ok\n"}
        self.assertEqual(outcomes.diff_stats(before, after),
                         {"files_changed": 1, "lines_added": 2, "lines_removed": 2})


if __name__ == "__main__":
    unittest.main()
