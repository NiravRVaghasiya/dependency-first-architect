"""Validate the outcome benchmark itself: the hidden tests must separate correct implementations
from the defects they claim to measure, and the runner must isolate the code under test.

Everything runs offline through eval/outcomes/runner.py in a child process, exactly as the
orchestrator runs model-written code. Standard library only:
    python -m unittest discover -s tests -v
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTCOMES = ROOT / "eval" / "outcomes"
TASK = OUTCOMES / "tasks" / "webhook-ledger"
sys.path.insert(0, str(OUTCOMES))
sys.path.insert(0, str(ROOT / "eval"))
import runner  # noqa: E402
from dfa_eval import schema, schemas  # noqa: E402

ROUND1 = ["failure-injection", "functional", "idempotency", "money", "ordering", "security"]


def run(code_dir, round_number, timeout_s=90):
    return runner.run_tests(TASK, code_dir, round_number, timeout_s=timeout_s)


def failing(results, category):
    counts = results["by_category"].get(category, {"passed": 0, "total": 0})
    return counts["total"] - counts["passed"]


class ReferenceAndStarterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ref1_r1 = run(TASK / "reference" / "round1", 1)
        cls.ref2_r2 = run(TASK / "reference" / "round2", 2)
        cls.ref1_r2 = run(TASK / "reference" / "round1", 2)
        cls.starter = run(TASK / "starter", 1)

    def test_results_match_the_schema(self):
        for results in (self.ref1_r1, self.ref2_r2, self.ref1_r2, self.starter):
            self.assertEqual(schema.validate(results, schemas.TEST_RESULTS), [])

    def test_round1_reference_passes_every_round1_test(self):
        self.assertEqual(self.ref1_r1["status"], "ok")
        self.assertEqual(sorted(self.ref1_r1["by_category"]), ROUND1)
        for category in ROUND1:
            self.assertEqual(failing(self.ref1_r1, category), 0, category)
        self.assertTrue(all(t["outcome"] == "pass" for t in self.ref1_r1["tests"]))

    def test_round2_reference_passes_every_test(self):
        self.assertEqual(sorted(self.ref2_r2["by_category"]), sorted(ROUND1 + ["change-request"]))
        self.assertTrue(all(t["outcome"] == "pass" for t in self.ref2_r2["tests"]),
                        [t for t in self.ref2_r2["tests"] if t["outcome"] != "pass"])

    def test_change_request_needs_new_work(self):
        self.assertGreater(failing(self.ref1_r2, "change-request"), 0)
        for category in ROUND1:  # round-1 behavior is still right
            self.assertEqual(failing(self.ref1_r2, category), 0, category)

    def test_starter_stub_passes_nothing(self):
        self.assertTrue(self.starter["tests"])
        self.assertTrue(all(t["outcome"] != "pass" for t in self.starter["tests"]))

    def test_every_round1_test_has_a_category(self):
        self.assertNotIn("unknown", self.ref1_r1["by_category"])


class MutantTest(unittest.TestCase):
    targets = json.loads((TASK / "mutants" / "targets.json").read_text(encoding="utf-8"))

    def test_every_mutant_directory_has_a_target(self):
        dirs = sorted(p.name for p in (TASK / "mutants").iterdir() if p.is_dir())
        self.assertEqual(dirs, sorted(self.targets))
        self.assertGreaterEqual(len(dirs), 7)

    def test_each_mutant_fails_its_category_and_passes_functional(self):
        for name, category in sorted(self.targets.items()):
            with self.subTest(mutant=name):
                results = run(TASK / "mutants" / name, 1)
                self.assertEqual(results["status"], "ok")
                self.assertGreater(failing(results, category), 0,
                                   f"{name} is not caught by the {category} tests")
                self.assertEqual(failing(results, "functional"), 0,
                                 f"{name} also breaks functional tests, so it is not isolated")


class RunnerIsolationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_canonical_ledgerkit_wins_over_a_tampered_copy(self):
        # The correct reference, shipped with a broken copy of ledgerkit that never commits.
        # If the runner imported that copy, nearly every test would fail; it must use its own.
        code = self.tmp / "impl"
        shutil.copytree(TASK / "reference" / "round1", code)
        shutil.copytree(TASK / "starter" / "ledgerkit", code / "ledgerkit")
        store = code / "ledgerkit" / "store.py"
        text = store.read_text(encoding="utf-8")
        self.assertIn("            self.commits += 1\n", text)
        store.write_text(text.replace("                    rows[key] = value\n",
                                      "                    pass\n"), encoding="utf-8")
        results = run(code, 1)
        self.assertEqual(results["status"], "ok")
        self.assertTrue(all(t["outcome"] == "pass" for t in results["tests"]))

    def test_hung_implementation_times_out(self):
        code = self.tmp / "impl"
        shutil.copytree(TASK / "reference" / "round1", code)
        service = code / "ledger" / "service.py"
        service.write_text(service.read_text(encoding="utf-8").replace(
            "    def handle(self, raw_body, headers):\n",
            "    def handle(self, raw_body, headers):\n        while True:\n            pass\n"),
            encoding="utf-8")
        results = run(code, 1, timeout_s=5)
        self.assertEqual(results["status"], "timeout")
        self.assertEqual(schema.validate(results, schemas.TEST_RESULTS), [])

    def test_import_error_is_reported_not_raised(self):
        code = self.tmp / "impl"
        shutil.copytree(TASK / "reference" / "round1", code)
        (code / "ledger" / "service.py").write_text("this is not python\n", encoding="utf-8")
        results = run(code, 1)
        self.assertEqual(results["status"], "ok")
        self.assertTrue(results["tests"])
        self.assertTrue(all(t["outcome"] == "error" for t in results["tests"]))

    def test_code_that_reaches_outside_is_not_run(self):
        code = self.tmp / "impl"
        shutil.copytree(TASK / "reference" / "round1", code)
        service = code / "ledger" / "service.py"
        service.write_text(service.read_text(encoding="utf-8")
                           + "\nimport subprocess\nos_call = None\n", encoding="utf-8")
        results = run(code, 1)
        self.assertEqual(results["status"], "refused")
        self.assertIn("imports subprocess", results["error"])
        self.assertEqual(results["tests"], [])
        self.assertEqual(schema.validate(results, schemas.TEST_RESULTS), [])
        # The references and every mutant pass the screen, so it does not affect validation.
        for folder in [TASK / "reference" / "round1", TASK / "reference" / "round2",
                       *sorted(p for p in (TASK / "mutants").iterdir() if p.is_dir())]:
            self.assertEqual(runner.screen_code(folder), [], folder.name)

    def reference_variant(self, old, new, round_number=1):
        """A reference solution with one legitimate change, in a temp code dir."""
        code = self.tmp / f"variant-r{round_number}"
        shutil.copytree(TASK / "reference" / f"round{round_number}", code)
        service = code / "ledger" / "service.py"
        text = service.read_text(encoding="utf-8")
        self.assertIn(old, text)
        service.write_text(text.replace(old, new), encoding="utf-8")
        return code

    def test_retrying_the_transaction_inside_the_request_is_correct(self):
        # A legitimate design the brief allows: retry a failed write once, then answer truthfully.
        code = self.reference_variant(
            "            with self.store.transaction() as txn:\n"
            "                outcome = self._apply(txn, event, hashlib.sha256(raw_body).hexdigest())\n",
            "            for attempt in range(3):\n"
            "                try:\n"
            "                    with self.store.transaction() as txn:\n"
            "                        outcome = self._apply(txn, event,\n"
            "                                              hashlib.sha256(raw_body).hexdigest())\n"
            "                    break\n"
            "                except StoreError:\n"
            "                    if attempt == 2:\n"
            "                        raise\n")
        results = run(code, 1)
        self.assertTrue(all(t["outcome"] == "pass" for t in results["tests"]),
                        [t for t in results["tests"] if t["outcome"] != "pass"])

    def test_rejecting_a_replayed_signature_is_correct(self):
        # Hardening the brief allows: refuse a request whose exact signature was already seen.
        # PayCo signs every delivery afresh, so no test may depend on byte-identical replays.
        check = ('        if abs(self.clock.now() - t) > TOLERANCE_S:\n'
                 '            raise Reject(401, "signature timestamp outside the replay window")\n')
        replay_cache = ('        seen = self.__dict__.setdefault("_seen_signatures", set())\n'
                        '        if (t, tuple(signatures)) in seen:\n'
                        '            raise Reject(401, "replayed request")\n'
                        '        seen.add((t, tuple(signatures)))\n')
        for round_number in (1, 2):
            code = self.reference_variant(check, check + replay_cache, round_number)
            results = run(code, round_number)
            self.assertEqual(results["status"], "ok")
            self.assertEqual([t["id"] for t in results["tests"] if t["outcome"] != "pass"], [],
                             f"round {round_number}")

    def test_standard_library_status_codes_are_allowed(self):
        code = self.reference_variant("import hashlib\n",
                                      "import hashlib\nfrom http import HTTPStatus\nimport shutil\n")
        results = run(code, 1)
        self.assertEqual(results["status"], "ok")
        self.assertTrue(all(t["outcome"] == "pass" for t in results["tests"]))

    def test_files_outside_the_package_cannot_shadow_hidden_tests(self):
        code = self.tmp / "impl"
        shutil.copytree(TASK / "mutants" / "non_idempotent", code)
        (code / "test_idempotency.py").write_text(
            "import unittest\nclass Fake(unittest.TestCase):\n    def test_ok(self):\n"
            "        pass\n", encoding="utf-8")
        (code / "_support.py").write_text("raise SystemExit('shadowed')\n", encoding="utf-8")
        results = run(code, 1)
        self.assertEqual(results["status"], "ok")
        self.assertGreater(failing(results, "idempotency"), 0)

    def test_scrubbed_env_drops_credentials(self):
        env = runner.scrubbed_env({
            "PATH": "/bin", "SYSTEMROOT": "C:\\Windows", "TEMP": "/tmp", "HOME": "/home/x",
            "AWS_SECRET_ACCESS_KEY": "s", "AWS_SESSION_TOKEN": "t", "ANTHROPIC_API_KEY": "a",
            "OPENAI_API_KEY": "o", "GITHUB_TOKEN": "g", "CLAUDE_CODE_USE_BEDROCK": "1",
            "PYTHONPATH": "/evil",
        })
        for key in ("AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "ANTHROPIC_API_KEY",
                    "OPENAI_API_KEY", "GITHUB_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "PYTHONPATH"):
            self.assertNotIn(key, env)
        for key in ("PATH", "SYSTEMROOT", "TEMP", "HOME"):
            self.assertIn(key, env)
        self.assertEqual(env["PYTHONHASHSEED"], "0")

    def test_a_shared_library_python_keeps_its_library_path(self):
        # actions/setup-python's Linux builds load libpython through LD_LIBRARY_PATH; dropping
        # it makes the child exit 127 before any test runs (every outcome test failed in CI).
        env = runner.scrubbed_env({"PATH": "/bin", "LD_LIBRARY_PATH": "/opt/python/lib",
                                   "DYLD_LIBRARY_PATH": "/opt/python/lib", "LD_PRELOAD": "x.so"})
        self.assertEqual(env["LD_LIBRARY_PATH"], "/opt/python/lib")
        self.assertEqual(env["DYLD_LIBRARY_PATH"], "/opt/python/lib")
        self.assertNotIn("LD_PRELOAD", env)


class TaskFileTest(unittest.TestCase):
    def test_task_json_is_consistent(self):
        task = json.loads((TASK / "task.json").read_text(encoding="utf-8"))
        modules = {p.stem for p in (TASK / "hidden_tests").glob("test_*.py")}
        self.assertEqual(set(task["categories"]), modules)
        self.assertEqual(set(task["round2_modules"]), modules)
        self.assertEqual(set(task["round1_modules"]), modules - {"test_change_request"})
        for key in ("brief", "change_request", "starter"):
            self.assertTrue((TASK / task[key]).exists(), key)
        self.assertIn("{brief}", task["planner_request"])
        self.assertIn("{plan_clause}", task["implementer_prompt"])
        self.assertIn("{plan_clause}", task["change_request_prompt"])

    def test_brief_does_not_spell_out_the_tested_implications(self):
        # The brief states facts; the hidden tests check what follows from them. Naming the
        # remedies would turn the benchmark into a reading test.
        brief = (TASK / "brief.md").read_text(encoding="utf-8").lower()
        for remedy in ("idempoten", "atomic", "single transaction", "compare_digest",
                       "float", "exactly once", "dedup"):
            self.assertNotIn(remedy, brief, remedy)


if __name__ == "__main__":
    unittest.main()
