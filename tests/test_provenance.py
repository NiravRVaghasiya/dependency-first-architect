"""`validate` and `check` must show that a committed run's numbers follow from its raw records:
a recorded score, probe answer, lint result, rubric copy, test count or code snapshot that no
longer matches what it was derived from is reported, as are raw outputs no record explains,
result files outside a committed run, and drift in the pinned v1 condition. Fake providers
only, temp dirs only. Standard library only:  python -m unittest discover -s tests -v
"""

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(ROOT / "tests"))

from dfa_eval import aggregate, blinding, cli, generate, importer, judge, lint, outcomes  # noqa: E402
from dfa_eval import records  # noqa: E402
from test_pipeline_offline import QUIET, files, pipeline_config, run_analysis  # noqa: E402

TASK = "eval/outcomes/tasks/webhook-ledger"


def problems_of(run_dir, repo_root=None):
    return "\n".join(cli.validate_run(run_dir, repo_root))


class PlanRunTest(unittest.TestCase):
    """One small, fully judged and probed run, copied fresh for every test."""

    @classmethod
    def setUpClass(cls):
        cls.shared = Path(tempfile.mkdtemp())
        run_dir = cls.shared / "run"
        generate.run_generate(run_dir, pipeline_config(runs_per_cell=1), None, None, None,
                              work_root=cls.shared / "w", log=QUIET)
        blinding.run_blind(run_dir)
        judge.run_judge(run_dir, work_root=cls.shared / "w", log=QUIET)
        judge.run_probe(run_dir, work_root=cls.shared / "w", log=QUIET)
        run_analysis(run_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.shared, ignore_errors=True)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.run_dir = self.tmp / "run"
        shutil.copytree(self.shared / "run", self.run_dir)
        self.assertEqual(cli.validate_run(self.run_dir), [])

    def ok_record(self, stage):
        for path in files(self.run_dir / stage, "*.json"):
            rec = records.read_json(path)
            if rec["status"] == "ok":
                return path, rec
        self.fail(f"no ok record in {stage}")

    def test_an_in_range_score_edit_is_caught_against_the_raw_output(self):
        path, rec = self.ok_record(records.JUDGMENTS)
        rec["scores"][0]["score"] = 1 if rec["scores"][0]["score"] != 1 else 0
        records.write_json(path, rec)
        self.assertIn("scores differs from its raw output", problems_of(self.run_dir))

    def test_a_probe_answer_edit_is_caught_against_the_raw_output(self):
        path, rec = self.ok_record(records.PROBES)
        rec["p_methodology"] = 0.5 if rec["p_methodology"] != 0.5 else 0.25
        records.write_json(path, rec)
        self.assertIn("p_methodology differs from its raw output", problems_of(self.run_dir))

    def test_the_runs_rubric_copy_must_be_the_rubric_its_judgments_used(self):
        copy = self.run_dir / "rubrics" / "engineering-quality-v1.json"
        rubric = json.loads(copy.read_text(encoding="utf-8"))
        for dim in rubric["dimensions"]:
            dim["overlaps_methodology"] = False
        copy.write_text(json.dumps(rubric), encoding="utf-8")
        self.assertIn("rubrics/engineering-quality-v1.json in the run is not the rubric",
                      problems_of(self.run_dir))

    def test_raw_outputs_without_a_record_are_reported(self):
        gen = files(self.run_dir / records.GENERATIONS, "*.json")[0]
        record = records.read_json(gen)
        gen.unlink()
        (self.run_dir / records.GENERATIONS / f"{record['gen_id']}.md").unlink()
        problems = problems_of(self.run_dir)
        self.assertIn(f"raw/gen-{record['gen_id']}.txt: no record refers to this raw output",
                      problems)
        # ... and the summary says a planned plan has no record.
        flags = aggregate.aggregate(self.run_dir)["flags"]
        self.assertTrue(any("planned generation(s) have no record" in f and record["gen_id"] in f
                            for f in flags), flags)

    def test_earlier_attempts_of_a_recorded_call_are_not_orphans(self):
        path, rec = self.ok_record(records.JUDGMENTS)
        stem = Path(rec["raw_file"]).name[:-len(".txt")]
        (self.run_dir / records.RAW / f"{stem}.attempt1.txt").write_text("{}", encoding="utf-8")
        self.assertEqual(cli.validate_run(self.run_dir), [])

    def test_a_stale_lint_record_fails_check(self):
        path = files(self.run_dir / records.LINT, "*.json")[0]
        rec = records.read_json(path)
        rec["passed"] = rec["passed"] + 1 if rec["passed"] < rec["total"] else rec["passed"] - 1
        records.write_json(path, rec)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(cli.lint_run(self.run_dir))

    def test_mixed_instruction_versions_and_manifest_notes_are_flagged(self):
        for path in files(self.run_dir / records.GENERATIONS, "P1.dfa.*.json"):
            rec = records.read_json(path)
            rec["instructions_loaded"] = [dict(rec["instructions_loaded"][0], sha256="0" * 64)]
            records.write_json(path, rec)
        manifest = records.read_json(self.run_dir / records.MANIFEST)
        manifest["notes"] = "config replaced with --force-config on 2026-10-03"
        records.write_json(self.run_dir / records.MANIFEST, manifest)
        flags = aggregate.aggregate(self.run_dir)["flags"]
        self.assertTrue(any(f.startswith("condition dfa: its plans loaded SKILL.md in 2 versions")
                            for f in flags), flags)
        self.assertIn("manifest note: config replaced with --force-config on 2026-10-03", flags)


    def test_skill_plans_that_could_not_read_the_skill_are_flagged(self):
        for path in files(self.run_dir / records.GENERATIONS, "*.dfa.*.json"):
            rec = records.read_json(path)
            rec["tool_calls"] = [{"tool": "Read", "ok": False, "path": "plugins/p1/skills/"
                                  "dependency-first-architect/reference/plan-template.md"}]
            records.write_json(path, rec)
        flags = aggregate.aggregate(self.run_dir)["flags"]
        self.assertTrue(any(f.startswith("condition dfa, generator gen: 2 of 2 plans could not "
                                         "read the skill's files") for f in flags), flags)

    def test_session_settings_from_the_transcripts_are_disclosed(self):
        for n, path in enumerate(files(self.run_dir / records.GENERATIONS, "*.json")):
            rec = records.read_json(path)
            rec["provider"] = "claude-cli"
            mode = "auto" if n % 2 else "default"
            init = {"type": "system", "subtype": "init", "output_style": "Concise",
                    "permissionMode": mode}
            raw = self.run_dir / rec["raw_file"]
            raw.write_text(json.dumps(init) + "\n" + raw.read_text(encoding="utf-8"),
                           encoding="utf-8")
            records.write_json(path, rec)
        flags = aggregate.aggregate(self.run_dir)["flags"]
        self.assertTrue(any(f.startswith("generation sessions ran with output style 'Concise'")
                            for f in flags), flags)
        self.assertTrue(any(f.startswith("generation sessions ran in different permission "
                                         "modes: auto (gen 3); default (gen 3)") for f in flags),
                        flags)


