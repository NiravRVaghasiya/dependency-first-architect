"""The whole evaluation pipeline, offline: plan, generate, blind, judge, probe, lint, aggregate,
report, validate, with fake generators and fake judges on 2 prompts x 3 conditions x 2 runs.

It checks the properties a real run depends on: every record validates, raw outputs are kept,
judges never see a condition, id or file name, a malformed judgment is retried and then excluded
and counted, the cost cap stops launching calls, a substituted model is never counted, resume
skips finished work, and aggregate/report reruns are byte-identical. Everything is written under
a temp dir. Standard library only:  python -m unittest discover -s tests -v
"""

import contextlib
import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))

from dfa_eval import blinding, cli, config, generate, judge, providers, records  # noqa: E402
from dfa_eval import schema, schemas  # noqa: E402

ANALYSIS = all(importlib.util.find_spec(f"dfa_eval.{name}") is not None
               for name in ("lint", "aggregate", "report"))
NEEDS_ANALYSIS = unittest.skipUnless(ANALYSIS, "lint.py, aggregate.py or report.py not present")
QUIET = lambda message: None  # noqa: E731
P1 = "Plan a customer-support RAG chatbot over our help-center docs."
P7 = "Plan a command-line tool that renames a folder of photos by their EXIF capture date."
CONDITIONS = ("baseline", "dfa", "generic-control")
SUBCOMMANDS = ("plan", "generate", "blind", "judge", "probe", "lint", "aggregate", "report",
               "validate", "matrix", "check", "all", "import-v1", "outcomes")


def pipeline_config(generator_options=None, judge_options=(None, None), **overrides):
    cfg = {
        "schema": "dfa-eval/config@1",
        "name": "offline",
        "seed": 99,
        "prompts": [
            {"id": "P1", "title": "RAG support chatbot", "kind": "AI", "request": P1,
             "checklist": "eval/rubrics/checklists/P1.json"},
            {"id": "P7", "title": "Photo renaming CLI", "kind": "non-AI", "request": P7,
             "checklist": "eval/rubrics/checklists/P7.json"},
        ],
        "conditions": {
            "baseline": {"kind": "plain", "description": "The request alone."},
            "dfa": {"kind": "skill", "description": "The skill.", "skill_dir": ".",
                    "skill_name": "dependency-first-architect"},
            "generic-control": {"kind": "skill", "description": "Active control.",
                                "skill_dir": "eval/conditions/generic-architect",
                                "skill_name": "architecture-planner"},
        },
        "generators": [{"id": "gen", "provider": "fake", "model": "fake-gen-1",
                        "options": generator_options or {}}],
        "judges": [{"id": "judge-a", "provider": "fake", "model": "fake-judge-a",
                    "options": judge_options[0] or {}},
                   {"id": "judge-b", "provider": "fake", "model": "fake-judge-b",
                    "options": judge_options[1] or {}}],
        "rubrics": ["methodology-adherence-v1", "engineering-quality-v1"],
        "runs_per_cell": 2,
        "contrasts": [{"treatment": "dfa", "control": "baseline"},
                      {"treatment": "dfa", "control": "generic-control"},
                      {"treatment": "generic-control", "control": "baseline"}],
        "probe": {"enabled": True, "judges": ["judge-a"]},
        "workspace": {"readme": "eval/conditions/neutral-workspace-README.md"},
        "limits": {"max_cost_usd": None, "jobs": 3, "timeout_s": 60},
    }
    cfg.update(overrides)
    return config.check_config(cfg, ROOT)


def files(folder, pattern="*"):
    return sorted(Path(folder).glob(pattern))


def run_analysis(run_dir):
    from dfa_eval import lint
    with contextlib.redirect_stdout(io.StringIO()):
        lint.run_lint(run_dir)
        cli.aggregate_run(run_dir)
        cli.report_run(run_dir)


def word(term, text):
    return re.search(rf"(?<![\w-]){re.escape(term)}(?![\w-])", text) is not None


class PipelineTest(unittest.TestCase):
    """One full run; the tests below inspect what it left behind."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.work = cls.tmp / "work"
        # judge-a: the first answer to any P1 prompt is malformed (it must be retried, then ok);
        # judge-b: every answer to a P7 prompt is malformed (it must end up invalid).
        cls.cfg = pipeline_config(judge_options=(
            {"malformed_on": ["help-center"], "malformed_times": 1},
            {"malformed_on": ["EXIF capture date"]}))
        cls.run_dir = cls.tmp / "offline-run"
        cls.plan_lines = []
        cls.plan_counts = generate.run_generate(cls.run_dir, cls.cfg, "offline.json", None, None,
                                                dry_run=True, log=cls.plan_lines.append)
        cls.dry_run_wrote = cls.run_dir.exists()
        cls.gen_counts = generate.run_generate(cls.run_dir, cls.cfg, None, None, None,
                                               work_root=cls.work, log=QUIET)
        cls.key = blinding.run_blind(cls.run_dir)
        cls.judge_counts = judge.run_judge(cls.run_dir, work_root=cls.work, log=QUIET)
        cls.probe_counts = judge.run_probe(cls.run_dir, work_root=cls.work, log=QUIET)
        if ANALYSIS:
            run_analysis(cls.run_dir)
        cls.rundir = records.RunDir(cls.run_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def judgments(self):
        return {j["judgment_id"]: j for j in self.rundir.judgments()}

    def test_dry_run_prints_the_matrix_and_writes_nothing(self):
        self.assertFalse(self.dry_run_wrote)
        self.assertEqual(self.plan_counts, {"planned": 12, "done": 0, "failed": 0, "to_run": 12})
        text = "\n".join(self.plan_lines)
        self.assertIn("2 prompts x 3 conditions x 1 generator(s) x 2 runs = 12 generations", text)
        self.assertIn("P7.generic-control.gen.r02", text)
        self.assertIn("about 48 judge calls", text)
        self.assertIn("about 12 leakage-probe calls", text)

    def test_stage_counts(self):
        self.assertEqual((self.gen_counts["ok"], self.gen_counts["error"]), (12, 0))
        self.assertEqual(len(self.key["entries"]), 24)
        self.assertEqual({k: self.judge_counts[k] for k in ("planned", "ok", "invalid", "error")},
                         {"planned": 48, "ok": 36, "invalid": 12, "error": 0})
        self.assertEqual((self.probe_counts["ok"], self.probe_counts["invalid"]), (12, 0))
        self.assertFalse(self.work.exists())  # workspaces and plugins are cleaned up

    def test_every_record_validates(self):
        checks = [("manifest.json", schemas.MANIFEST), ("generations/*.json", schemas.GENERATION),
                  ("judgments/*.json", schemas.JUDGMENT), ("probes/*.json", schemas.PROBE),
                  ("blind/key.json", schemas.BLIND_KEY)]
        if ANALYSIS:
            checks += [("lint/*.json", schemas.LINT), ("summary.json", schemas.SUMMARY)]
        for pattern, record_schema in checks:
            paths = files(self.run_dir, pattern)
            self.assertTrue(paths, pattern)
            for path in paths:
                self.assertEqual(schema.validate(records.read_json(path), record_schema), [], path)
        self.assertEqual(cli.validate_run(self.run_dir), [])

    def test_generation_records(self):
        gens = {g["gen_id"]: g for g in self.rundir.generations()}
        self.assertEqual(len(gens), 12)
        manifest = self.rundir.manifest()
        self.assertEqual(manifest["condition_files"]["dfa"]["SKILL.md"],
                         manifest["skill"]["files"]["SKILL.md"])
        self.assertNotIn("reference/evals.md", manifest["condition_files"]["dfa"])
        for gid, rec in gens.items():
            request = P1 if rec["prompt_id"] == "P1" else P7
            self.assertEqual(rec["seed"], config.derive_seed(99, gid))
            self.assertTrue(rec["seed_honored"])
            self.assertFalse(rec["model_mismatch"])
            self.assertEqual(rec["models_used"], ["fake-gen-1"])
            loaded = [f["path"] for f in rec["instructions_loaded"] or []]
            if rec["condition"] == "baseline":
                self.assertEqual(rec["request"], request)
                self.assertIsNone(rec["instructions_loaded"])
            elif rec["condition"] == "dfa":
                self.assertEqual(rec["request"], f"/dependency-first-architect {request}")
                self.assertEqual(loaded, ["SKILL.md", "reference/ai-systems.md",
                                          "reference/layer-map.md", "reference/plan-template.md",
                                          "reference/validation.md"])
                self.assertEqual({f["sha256"] for f in rec["instructions_loaded"]},
                                 set(manifest["condition_files"]["dfa"].values()))
            else:
                self.assertEqual(loaded, ["eval/conditions/generic-architect/SKILL.md",
                                          "eval/conditions/generic-architect/reference/"
                                          "document-template.md",
                                          "eval/conditions/generic-architect/reference/"
                                          "guidance.md"])
            for call in rec["tool_calls"]:  # relative paths: no temp dirs or user names
                self.assertTrue(call["path"].startswith("plugins/p"), call)
                self.assertNotIn(str(self.tmp), call["path"])
            text = self.rundir.generation_text(gid).read_bytes().decode("utf-8")
            self.assertEqual(rec["metrics"], generate.text_metrics(text))

    def test_raw_outputs_are_preserved(self):
        for rec in self.rundir.generations():
            raw = (self.run_dir / rec["raw_file"]).read_text(encoding="utf-8")
            text = self.rundir.generation_text(rec["gen_id"]).read_text(encoding="utf-8")
            self.assertEqual(json.loads(raw)["text"], text)
        for rec in self.judgments().values():
            raw = json.loads((self.run_dir / rec["raw_file"]).read_text(encoding="utf-8"))
            if rec["status"] == "ok":
                self.assertEqual(sorted(raw["structured"]["dimensions"], key=lambda d: d["dim"]),
                                 rec["scores"])
        raw_names = {p.name for p in files(self.run_dir / "raw")}
        self.assertEqual(len([n for n in raw_names if n.startswith("gen-")]), 12)

    def test_malformed_judgments_are_retried_then_excluded_and_counted(self):
        judgments = self.judgments()
        invalid = [j for j in judgments.values() if j["status"] == "invalid"]
        self.assertEqual(len(invalid), 12)
        gens = {g["gen_id"]: g for g in self.rundir.generations()}
        key = {e["blind_id"]: e for e in self.key["entries"]}
        for rec in invalid:
            self.assertEqual(rec["judge"], "judge-b")
            self.assertEqual(gens[key[rec["blind_id"]]["gen_id"]]["prompt_id"], "P7")
            self.assertEqual((rec["scores"], rec["checklist"], rec["traps"], rec["note"]),
                             ([], None, None, None))
            joined = "\n".join(rec["validation_errors"])
            for attempt in (1, 2, 3):
                self.assertIn(f"attempt {attempt}: ", joined)
            self.assertIn("is greater than maximum", joined)
            self.assertIn("given more than once", joined)
            self.assertIn("missing dim", joined)
            for n in (1, 2):
                self.assertTrue((self.run_dir / "raw" /
                                 f"judge-{rec['judgment_id']}.attempt{n}.txt").is_file())
        retried = [j for j in judgments.values() if j["judge"] == "judge-a"
                   and gens[key[j["blind_id"]]["gen_id"]]["prompt_id"] == "P1"]
        self.assertEqual(len(retried), 12)
        for rec in retried:  # malformed once, then valid: ok, and the first answer is kept
            self.assertEqual(rec["status"], "ok")
            self.assertEqual(rec["validation_errors"], [])
            first = self.run_dir / "raw" / f"judge-{rec['judgment_id']}.attempt1.txt"
            self.assertTrue(first.is_file())
            self.assertFalse(Path(str(first).replace("attempt1", "attempt2")).is_file())
        untouched = [j for j in judgments.values() if j["status"] == "ok" and j not in retried]
        for rec in untouched:
            self.assertFalse((self.run_dir / "raw" /
                              f"judge-{rec['judgment_id']}.attempt1.txt").exists())

    @NEEDS_ANALYSIS
    def test_aggregate_counts_the_excluded_judgments(self):
        summary = records.read_json(self.rundir.summary_path)
        self.assertEqual(summary["generated_from"]["judgments"]["invalid"], 12)
        self.assertEqual(summary["generated_from"]["judgments"]["ok"], 36)
        self.assertEqual(summary["generated_from"]["judgments_used"], 36)
        self.assertTrue(any(f.startswith("excluded 12 judgment(s) with status invalid")
                            for f in summary["flags"]), summary["flags"])

    @NEEDS_ANALYSIS
    def test_aggregate_and_report_reruns_are_byte_identical(self):
        before = {p: p.read_bytes() for p in (self.rundir.summary_path, self.rundir.report_path)}
        run_analysis(self.run_dir)  # lint, aggregate and report again, from the same records
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["aggregate", "--check", "--run", str(self.run_dir)]), 0)
            self.assertEqual(cli.main(["report", "--check", "--run", str(self.run_dir)]), 0)
            self.assertEqual(cli.main(["validate", "--run", str(self.run_dir)]), 0)
        for path in before:
            self.assertNotIn(b"\r\n", path.read_bytes())

    @NEEDS_ANALYSIS
    def test_lint_scores(self):
        lints = {rec["gen_id"]: rec for rec in self.rundir.lints()}
        self.assertEqual(len(lints), 12)
        for gid, rec in lints.items():
            if ".dfa." in gid:
                self.assertEqual(rec["score"], 1.0, gid)
            else:
                self.assertLess(rec["score"], 0.5, gid)

    def test_judges_never_see_conditions_ids_or_file_names(self):
        tasks = judge.plan_judging(self.run_dir) + judge.plan_probes(self.run_dir)
        self.assertEqual(len(tasks), 48 + 12)
        gens = {g["gen_id"]: g for g in self.rundir.generations()}
        key = {e["blind_id"]: e for e in self.key["entries"]}
        shared = {}
        for task in tasks:
            prompt = task["prompt"]
            plan = self.rundir.blind_text(task["blind_id"]).read_bytes().decode("utf-8")
            marker = f"<<<PLAN\n{plan}\nPLAN>>>"
            self.assertEqual(prompt.count(marker), 1, task["id"])
            # The only per-plan text is the blind copy itself, and it names no condition...
            for condition in CONDITIONS:
                self.assertFalse(word(condition, plan), (condition, task["id"]))
            # ...so what surrounds it must be identical for every condition of a prompt.
            gen = gens[key[task["blind_id"]]["gen_id"]]
            group = shared.setdefault((gen["prompt_id"], task.get("rubric", "probe")), {})
            group.setdefault(prompt.replace(marker, "<<<PLAN\n\nPLAN>>>"), set()).add(
                gen["condition"])
            for condition in ("dfa", "generic-control"):  # not English words: never anywhere
                self.assertFalse(word(condition, prompt), (condition, task["id"]))
            for gid in gens:
                self.assertNotIn(gid, prompt)
            for bid in key:
                self.assertNotIn(bid, prompt)
            for fragment in ("generations/", "blind/", "raw/", ".md", ".json", "dfa-eval",
                             self.run_dir.name, "Length limit", "/dependency-first-architect",
                             "/architecture-planner", "gen.r0"):
                self.assertNotIn(fragment, prompt, (fragment, task["id"]))
        self.assertEqual(len(shared), 2 * 3)  # 2 prompts x (2 rubrics + probe)
        for (prompt_id, rubric), variants in shared.items():
            self.assertEqual(len(variants), 1, (prompt_id, rubric))
            self.assertEqual(next(iter(variants.values())), set(CONDITIONS))

    def test_judge_prompts_are_the_documented_templates(self):
        sys.path.insert(0, str(ROOT / "examples" / "scoring"))
        try:
            import run_eval
        finally:
            sys.path.pop(0)
        gens = {g["gen_id"]: g for g in self.rundir.generations()}
        key = {e["blind_id"]: e for e in self.key["entries"]}
        prompts = {p["id"]: p for p in self.cfg["prompts"]}
        for task in judge.plan_judging(self.run_dir):
            entry = key[task["blind_id"]]
            prompt = prompts[gens[entry["gen_id"]]["prompt_id"]]
            plan = self.rundir.blind_text(task["blind_id"]).read_bytes().decode("utf-8")
            if task["rubric"] == "methodology-adherence-v1":
                self.assertEqual(entry["variant"], "plain")
                self.assertEqual(task["prompt"], run_eval.JUDGE_TASK.format(
                    request=prompt["request"], kind=f"{prompt['kind']} system",
                    rubric=run_eval.RUBRIC, plan=plan))
            else:
                self.assertEqual(entry["variant"], "neutralized")
                self.assertIn(f'The plan answers this request: "{prompt["request"]}"',
                              task["prompt"])
                self.assertIn("RUBRIC: 11 dimensions", task["prompt"])
                self.assertIn("1. Technical correctness: ", task["prompt"])
                self.assertIn("   Look for: ", task["prompt"])
                checklist = json.loads((ROOT / prompt["checklist"]).read_text(encoding="utf-8"))
                for item in checklist["items"]:
                    self.assertIn(f"- {item['id']} [{item['category']}] {item['title']}: ",
                                  task["prompt"])
                for trap in checklist["traps"]:
                    self.assertIn(f"- {trap['id']} {trap['title']}: ", task["prompt"])
                self.assertTrue(task["prompt"].endswith(f"<<<PLAN\n{plan}\nPLAN>>>"))

    def test_probe_prompt_is_the_documented_text(self):
        self.assertEqual(judge.PROBE_TASK, (
            "You are helping audit a blinded study. Some of the plans in this study were written "
            "by an AI model following a specific written planning methodology; the others were "
            "written by the same model without one. Read the plan below and estimate the "
            "probability (0 to 1) that it was written following a specific planning methodology. "
            "List up to 5 textual cues you relied on.\n\nThe plan answers this request: "
            "\"{request}\"\n\n<<<PLAN\n{plan}\nPLAN>>>"))
        key = {e["blind_id"]: e for e in self.key["entries"]}
        for task in judge.plan_probes(self.run_dir):
            self.assertEqual(key[task["blind_id"]]["variant"], "neutralized")
            self.assertEqual(task["schema"], schemas.PROBE_RESPONSE)
        probes = self.rundir.probes()
        self.assertEqual(len(probes), 12)
        self.assertTrue(all(0 <= p["p_methodology"] <= 1 for p in probes))

    def test_judge_order_is_seeded_and_recorded(self):
        first = [t["id"] for t in judge.plan_judging(self.run_dir)]
        self.assertEqual(first, [t["id"] for t in judge.plan_judging(self.run_dir)])
        unshuffled = sorted(first, key=lambda jid: (
            ("methodology-adherence-v1", "engineering-quality-v1").index(jid.split(".")[1]),
            jid.split(".")[0], jid.split(".")[2]))
        self.assertNotEqual(first, unshuffled)
        self.assertEqual(sorted(first), sorted(unshuffled))
        for jid, rec in self.judgments().items():
            self.assertEqual(rec["order_index"], first.index(jid))

    def test_cli_help_lists_every_subcommand(self):
        out = subprocess.run([sys.executable, str(ROOT / "eval" / "run.py"), "--help"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(out.returncode, 0, out.stderr)
        for name in SUBCOMMANDS:
            self.assertRegex(out.stdout, rf"(?m)^\s+{name}\s", name)


class CostCapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.run_dir = self.tmp / "capped"

    def generate(self, cfg, cap):
        return generate.run_generate(self.run_dir, cfg, None, 1, cap, work_root=self.tmp / "w",
                                     log=QUIET)

    def test_generation_stops_launching_at_the_cap_counting_existing_records(self):
        cfg = pipeline_config(generator_options={"cost_usd": 1.0})
        counts = self.generate(cfg, 3.0)
        self.assertEqual((counts["ok"], counts["skipped_cost"]), (3, 9))
        self.assertEqual(len(records.RunDir(self.run_dir).generations()), 3)
        self.assertEqual(len(files(self.run_dir / "generations", "*.md")), 3)
        again = self.generate(cfg, 3.0)  # $3 already spent: nothing more is launched
        self.assertEqual((again["to_run"], again["ok"], again["skipped_cost"]), (9, 0, 9))
        more = self.generate(cfg, 5.0)
        self.assertEqual((more["to_run"], more["ok"], more["skipped_cost"]), (9, 2, 7))
        self.assertEqual(more["spent_usd"], 5.0)
        # Judging shares the run's budget: $5 of generations already count against the cap.
        blinding.run_blind(self.run_dir)
        cfg_judges = records.RunDir(self.run_dir).manifest()["config"]["judges"]
        self.assertTrue(cfg_judges)
        with mock.patch.object(providers.FakeProvider, "_cost", return_value=0.5):
            judged = judge.run_judge(self.run_dir, jobs=1, max_cost_usd=6.0,
                                     work_root=self.tmp / "w", log=QUIET)
        self.assertEqual(judged["ok"], 2)
        self.assertEqual(judged["skipped_cost"], judged["to_run"] - 2)
        self.assertEqual(len(records.RunDir(self.run_dir).judgments()), 2)


class ModelMismatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_a_substituted_generator_model_is_recorded_as_an_error_and_never_counted(self):
        cfg = pipeline_config(generator_options={"models_used": ["global.anthropic.claude-opus-5"]},
                              generators=[{"id": "opus", "provider": "fake",
                                           "model": "claude-haiku-4-5", "options": {
                                               "models_used": ["global.anthropic.claude-opus-5"]}}])
        run_dir = self.tmp / "mismatch"
        counts = generate.run_generate(run_dir, cfg, None, None, None, work_root=self.tmp / "w",
                                       log=QUIET)
        self.assertEqual((counts["ok"], counts["error"]), (0, 12))
        run = records.RunDir(run_dir)
        for rec in run.generations():
            self.assertEqual(rec["status"], "error")
            self.assertTrue(rec["model_mismatch"])
            self.assertIn("model mismatch: requested 'claude-haiku-4-5'", rec["error"])
            self.assertIsNone(rec["output_file"])
            self.assertTrue((run_dir / rec["raw_file"]).is_file())  # the evidence is kept
        self.assertEqual(files(run_dir / "generations", "*.md"), [])
        self.assertEqual(blinding.run_blind(run_dir)["entries"], [])
        self.assertEqual(cli.validate_run(run_dir), [])
        if ANALYSIS:
            from dfa_eval import aggregate
            summary = aggregate.aggregate(run_dir)
            self.assertEqual(summary["generated_from"]["generations"]["error"], 12)
            self.assertEqual(summary["generated_from"]["generations"]["ok"], 0)
            self.assertTrue(any("model mismatch" in f for f in summary["flags"]))

    def test_a_substituted_judge_model_is_an_error_and_not_retried(self):
        cfg = pipeline_config(judge_options=({"models_used": ["some-other-model"]}, None),
                              runs_per_cell=1)
        run_dir = self.tmp / "judge-mismatch"
        generate.run_generate(run_dir, cfg, None, None, None, work_root=self.tmp / "w", log=QUIET)
        blinding.run_blind(run_dir)
        counts = judge.run_judge(run_dir, work_root=self.tmp / "w", log=QUIET)
        self.assertEqual((counts["ok"], counts["error"]), (12, 12))
        for rec in records.RunDir(run_dir).judgments():
            if rec["judge"] == "judge-a":
                self.assertEqual(rec["status"], "error")
                self.assertTrue(rec["model_mismatch"])
                self.assertEqual(rec["scores"], [])
                self.assertFalse((run_dir / "raw" /
                                  f"judge-{rec['judgment_id']}.attempt1.txt").exists())
            else:
                self.assertEqual(rec["status"], "ok")
        self.assertEqual(cli.validate_run(run_dir), [])


class ResumeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.run_dir = self.tmp / "resumed"
        self.cfg = pipeline_config(runs_per_cell=1)

    def generate(self, cfg=None, **kwargs):
        return generate.run_generate(self.run_dir, cfg or self.cfg, None, None, None,
                                     work_root=self.tmp / "w", log=QUIET, **kwargs)

    def test_finished_work_is_skipped_and_failed_work_is_retried_on_request(self):
        real = providers.FakeProvider._failure

        def flaky(provider, prompt):
            return "transient failure" if "EXIF" in prompt else real(provider, prompt)

        with mock.patch.object(providers.FakeProvider, "_failure", flaky):
            first = self.generate()
        self.assertEqual((first["ok"], first["error"]), (3, 3))
        snapshot = {p: p.read_bytes() for p in files(self.run_dir / "generations")}
        again = self.generate()  # without --retry-failed, failed records are left alone
        self.assertEqual((again["to_run"], again["done"], again["failed"]), (0, 3, 3))
        self.assertEqual({p: p.read_bytes() for p in files(self.run_dir / "generations")},
                         snapshot)
        retried = self.generate(retry_failed=True)
        self.assertEqual((retried["to_run"], retried["ok"]), (3, 3))
        statuses = [g["status"] for g in records.RunDir(self.run_dir).generations()]
        self.assertEqual(statuses, ["ok"] * 6)
        for path, data in snapshot.items():  # the ok records were not redone
            if path.suffix == ".md":
                self.assertEqual(path.read_bytes(), data)
        run = records.RunDir(self.run_dir)
        run.generation_json("P1.dfa.gen.r01").unlink()
        run.generation_text("P1.dfa.gen.r01").unlink()
        self.assertEqual(self.generate()["ok"], 1)

    def test_judging_resumes_and_retries_invalid_judgments_on_request(self):
        self.generate()
        blinding.run_blind(self.run_dir)
        with mock.patch.object(providers.FakeProvider, "_is_malformed",
                               lambda provider, prompt: "EXIF" in prompt):
            first = judge.run_judge(self.run_dir, work_root=self.tmp / "w", log=QUIET)
        self.assertEqual((first["ok"], first["invalid"]), (12, 12))
        again = judge.run_judge(self.run_dir, work_root=self.tmp / "w", log=QUIET)
        self.assertEqual((again["to_run"], again["ok"]), (0, 0))
        retried = judge.run_judge(self.run_dir, retry_failed=True, work_root=self.tmp / "w",
                                  log=QUIET)
        self.assertEqual((retried["to_run"], retried["ok"]), (12, 12))
        for rec in records.RunDir(self.run_dir).judgments():
            self.assertEqual(rec["status"], "ok")
            self.assertFalse((self.run_dir / "raw" /
                              f"judge-{rec['judgment_id']}.attempt1.txt").exists())

    def test_a_changed_config_is_refused_unless_forced(self):
        self.generate()
        changed = pipeline_config(runs_per_cell=2)
        with self.assertRaises(generate.HarnessError) as caught:
            self.generate(changed)
        message = str(caught.exception)
        self.assertIn(config.config_sha256(self.cfg), message)
        self.assertIn(config.config_sha256(changed), message)
        forced = self.generate(changed, force_config=True)
        self.assertEqual((forced["to_run"], forced["ok"]), (6, 6))
        manifest = records.RunDir(self.run_dir).manifest()
        self.assertEqual(manifest["config_sha256"], config.config_sha256(changed))
        self.assertIn("--force-config", manifest["notes"])
        self.assertEqual(cli.validate_run(self.run_dir), [])

    def test_a_nonempty_directory_without_a_manifest_is_not_a_run(self):
        self.run_dir.mkdir()
        (self.run_dir / "notes.txt").write_text("mine", encoding="utf-8")
        with self.assertRaises(generate.HarnessError):
            self.generate()
        self.assertEqual([p.name for p in self.run_dir.iterdir()], ["notes.txt"])

    def test_workspaces_never_live_in_the_repository(self):
        with self.assertRaises(generate.HarnessError):
            generate.run_generate(self.run_dir, self.cfg, None, None, None,
                                  work_root=ROOT / "eval" / "tmp-work", log=QUIET)
        self.assertFalse((ROOT / "eval" / "tmp-work").exists())


# A command-provider judge in the documented style (it reads {prompt_file}, writes {output_file}).
# It echoes the "draft <seed>" of the plan it was shown, says what else it found in its
# directory, and answers nothing for photo-renaming (EXIF) plans.
JUDGE_STUB = r'''
import json, random, re, sys, time
from pathlib import Path
prompt = Path(sys.argv[1]).read_text(encoding="utf-8")
others = sorted(p.name for p in Path.cwd().iterdir() if p.name != "prompt.txt")
time.sleep(random.random() * 0.2)
if "EXIF" in prompt:
    sys.exit(0)
cues = ["draft " + re.search(r"[Dd]raft (\d+)", prompt).group(1)]
if others:
    cues.append("found: " + ", ".join(others))
Path(sys.argv[2]).write_text(json.dumps({"p_methodology": 0.5, "cues": cues}), encoding="utf-8")
'''


def draft_seed(text):
    return re.search(r"[Dd]raft (\d+)", text).group(1)


class CallIsolationTest(unittest.TestCase):
    """Every judge and probe call runs in a fresh directory of its own."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_concurrent_command_probes_each_answer_their_own_plan(self):
        stub = self.tmp / "judge_stub.py"
        stub.write_text(JUDGE_STUB, encoding="utf-8")
        cfg = pipeline_config(
            judges=[{"id": "cmd-judge", "provider": "command", "options": {
                "argv": [sys.executable, str(stub), "{prompt_file}", "{output_file}"]}}],
            rubrics=[], probe={"enabled": True, "judges": ["cmd-judge"]})
        run_dir = self.tmp / "isolated"
        generate.run_generate(run_dir, cfg, None, None, None, work_root=self.tmp / "w", log=QUIET)
        blinding.run_blind(run_dir)
        with mock.patch.object(judge, "STAGGER_S", 0.0):
            counts = judge.run_probe(run_dir, jobs=4, work_root=self.tmp / "w", log=QUIET)
        self.assertEqual((counts["ok"], counts["error"]), (6, 6))
        run = records.RunDir(run_dir)
        for rec in run.probes():
            plan = run.blind_text(rec["blind_id"]).read_text(encoding="utf-8")
            if rec["status"] == "error":  # an agent that wrote nothing has no answer
                self.assertIn("without writing its output file", rec["error"])
                self.assertEqual(rec["cues"], [])
                continue
            # The answer is about this record's own plan, from a directory nothing else used.
            self.assertEqual(rec["cues"], [f"draft {draft_seed(plan)}"], rec["probe_id"])
        prompts = {e["blind_id"]: e["gen_id"].split(".")[0] for e in run.blind_key()["entries"]}
        self.assertEqual(sorted(prompts[r["blind_id"]] for r in run.probes()
                                if r["status"] == "error"), ["P7"] * 6)
        self.assertFalse((self.tmp / "w").exists())  # per-call directories are removed

    def test_each_judge_call_and_retry_starts_in_an_empty_directory_of_its_own(self):
        cfg = pipeline_config(runs_per_cell=1, judge_options=(
            {"malformed_on": ["help-center"], "malformed_times": 1}, None))
        run_dir = self.tmp / "dirs"
        generate.run_generate(run_dir, cfg, None, None, None, work_root=self.tmp / "w", log=QUIET)
        blinding.run_blind(run_dir)
        real, seen = providers.FakeProvider.judge, []

        def judge_and_litter(provider, prompt, schema, workdir, seed, timeout_s):
            seen.append((Path(workdir), sorted(p.name for p in Path(workdir).iterdir())))
            (Path(workdir) / "scratch.txt").write_text(prompt[:50], encoding="utf-8")
            return real(provider, prompt, schema, workdir, seed, timeout_s)

        with mock.patch.object(providers.FakeProvider, "judge", judge_and_litter):
            counts = judge.run_judge(run_dir, work_root=self.tmp / "w", log=QUIET)
        self.assertEqual(counts["ok"], 24)
        self.assertEqual(len(seen), 24 + 6)  # 6 P1 judgments by judge-a needed a second attempt
        self.assertEqual([listing for _, listing in seen], [[]] * len(seen))
        self.assertEqual(len({path for path, _ in seen}), 24)  # one directory per judgment
        self.assertTrue(all(re.fullmatch(r"w[0-9a-f]{8}", path.name) for path, _ in seen))
        self.assertFalse((self.tmp / "w").exists())