class ImportedPlanTest(unittest.TestCase):
    def test_an_imported_plan_must_keep_the_v1_recorded_hash(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        run_dir = tmp / "v1"
        importer.import_v1_examples(run_dir, [{"id": "j", "provider": "fake", "model": "m"}],
                                    log=QUIET)
        self.assertEqual(cli.validate_run(run_dir), [])
        # Replace one plan consistently (text, hash, metrics): only generation.json disagrees.
        path = files(run_dir / records.GENERATIONS, "*.json")[0]
        rec = records.read_json(path)
        text = "A different plan.\n"
        records.write_text(run_dir / rec["output_file"], text)
        rec.update(output_sha256=records.sha256_text(text), metrics=generate.text_metrics(text))
        records.write_json(path, rec)
        self.assertIn("is not the sha256 that examples/scoring/generation.json recorded",
                      problems_of(run_dir))


class OutcomeRunTest(unittest.TestCase):
    def test_test_counts_and_code_snapshots_must_follow_from_the_records(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        cfg_path = tmp / "outcomes.json"
        cfg_path.write_text(json.dumps({
            "schema": "dfa-eval/outcomes-config@1", "name": "provenance", "seed": 5,
            "task": TASK, "planner": {"id": "fake-planner", "provider": "fake"},
            "implementer": {"id": "fake-impl", "provider": "fake", "options": {
                "agent_copy_from": [f"{TASK}/reference/round1", f"{TASK}/reference/round2"]}},
            "conditions": {"dfa": {"kind": "skill", "description": "skill", "skill_dir": ".",
                                   "skill_name": "dependency-first-architect"}},
            "arms": {"dfa-plan": "dfa"}, "runs_per_arm": 1, "limits": {"jobs": 1}}),
            encoding="utf-8")
        run_dir = tmp / "outcomes-run"
        ocfg = outcomes.load_outcomes_config(cfg_path, ROOT)
        outcomes.run_plan(run_dir, ocfg, cfg_path, repo_root=ROOT, work_root=tmp / "p", log=QUIET)
        outcomes.run_implement(run_dir, ocfg, allow_code_execution=True, repo_root=ROOT,
                               work_root=tmp / "i", log=QUIET)
        self.assertEqual(cli.validate_run(run_dir), [])
        path = next(p for p in files(run_dir / outcomes.OUTCOMES, "*.json")
                    if p.name not in ("config.json", outcomes.TASK_FILES))
        rec = records.read_json(path)
        rec["rounds"][0]["tests"]["by_category"]["money"]["passed"] -= 1
        records.write_json(path, rec)
        service = run_dir / outcomes.OUTCOMES / rec["attempt_id"] / "round2" / "ledger" / "service.py"
        service.write_bytes(service.read_bytes() + b"\n# edited\n")
        problems = problems_of(run_dir)
        self.assertIn("round 1 by_category does not follow from its test rows", problems)
        self.assertIn("round 2 snapshot of ledger/service.py does not match its sha256", problems)
        self.assertIn("round 2 code_sha256 does not match its snapshot", problems)
        # A CRLF checkout of the same code still verifies: hashes are of LF-normalized bytes.
        service.write_bytes(service.read_bytes().replace(b"\n# edited\n", b""))
        round1 = run_dir / outcomes.OUTCOMES / rec["attempt_id"] / "round1" / "ledger" / "service.py"
        round1.write_bytes(round1.read_bytes().replace(b"\n", b"\r\n"))
        self.assertNotIn("snapshot", problems_of(run_dir))


class TaskPinTest(unittest.TestCase):
    """An outcome run pins its task: tests changed after it started cannot mix into it."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        task = self.root / "eval" / "outcomes" / "tasks" / "t"
        (task / "hidden_tests").mkdir(parents=True)
        (task / "hidden_tests" / "test_a.py").write_bytes(b"x = 1\n")
        (task / "task.json").write_bytes(b"{}\n")
        (self.root / "eval" / "outcomes" / "runner.py").write_bytes(b"# runner\n")
        self.run = records.RunDir(self.root / "run")
        self.ocfg = {"task": "eval/outcomes/tasks/t"}
        self.test_file = task / "hidden_tests" / "test_a.py"

    def test_a_changed_test_file_stops_the_run_and_fails_validation(self):
        self.assertEqual(outcomes.task_changes(self.run.root, self.root),
                         [f"{outcomes.OUTCOMES}/{outcomes.TASK_FILES} is missing, so the task "
                          "version these results come from is unknown"])
        outcomes.pin_task(self.run, self.ocfg, self.root)
        self.assertEqual(outcomes.task_changes(self.run.root, self.root), [])
        self.test_file.write_bytes(b"x = 1\r\n")  # a CRLF checkout is the same file
        self.assertEqual(outcomes.task_changes(self.run.root, self.root), [])
        self.test_file.write_bytes(b"x = 2\n")
        self.assertEqual(outcomes.task_changes(self.run.root, self.root),
                         ["hidden_tests/test_a.py"])
        with self.assertRaises(outcomes.OutcomeError):
            outcomes.pin_task(self.run, self.ocfg, self.root)
        (self.root / "eval" / "outcomes" / "runner.py").write_bytes(b"# changed\n")
        self.assertIn("eval/outcomes/runner.py", outcomes.task_changes(self.run.root, self.root))


class CheckScopeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_result_files_outside_a_committed_run_are_reported(self):
        results = self.tmp / "results"
        (results / "loose").mkdir(parents=True)
        (results / "loose" / "summary.json").write_text("{}", encoding="utf-8")
        (results / "empty").mkdir()
        problems = cli.stray_results(results, self.tmp)
        self.assertEqual(len(problems), 1)
        self.assertIn("loose: has summary.json but no manifest.json", problems[0])

    def test_experiment_configs_must_use_the_original_prompts(self):
        config = {"prompts": [{"id": "P1", "request": "Plan a chatbot."}]}
        problems = cli.check_prompts(ROOT, config, "eval/experiments/x.json", require_all=False)
        self.assertTrue(any("eval/experiments/x.json P1: request differs" in p for p in problems))
        # A config without P1-P5 (e.g. an outcome task's planning config) is fine.
        self.assertEqual(cli.check_prompts(ROOT, {"prompts": []}, "x", require_all=False), [])
        self.assertEqual(cli.check_experiment_prompts(ROOT, cli.committed_runs(
            ROOT / "eval" / "results")), [])

    def test_the_v1_condition_is_pinned_not_only_self_described(self):
        root = self.tmp / "repo"
        shutil.copytree(ROOT / cli.V1_CONDITION, root / cli.V1_CONDITION)
        self.assertEqual(cli.check_v1_condition(root), [])
        folder = root / cli.V1_CONDITION
        skill = folder / "SKILL.md"
        skill.write_bytes(skill.read_bytes() + b"\nAn edit.\n")
        sums = folder / "SHA256SUMS"  # regenerate the self-description to match the edit
        lines = []
        for line in sums.read_text(encoding="utf-8").splitlines():
            digest, _, rel = line.partition("  ")
            if rel == "SKILL.md":
                digest = records.sha256_bytes(skill.read_bytes().replace(b"\r\n", b"\n"))
            lines.append(f"{digest}  {rel}")
        sums.write_text("\n".join(lines) + "\n", encoding="utf-8")
        problems = cli.check_v1_condition(root)
        self.assertTrue(any("SKILL.md is not the pinned 3ba6b70 digest" in p for p in problems),
                        problems)


if __name__ == "__main__":
    unittest.main()