class HeldDirectoryTest(unittest.TestCase):
    """On Windows a helper process of an earlier CLI call (its telemetry server) can keep that
    call's directory open after the call ends; the next call must still get a fresh directory."""

    def test_a_directory_that_cannot_be_removed_gets_a_new_name(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "j").mkdir()
        held = "w1234abcd"
        (tmp / "j" / held).mkdir()
        real = judge.workspace.prepare_workdir

        def prepare(base, name, *args, **kwargs):
            if name == held:
                raise PermissionError(13, "being used by another process")
            return real(base, name, *args, **kwargs)

        with mock.patch.object(judge.workspace, "prepare_workdir", side_effect=prepare):
            workdir = judge._fresh_workdir(tmp / "j", held, "task-1")
        self.assertNotEqual(workdir.name, held)
        self.assertTrue(workdir.is_dir() and not any(workdir.iterdir()))
        self.assertRegex(workdir.name, r"^w[0-9a-f]{8}$")


class SupersedeTest(unittest.TestCase):
    """A retry never overwrites a paid call: the old record and raw files move to superseded/."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.run_dir = self.tmp / "retried"

    def generate(self, cfg, cap=None, **kwargs):
        return generate.run_generate(self.run_dir, cfg, None, 1, cap, work_root=self.tmp / "w",
                                     log=QUIET, **kwargs)

    def test_retried_generations_are_kept_and_still_count_against_the_cap(self):
        # P7 calls fail after they were paid for ($0.50 each).
        cfg = pipeline_config(generator_options={"cost_usd": 0.5, "fail_on": ["EXIF"]},
                              runs_per_cell=1)
        run = records.RunDir(self.run_dir)
        first = self.generate(cfg)
        self.assertEqual((first["ok"], first["error"]), (3, 3))
        self.assertEqual(generate.run_cost(run), 3.0)
        second = self.generate(cfg, retry_failed=True)
        self.assertEqual((second["to_run"], second["error"]), (3, 3))
        self.assertEqual(generate.run_cost(run), 4.5)  # the replaced calls' $1.50 still counts
        failed = sorted(g["gen_id"] for g in run.generations() if g["status"] == "error")
        self.assertEqual(len(failed), 3)
        for gid in failed:
            old = records.read_json(self.run_dir / "superseded" / "generations" / f"{gid}.1.json")
            self.assertEqual((old["gen_id"], old["status"], old["cost_usd"]), (gid, "error", 0.5))
            self.assertEqual(old["raw_file"], f"superseded/raw/gen-{gid}.1.txt")
            self.assertIn("fake provider failure",
                          (self.run_dir / old["raw_file"]).read_text(encoding="utf-8"))
            self.assertTrue((self.run_dir / "raw" / f"gen-{gid}.txt").is_file())  # the new one
        # $4.50 spent: a $5 cap allows one more call, then stops.
        third = self.generate(cfg, cap=5.0, retry_failed=True)
        self.assertEqual((third["to_run"], third["error"], third["skipped_cost"]), (3, 1, 2))
        self.assertEqual(generate.run_cost(run), 5.0)
        self.assertTrue((self.run_dir / "superseded" / "generations" / f"{failed[0]}.2.json")
                        .is_file())  # numbered after the existing ones, never overwritten
        self.assertTrue((self.run_dir / "superseded" / "raw" / f"gen-{failed[0]}.2.txt")
                        .is_file())
        self.assertEqual(len(generate.superseded_records(run)), 4)
        self.assertEqual(cli.validate_run(self.run_dir), [])
        # A retry that succeeds keeps the failed attempts too; the plan itself is one record.
        with mock.patch.object(providers.FakeProvider, "_failure", lambda provider, prompt: None):
            self.generate(cfg, retry_failed=True)
        self.assertEqual(sorted(g["status"] for g in run.generations()), ["ok"] * 6)
        self.assertEqual(len(generate.superseded_records(run, records.GENERATIONS)), 7)
        self.assertEqual(generate.run_cost(run), 6.5)
        self.assertEqual(cli.validate_run(self.run_dir), [])

    def test_retried_judgments_keep_every_earlier_attempt(self):
        cfg = pipeline_config(runs_per_cell=1)
        self.generate(cfg)
        blinding.run_blind(self.run_dir)
        run = records.RunDir(self.run_dir)
        with mock.patch.object(providers.FakeProvider, "_is_malformed",
                               lambda provider, prompt: "EXIF" in prompt), \
                mock.patch.object(providers.FakeProvider, "_cost", return_value=0.25):
            first = judge.run_judge(self.run_dir, work_root=self.tmp / "w", log=QUIET)
        self.assertEqual(first["invalid"], 12)
        spent = generate.run_cost(run)
        invalid = sorted(j["judgment_id"] for j in run.judgments() if j["status"] == "invalid")
        with mock.patch.object(providers.FakeProvider, "_cost", return_value=0.25):
            retried = judge.run_judge(self.run_dir, retry_failed=True, work_root=self.tmp / "w",
                                      log=QUIET)
        self.assertEqual((retried["to_run"], retried["ok"]), (12, 12))
        self.assertAlmostEqual(generate.run_cost(run), spent + 12 * 0.25)
        for jid in invalid:
            old = records.read_json(self.run_dir / "superseded" / "judgments" / f"{jid}.1.json")
            self.assertEqual((old["status"], old["cost_usd"]), ("invalid", 0.75))  # 3 attempts
            self.assertEqual(old["raw_file"], f"superseded/raw/judge-{jid}.1.txt")
            for name in (f"judge-{jid}.1.txt", f"judge-{jid}.attempt1.1.txt",
                         f"judge-{jid}.attempt2.1.txt"):
                self.assertTrue((self.run_dir / "superseded" / "raw" / name).is_file(), name)
            self.assertFalse((self.run_dir / "raw" / f"judge-{jid}.attempt1.txt").exists())
        self.assertEqual(cli.validate_run(self.run_dir), [])


class SkillGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_a_skill_session_that_lists_skills_without_its_own_is_an_error(self):
        # The plugin did not load: the session listed its skills, and the condition's skill was
        # not among them, so the "treatment" plan was written without the treatment.
        real = providers.FakeProvider.generate

        def without_plugin(provider, prompt, workdir, plugin_dirs, system_append, seed, timeout):
            out = real(provider, prompt, workdir, plugin_dirs, system_append, seed, timeout)
            if plugin_dirs:
                out.skills_available = ["compact", "review"]
                out.loaded_paths, out.tool_calls = [], []
            return out

        run_dir = self.tmp / "no-skill"
        with mock.patch.object(providers.FakeProvider, "generate", without_plugin):
            counts = generate.run_generate(run_dir, pipeline_config(runs_per_cell=1), None, None,
                                           None, work_root=self.tmp / "w", log=QUIET)
        self.assertEqual((counts["ok"], counts["error"]), (2, 4))
        for rec in records.RunDir(run_dir).generations():
            if rec["condition"] == "baseline":
                self.assertEqual(rec["status"], "ok")
                continue
            self.assertEqual(rec["status"], "error")
            self.assertIn("skill not offered", rec["error"])
            self.assertIsNone(rec["output_file"])
        blinded = {e["gen_id"].split(".")[1] for e in blinding.run_blind(run_dir)["entries"]}
        self.assertEqual(blinded, {"baseline"})  # nothing skill-less reaches the judges as dfa
        self.assertEqual(cli.validate_run(run_dir), [])

    def test_a_second_copy_of_the_conditions_own_skill_is_contamination(self):
        real = providers.FakeProvider.generate

        def personal_copy(provider, prompt, workdir, plugin_dirs, system_append, seed, timeout):
            out = real(provider, prompt, workdir, plugin_dirs, system_append, seed, timeout)
            out.skills_available = sorted((out.skills_available or []) + [
                f"{n}:{n}" for n in out.skills_available or []] + ["dependency-first-architect"])
            return out

        run_dir = self.tmp / "personal"
        with mock.patch.object(providers.FakeProvider, "generate", personal_copy):
            generate.run_generate(run_dir, pipeline_config(runs_per_cell=1), None, None, None,
                                  work_root=self.tmp / "w", log=QUIET)
        by_condition = {}
        for rec in records.RunDir(run_dir).generations():
            by_condition.setdefault(rec["condition"], set()).add(rec["status"])
            if rec["condition"] == "dfa":
                self.assertIn("second copy of its own skill 'dependency-first-architect'",
                              rec["error"])
        self.assertEqual(by_condition, {"baseline": {"error"}, "dfa": {"error"},
                                        "generic-control": {"error"}})


class StaleJudgmentTest(unittest.TestCase):
    """A judgment records the blind copy it scored; re-blinding to other text makes it stale."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.run_dir = self.tmp / "reblinded"
        generate.run_generate(self.run_dir, pipeline_config(runs_per_cell=1), None, None, None,
                              work_root=self.tmp / "w", log=QUIET)
        blinding.run_blind(self.run_dir)
        judge.run_judge(self.run_dir, work_root=self.tmp / "w", log=QUIET)
        judge.run_probe(self.run_dir, work_root=self.tmp / "w", log=QUIET)
        self.run = records.RunDir(self.run_dir)

    def reblind_with_changed_neutralized_copies(self):
        real, lines = blinding.blind_variants, []

        def changed(*args, **kwargs):
            plain, (variant, text, count, removed) = real(*args, **kwargs)
            return [plain, (variant, text + "A line a new blinding rule would add.\n", count,
                            removed)]

        with mock.patch.object(blinding, "blind_variants", changed):
            blinding.run_blind(self.run_dir, log=lines.append)
        return lines

    def test_records_carry_the_sha256_of_what_they_scored(self):
        shas = {e["blind_id"]: e["sha256"] for e in self.run.blind_key()["entries"]}
        records_ = self.run.judgments() + self.run.probes()
        self.assertEqual(len(records_), 24 + 6)
        for rec in records_:
            self.assertEqual(rec["blind_sha256"], shas[rec["blind_id"]])
        self.assertEqual(cli.validate_run(self.run_dir), [])

    def test_judgments_of_a_changed_blind_copy_are_flagged_and_redone(self):
        lines = self.reblind_with_changed_neutralized_copies()
        self.assertTrue(any("will redo them" in line for line in lines), lines)
        problems = cli.validate_run(self.run_dir)
        # Engineering quality scores the neutralized copy (6 plans x 2 judges); so does the probe.
        self.assertEqual(len([p for p in problems if "scored an earlier text" in p]), 12, problems)
        self.assertEqual(len([p for p in problems if "probed an earlier text" in p]), 6, problems)
        self.assertEqual(len(problems), 18, problems)
        judged = judge.run_judge(self.run_dir, work_root=self.tmp / "w", log=QUIET)
        probed = judge.run_probe(self.run_dir, work_root=self.tmp / "w", log=QUIET)
        self.assertEqual((judged["to_run"], judged["ok"]), (12, 12))
        self.assertEqual((probed["to_run"], probed["ok"]), (6, 6))
        self.assertEqual(cli.validate_run(self.run_dir), [])
        self.assertEqual(len(list((self.run_dir / "superseded" / "judgments").glob("*.json"))), 12)
        self.assertEqual(len(list((self.run_dir / "superseded" / "probes").glob("*.json"))), 6)
        self.assertEqual(judge.run_judge(self.run_dir, work_root=self.tmp / "w",
                                         log=QUIET)["to_run"], 0)

    def test_records_from_before_blind_sha256_are_kept_and_the_change_is_reported(self):
        for path in files(self.run_dir / "judgments", "*.json"):
            rec = records.read_json(path)
            del rec["blind_sha256"]  # as written by an earlier harness version
            records.write_json(path, rec)
        lines = self.reblind_with_changed_neutralized_copies()
        warning = [line for line in lines if line.startswith("WARNING:")]
        self.assertEqual(len(warning), 1, lines)
        self.assertIn("12 judgment(s)/probe(s) of changed blind copies were recorded without "
                      "blind_sha256", warning[0])
        # Old records still validate and are not redone (nothing says what they scored)...
        problems = cli.validate_run(self.run_dir)
        self.assertEqual(len([p for p in problems if "scored an earlier text" in p]), 0)
        self.assertEqual(judge.run_judge(self.run_dir, work_root=self.tmp / "w",
                                         log=QUIET)["to_run"], 0)
        # ...while the probes, which carry it, are.
        self.assertEqual(judge.run_probe(self.run_dir, work_root=self.tmp / "w",
                                         log=QUIET)["to_run"], 6)

    def test_judging_refuses_a_blind_copy_that_does_not_match_the_key(self):
        entry = self.run.blind_key()["entries"][0]
        path = self.run.blind_text(entry["blind_id"])
        path.write_bytes(path.read_bytes() + b"An edit after blinding.\n")
        with self.assertRaises(generate.HarnessError) as caught:
            judge.run_judge(self.run_dir, work_root=self.tmp / "w", log=QUIET)
        self.assertIn("does not match its sha256 in blind/key.json", str(caught.exception))


class ValidateAndCheckTest(unittest.TestCase):
    """`validate` catches tampering; `check` passes on a clean copy and fails on drift."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.repo = cls.tmp / "repo"
        for rel in ("SKILL.md", "VERSION", "reference", "examples", "eval", "adapters"):
            source = ROOT / rel
            if source.is_dir():
                shutil.copytree(source, cls.repo / rel,
                                ignore=shutil.ignore_patterns("__pycache__", "results"))
            elif source.is_file():
                (cls.repo / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, cls.repo / rel)
        (cls.repo / "docs").mkdir()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def check(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            if importlib.util.find_spec("dfa_eval.matrix") is not None:
                cli.matrix_doc(self.repo)
            failures = cli.run_check(self.repo)
        return failures, out.getvalue()

    def edit(self, rel, old, new):
        path = self.repo / rel
        original = path.read_bytes()
        text = original.decode("utf-8")
        self.assertIn(old, text)
        path.write_bytes(text.replace(old, new, 1).encode("utf-8"))
        self.addCleanup(path.write_bytes, original)

    def test_clean_copy_without_runs_passes(self):
        failures, out = self.check()
        self.assertEqual(failures, [], out)
        self.assertIn("OK: no committed runs", out)
        self.assertIn("OK: the control skill equals its authoring session output", out)

    def test_each_kind_of_drift_fails(self):
        cases = [
            ("eval/rubrics/checklists/P1.json", "Ingestion with updates and deletions",
             "Ingestion with updates", "items differs from the authoring record"),
            ("eval/conditions/generic-architect/reference/guidance.md", "the", "teh",
             "differs from authoring-session.json guidance_md"),
            ("eval/rubrics/methodology-adherence-v1.json", "Score substance", "Score style",
             "rubric_text differs"),
            ("eval/benchmark.json", "Architect a multi-tenant SaaS billing system.",
             "Architect a multitenant SaaS billing system.", "request differs from "
                                                             "reference/evals.md"),
            ("eval/rubrics/engineering-quality-v1.json", "Technical correctness",
             "Technical accuracy", "differs from eval/rubrics/authoring"),
        ]
        for rel, old, new, expected in cases:
            with self.subTest(rel):
                path = self.repo / rel
                original = path.read_bytes()
                text = original.decode("utf-8")
                self.assertIn(old, text)
                path.write_bytes(text.replace(old, new, 1).encode("utf-8"))
                try:
                    failures, out = self.check()
                finally:
                    path.write_bytes(original)
                self.assertTrue(any(expected in f for f in failures), (expected, failures))
                self.assertIn("FAIL: ", out)
        self.assertEqual(self.check()[0], [])

    @NEEDS_ANALYSIS
    def test_committed_runs_are_validated_and_rechecked(self):
        results = self.repo / "eval" / "results"
        self.addCleanup(lambda: shutil.rmtree(results, ignore_errors=True))
        run_dir = results / "offline-run"
        cfg = pipeline_config(runs_per_cell=1)
        generate.run_generate(run_dir, cfg, None, None, None, work_root=self.tmp / "w",
                              repo_root=self.repo, log=QUIET)
        blinding.run_blind(run_dir)
        judge.run_judge(run_dir, work_root=self.tmp / "w", repo_root=self.repo, log=QUIET)
        judge.run_probe(run_dir, work_root=self.tmp / "w", repo_root=self.repo, log=QUIET)
        run_analysis(run_dir)
        failures, out = self.check()
        self.assertEqual(failures, [], out)
        self.assertIn("offline-run: every record validates", out)
        md = files(run_dir / "generations", "*.md")[0]
        original = md.read_bytes()
        md.write_bytes(original.replace(b"\n", b"\nAn edit.\n", 1))
        try:
            failures, _ = self.check()
        finally:
            md.write_bytes(original)
        self.assertTrue(any("does not match output_sha256" in f for f in failures), failures)
        summary = run_dir / "summary.json"
        original = summary.read_bytes()
        summary.write_bytes(original.replace(b'"schema"', b'"schema" ', 1))
        try:
            failures, out = self.check()
        finally:
            summary.write_bytes(original)
        self.assertTrue(any("summary.json is stale" in f for f in failures), failures)
        self.assertIn("(committed)", out)

    def test_committed_outcome_runs_are_checked_with_their_own_summary(self):
        if importlib.util.find_spec("dfa_eval.outcomes") is None:
            self.skipTest("eval/dfa_eval/outcomes.py is not present")
        from dfa_eval import outcomes
        task = "eval/outcomes/tasks/webhook-ledger"
        results = self.repo / "eval" / "results"
        self.addCleanup(lambda: shutil.rmtree(results, ignore_errors=True))
        cfg_path = self.tmp / "outcomes.json"
        cfg_path.write_text(json.dumps({
            "schema": "dfa-eval/outcomes-config@1", "name": "check-test", "seed": 3,
            "task": task, "planner": {"id": "fake-planner", "provider": "fake"},
            "implementer": {"id": "fake-impl", "provider": "fake", "options": {
                "agent_copy_from": [f"{task}/reference/round1", f"{task}/reference/round2"]}},
            "conditions": {"dfa": {"kind": "skill", "description": "skill", "skill_dir": ".",
                                   "skill_name": "dependency-first-architect"}},
            "arms": {"no-plan": None, "dfa-plan": "dfa"}, "runs_per_arm": 1,
            "limits": {"jobs": 1}}), encoding="utf-8")
        run_dir = results / "outcomes-run"
        ocfg = outcomes.load_outcomes_config(cfg_path, self.repo)
        outcomes.run_plan(run_dir, ocfg, cfg_path, repo_root=self.repo,
                          work_root=self.tmp / "op", log=QUIET)
        outcomes.run_implement(run_dir, ocfg, allow_code_execution=True, repo_root=self.repo,
                               work_root=self.tmp / "oi", log=QUIET)
        with contextlib.redirect_stdout(io.StringIO()):
            outcomes.write_or_check(run_dir)
        self.assertEqual(cli.validate_run(run_dir, self.repo), [])
        failures, out = self.check()
        self.assertEqual(failures, [], out)
        self.assertIn("outcomes-run: outcomes-summary.json and OUTCOMES.md are what", out)
        report = run_dir / "OUTCOMES.md"
        report.write_bytes(report.read_bytes() + b"hand edit\n")
        with contextlib.redirect_stderr(io.StringIO()):
            failures, _ = self.check()
        self.assertTrue(any("OUTCOMES.md is stale" in f for f in failures), failures)

    def test_check_exit_codes(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with mock.patch.object(cli, "run_check", return_value=["FAIL: something"]):
                self.assertEqual(cli.main(["check"]), 1)
            with mock.patch.object(cli, "run_check", return_value=[]):
                self.assertEqual(cli.main(["check"]), 0)

    def test_validate_reports_tampering(self):
        run_dir = self.tmp / "validated"
        cfg = pipeline_config(runs_per_cell=1)
        generate.run_generate(run_dir, cfg, None, None, None, work_root=self.tmp / "w", log=QUIET)
        blinding.run_blind(run_dir)
        judge.run_judge(run_dir, work_root=self.tmp / "w", log=QUIET)
        self.assertEqual(cli.validate_run(run_dir), [])
        run = records.RunDir(run_dir)
        jpath = files(run_dir / "judgments", "*.json")[0]
        rec = records.read_json(jpath)
        rec["scores"][0]["score"] = 9
        records.write_json(jpath, rec)
        stray = run_dir / "generations" / "P9.dfa.gen.r01.md"
        stray.write_text("orphan\n", encoding="utf-8")
        key = run.blind_key()
        blind = run.blind_text(key["entries"][0]["blind_id"])
        blind.write_bytes(blind.read_bytes() + b"x")
        problems = "\n".join(cli.validate_run(run_dir))
        self.assertIn("is greater than maximum", problems)
        self.assertIn("P9.dfa.gen.r01.md: no ok generation record", problems)
        self.assertIn("does not match its sha256 in the key", problems)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["validate", "--run", str(run_dir)]), 1)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def main(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_plan_prints_the_benchmark_matrix_and_touches_nothing(self):
        run_dir = self.tmp / "core-v2-planned"
        code, out, _ = self.main("plan", "--run", str(run_dir))
        self.assertEqual(code, 0)
        self.assertIn("8 prompts x 5 conditions x 1 generator(s) x 5 runs = 200 generations", out)
        self.assertIn("generation calls to make: 200", out)
        self.assertIn("no calls were made", out)
        self.assertFalse(run_dir.exists())
        code, out, _ = self.main("plan", "--prompts", "P7")
        self.assertIn("1 prompts x 5 conditions", out)

    def test_stages_refuse_a_directory_that_is_not_a_run(self):
        for stage in ("blind", "judge", "probe", "lint", "aggregate", "report"):
            code, _, err = self.main(stage, "--run", str(self.tmp))
            self.assertEqual(code, 1, stage)
            self.assertIn("ERROR: ", err)
            self.assertNotIn("Traceback", err)
        code, _, err = self.main("validate", "--run", str(self.tmp))
        self.assertEqual(code, 1)
        self.assertIn("no manifest.json", err)

    def test_config_errors_are_reported_not_raised(self):
        bad = self.tmp / "bad.json"
        bad.write_text(json.dumps({"schema": "dfa-eval/config@1"}), encoding="utf-8")
        code, _, err = self.main("plan", "--config", str(bad))
        self.assertEqual(code, 1)
        self.assertIn('missing required property "prompts"', err)

    def test_import_v1_creates_a_run_that_validates_and_judges(self):
        if importlib.util.find_spec("dfa_eval.importer") is None:
            self.skipTest("eval/dfa_eval/importer.py is not present")
        cfg_path = self.tmp / "judges.json"
        raw = json.loads((ROOT / "eval" / "benchmark.json").read_text(encoding="utf-8"))
        raw["judges"] = [{"id": "fake-a", "provider": "fake", "model": "fake-a"}]
        raw["probe"] = {"enabled": True, "judges": ["fake-a"]}
        cfg_path.write_text(json.dumps(raw), encoding="utf-8")
        run_dir = self.tmp / "v1-rejudge"
        code, out, err = self.main("import-v1", "--run", str(run_dir), "--config", str(cfg_path))
        self.assertEqual(code, 0, err)
        gens = records.RunDir(run_dir).generations()
        self.assertEqual(len(gens), 10)
        self.assertTrue(all(g["provider"] == "imported" and g["seed"] is None for g in gens))
        self.assertEqual(cli.validate_run(run_dir), [])
        key = blinding.run_blind(run_dir)
        self.assertEqual(sum(e["self_score_removed"] for e in key["entries"]), 10)  # 5 x 2
        counts = judge.run_judge(run_dir, work_root=self.tmp / "w", log=QUIET)
        self.assertEqual((counts["ok"], counts["error"]), (20, 0))
        self.assertEqual(cli.validate_run(run_dir), [])
        record = records.read_json(run_dir / "generations" / "P1.baseline.opus-5.5-max.r01.json")
        record["seed"] = 5
        records.write_json(run_dir / "generations" / "P1.baseline.opus-5.5-max.r01.json", record)
        problems = cli.validate_run(run_dir)
        self.assertTrue(any("an imported plan cannot carry a harness seed" in p
                            for p in problems), problems)

    def test_outcomes_arguments_pass_through_untouched(self):
        seen = []
        module = mock.Mock()
        module.main = lambda argv: seen.append(argv) or 0
        with mock.patch.object(cli, "_analysis", return_value=module):
            code, _, _ = self.main("outcomes", "implement", "--help", "--allow-code-execution")
        self.assertEqual(code, 0)
        self.assertEqual(seen, [["implement", "--help", "--allow-code-execution"]])


if __name__ == "__main__":
    unittest.main()
