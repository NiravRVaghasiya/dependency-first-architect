"""Tests for eval/dfa_eval/matrix.py and eval/agents.json: the evaluation matrix.

Every case builds a temp repo root (agents.json, VERSION, optional pilot records, committed runs
under eval/results/) so the real tree is only ever read. The status rules are tested on synthetic
manifests and summaries, each changing one fact of an otherwise qualifying run.
Standard library only:  python -m unittest discover -s tests -v
"""

import copy
import json
import math
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
from dfa_eval import aggregate, matrix, records  # noqa: E402
import runbuilder as rb  # noqa: E402

OPUS = {"id": "opus", "provider": "claude-cli", "model": "claude-opus-5-5", "effort": "high"}
SONNET = {"id": "sonnet", "provider": "claude-cli", "model": "claude-sonnet-5-5", "effort": "high"}
PROMPTS5 = ("P1", "P2", "P3", "P4", "P5")
T975_4 = 2.7764451051977987  # Student t, 0.975 quantile, 4 degrees of freedom
SCORECARD_DELTAS = [8, 12, 3, 4, 9]  # examples/SCORECARD.md, per-prompt with - without (/20)


def matrix_inputs_only(directory, names):
    """copytree filter: keep what the matrix reads (manifest, summary, report, generation records)."""
    if Path(directory).name == "generations":
        return [n for n in names if n.endswith(".md")]
    return [n for n in names if n in ("blind", "raw", "judgments", "lint", "probes")]


def evaluated_gens(runs=3, prompts=PROMPTS5):
    """dfa and baseline plans with spread-out scores, enough for every status criterion."""
    gens = []
    for i, p in enumerate(prompts):
        for r in range(1, runs + 1):
            gens.append(rb.gen(p, "dfa", r, generator="opus",
                               adherence={"sonnet": 17 + (i + r) % 4},
                               eq={"sonnet": 28 + (2 * i + r) % 7}))
            gens.append(rb.gen(p, "baseline", r, generator="opus",
                               adherence={"sonnet": 9 + (i * r) % 5},
                               eq={"sonnet": 22 + (i + 2 * r) % 6}))
    return gens


def make_root(tmp, name="repo", version="2.0.0", pilot=True):
    """A minimal repo tree for the matrix: agents.json, VERSION and, optionally, the pilot."""
    root = Path(tmp) / name
    (root / "eval").mkdir(parents=True)
    shutil.copyfile(ROOT / "eval" / "agents.json", root / "eval" / "agents.json")
    (root / "VERSION").write_bytes(f"{version}\n".encode("utf-8"))
    if pilot:
        (root / "examples" / "scoring").mkdir(parents=True)
        for record in ("generation.json", "judgments.json"):
            shutil.copyfile(ROOT / "examples" / "scoring" / record,
                            root / "examples" / "scoring" / record)
    return root


def add_run(root, gens, run_id="core-run", generators=(OPUS,), judges=(SONNET,),
            summarize=True, **kwargs):
    """A committed run under root/eval/results/, with summary.json and a REPORT.md."""
    run_dir = rb.make_run(root / "eval" / "results", gens, run_id=run_id,
                          prompts=kwargs.pop("prompts", PROMPTS5), generators=list(generators),
                          judges=list(judges), runs_per_cell=kwargs.pop("runs_per_cell", 3),
                          **kwargs)
    if summarize:
        # The matrix never reads bootstrap intervals; fewer resamples keep the tests fast.
        with mock.patch.object(aggregate, "BOOTSTRAP_RESAMPLES", 200):
            aggregate.write_summary(run_dir)
        (run_dir / "REPORT.md").write_bytes(b"report\n")
    return run_dir


class MatrixTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def make_root(self, name="repo", version="2.0.0", pilot=True):
        return make_root(self.tmp, name, version, pilot)

    def add_run(self, root, gens, **kwargs):
        return add_run(root, gens, **kwargs)

    def rows(self, root, agent=None):
        rows, _, _ = matrix.matrix_rows(root)
        return [r for r in rows if agent is None or r["agent"] == agent]

    def run_row(self, root, run_id="core-run"):
        hits = [r for r in self.rows(root) if f"run {run_id}" in r["configuration"]]
        self.assertEqual(len(hits), 1, hits)
        return hits[0]


class AgentsFileTest(unittest.TestCase):
    def test_declares_the_three_agents_and_their_artifacts_exist(self):
        agents = matrix.load_agents(ROOT)
        self.assertEqual([a["agent"] for a in agents], ["Claude Code", "Cursor", "Codex"])
        claude, cursor, codex = agents
        self.assertEqual(claude["artifact"], "SKILL.md + reference/")
        self.assertTrue((ROOT / "SKILL.md").is_file() and (ROOT / "reference").is_dir())
        for entry in (cursor, codex):
            self.assertTrue((ROOT / entry["artifact"]).is_file(), entry["artifact"])
            self.assertIn(Path(entry["artifact"]).name, entry["install"])
        self.assertIn("~/.claude/skills/dependency-first-architect", claude["install"])

    def test_install_commands_match_the_readme(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for entry in matrix.load_agents(ROOT):
            self.assertIn(entry["install"], readme, entry["agent"])

    def test_malformed_agents_file_is_rejected(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        (tmp / "eval").mkdir()
        good = {"agent": "A", "artifact": "x", "install": "y", "notes": "z"}
        for bad in ([dict(good, extra="no")], [{"agent": "A", "artifact": "x", "install": "y"}],
                    [good, dict(good)], [dict(good, notes="")], {"agent": "A"}):
            (tmp / "eval" / "agents.json").write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(ValueError, msg=bad):
                matrix.load_agents(tmp)


class StatusRulesTest(MatrixTestCase):
    """One qualifying run, then one fact changed at a time."""

    @classmethod
    def setUpClass(cls):
        cls.shared = Path(tempfile.mkdtemp())
        cls.addClassCleanup(shutil.rmtree, cls.shared)
        add_run(make_root(cls.shared), evaluated_gens())

    def setUp(self):
        super().setUp()
        self.root = self.tmp / "qualifying"
        shutil.copytree(self.shared / "repo", self.root, ignore=matrix_inputs_only)
        self.run_dir = self.root / "eval" / "results" / "core-run"

    def edit_summary(self, change):
        path = self.run_dir / "summary.json"
        summary = records.read_json(path)
        change(summary)
        records.write_json(path, summary)

    def test_qualifying_run_is_experimentally_evaluated(self):
        row = self.run_row(self.root)
        self.assertEqual(row["status"], "Experimentally evaluated")
        self.assertEqual((row["agent"], row["model"], row["skill_version"], row["runs"]),
                         ("Claude Code", "claude-opus-5-5 (effort high)", "2.0.0", 30))
        self.assertIn("[fixture-bench run core-run](../eval/results/core-run/REPORT.md)",
                      row["configuration"])
        # A committed run at the current version: no separate "Supported" row for Claude Code.
        self.assertEqual([r["status"] for r in self.rows(self.root, "Claude Code")],
                         ["Experimentally evaluated", "Tested"])

    def test_measured_result_quotes_the_summary(self):
        summary = records.read_json(self.run_dir / "summary.json")
        parts = []
        for metric, label in (("adherence_total", "adherence"), ("eq_total", "EQ")):
            entry = next(e for e in summary["contrasts"] if e["metric"] == metric)
            lo, hi = entry["diff"]["ci95"]
            parts.append(f"{label} {entry['mean_diff']:+.1f} [{lo:.1f}, {hi:.1f}]")
        self.assertEqual(self.run_row(self.root)["measured"],
                         "dfa − baseline: " + "; ".join(parts) + " (t-CI over 5 prompts)")

    def test_a_run_of_length_capped_arms_quotes_their_contrast(self):
        gens = [dict(g, condition=f"{g['condition']}-capped") for g in evaluated_gens()]
        root = self.make_root("capped")
        self.add_run(root, gens, conditions=("baseline-capped", "dfa-capped"),
                     contrasts=(("dfa-capped", "baseline-capped"),))
        row = self.run_row(root)
        self.assertEqual(row["status"], "Experimentally evaluated")
        self.assertRegex(row["measured"], r"^dfa-capped − baseline-capped: adherence [+−-]\d")
        self.assertIn("3 runs per cell", row["configuration"])

    def test_fewer_than_three_runs_in_a_cell(self):
        def two_runs(summary):
            for cell in summary["cells"]:
                if cell["condition"] == "dfa" and cell["prompt"] == "P3":
                    cell["n_generations"] = 2
        self.edit_summary(two_runs)  # P3 no longer counts: 4 qualifying prompts
        self.assertEqual(self.run_row(self.root)["status"], "Tested")

    def test_fewer_than_five_prompts(self):
        root = self.make_root("four-prompts")
        self.add_run(root, evaluated_gens(prompts=PROMPTS5[:4]), prompts=PROMPTS5[:4])
        self.assertEqual(self.run_row(root)["status"], "Tested")

    def test_judge_on_the_generator_model(self):
        def same_model(summary):
            summary["run"]["judges"][0]["model_requested"] = OPUS["model"]
        self.edit_summary(same_model)
        self.assertEqual(self.run_row(self.root)["status"], "Tested")

    def test_skill_plans_that_could_not_read_the_skill(self):
        def denied(summary):
            summary["flags"].append("condition dfa, generator opus: 15 of 15 plans could not read "
                                    "the skill's files (all 30 reads inside its directory "
                                    "failed), so they follow SKILL.md alone, not the skill as "
                                    "shipped")
        self.edit_summary(denied)
        self.assertEqual(self.run_row(self.root)["status"], "Tested")

    def test_judge_whose_judgments_were_not_used(self):
        def unused(summary):
            self.assertGreater(summary["run"]["judges"][0]["n_used"], 0)
            summary["run"]["judges"][0]["n_used"] = 0
        self.edit_summary(unused)
        self.assertEqual(self.run_row(self.root)["status"], "Tested")

    def test_not_blinded(self):
        def unblinded(summary):
            summary["generated_from"]["blind_key_entries"] = 0
        self.edit_summary(unblinded)
        self.assertEqual(self.run_row(self.root)["status"], "Tested")

    def test_summary_without_confidence_intervals(self):
        def no_ci(summary):
            for entry in summary["contrasts"]:
                entry["diff"]["ci95"] = None
        self.edit_summary(no_ci)
        row = self.run_row(self.root)
        self.assertEqual(row["status"], "Tested")
        self.assertRegex(row["measured"], r"^dfa − baseline: adherence \+\d+\.\d n/a; EQ ")

    def test_no_summary(self):
        (self.run_dir / "summary.json").unlink()
        row = self.run_row(self.root)
        self.assertEqual((row["status"], row["runs"], row["measured"]), ("Tested", 30, "—"))

    def test_no_plans(self):
        def nothing(summary):
            for cell in summary["cells"]:
                cell["n_generations"] = 0
        self.edit_summary(nothing)
        row = self.run_row(self.root)
        self.assertEqual((row["status"], row["runs"]), ("Supported", 0))

    def test_a_contrast_without_the_skill_does_not_qualify(self):
        root = self.make_root("generic-only")
        gens = [dict(g, condition="generic-control" if g["condition"] == "dfa" else g["condition"])
                for g in evaluated_gens()]
        self.add_run(root, gens, conditions=("baseline", "generic-control"),
                     contrasts=(("generic-control", "baseline"),))
        # Same numbers, but no arm runs the dependency-first-architect skill.
        self.assertEqual(self.run_row(root)["status"], "Tested")

    def test_classify_boundaries(self):
        base = {"plans": 30, "blinded": True, "independent_judge": True,
                "contrasts": [{"paired_prompts": 5, "has_ci": True}]}
        self.assertEqual(matrix.classify(base), "Experimentally evaluated")
        for change in ({"plans": 0}, {"blinded": False}, {"independent_judge": False},
                       {"contrasts": [{"paired_prompts": 4, "has_ci": True}]},
                       {"contrasts": [{"paired_prompts": 5, "has_ci": False}]},
                       {"contrasts": []}):
            facts = dict(copy.deepcopy(base), **change)
            expected = "Supported" if change.get("plans") == 0 else "Tested"
            self.assertEqual(matrix.classify(facts), expected, change)
        two = dict(base, contrasts=[{"paired_prompts": 5, "has_ci": False},
                                    {"paired_prompts": 6, "has_ci": True}])
        self.assertEqual(matrix.classify(two), "Experimentally evaluated")


class RowsTest(MatrixTestCase):
    def test_pilot_row(self):
        root = self.make_root()
        (pilot,) = [r for r in self.rows(root) if "v1.0.0 pilot" in r["configuration"]]
        self.assertEqual(pilot["agent"], "Claude Code")
        self.assertEqual(pilot["model"], "claude-opus-5-5 (effort max)")
        self.assertEqual(pilot["skill_version"], "1.0.0 (commit 3ba6b70)")
        self.assertEqual(pilot["runs"], 10)
        # 1 run per arm and judges on the generator's model: plans exist, but not an experiment.
        self.assertEqual(pilot["status"], "Tested")
        self.assertIn("5 prompts; 1 run per arm; 3 judges on the generator's own model",
                      pilot["configuration"])
        mean = sum(SCORECARD_DELTAS) / 5
        sd = math.sqrt(sum((d - mean) ** 2 for d in SCORECARD_DELTAS) / 4)
        half = T975_4 * sd / math.sqrt(5)
        self.assertEqual(pilot["measured"],
                         f"with − without: adherence (v1 rubric) +{mean:.1f} "
                         f"[{mean - half:.1f}, {mean + half:.1f}] (t-CI over 5 prompts); "
                         "EQ not measured")
        self.assertIn("+7.2 [2.6, 11.8]", pilot["measured"])

    def test_pilot_stays_tested_even_with_an_independent_judge(self):
        root = self.make_root()
        path = root / "examples" / "scoring" / "judgments.json"
        data = records.read_json(path)
        data["model"] = "some-other-model"
        records.write_json(path, data)
        (pilot,) = [r for r in self.rows(root) if "v1.0.0 pilot" in r["configuration"]]
        self.assertEqual(pilot["status"], "Tested")  # still 1 run per arm, no summary.json
        self.assertNotIn("generator's own model", pilot["configuration"])

    def test_without_runs_every_agent_is_supported(self):
        root = self.make_root(pilot=False)
        rows = self.rows(root)
        self.assertEqual([(r["agent"], r["status"], r["runs"], r["skill_version"]) for r in rows],
                         [("Claude Code", "Supported", 0, "2.0.0"),
                          ("Cursor", "Supported", 0, "2.0.0"),
                          ("Codex", "Supported", 0, "2.0.0")])
        self.assertTrue(all(r["measured"] == "—" and r["model"] == "—" for r in rows))

    def test_old_version_runs_keep_a_supported_row_for_the_current_version(self):
        root = self.make_root()
        rows = self.rows(root, "Claude Code")
        self.assertEqual([(r["skill_version"], r["status"]) for r in rows],
                         [("2.0.0", "Supported"), ("1.0.0 (commit 3ba6b70)", "Tested")])

    def test_malformed_committed_manifest_fails_loudly(self):
        root = self.make_root(pilot=False)
        bad = root / "eval" / "results" / "bad-run"
        bad.mkdir(parents=True)
        (bad / "manifest.json").write_text('{"schema": "dfa-eval/manifest@1"}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "eval/results/bad-run/manifest.json"):
            matrix.render_matrix(root)

    def test_agent_names_come_from_the_provider_or_the_config(self):
        root = self.make_root(pilot=False)
        codex = {"id": "codex", "provider": "command", "model": "gpt-x",
                 "options": {"agent": "Codex", "argv": ["codex", "exec", "{prompt_file}"]}}
        api = {"id": "api", "provider": "openai-compatible", "model": "gpt-y"}
        gens = [rb.gen("P1", c, 1, generator=g["id"]) for g in (codex, api)
                for c in ("baseline", "dfa")]
        self.add_run(root, gens, generators=(codex, api), judges=(), prompts=("P1",),
                     runs_per_cell=1, rubrics=())
        rows = self.rows(root)
        self.assertEqual([(r["agent"], r["status"]) for r in rows],
                         [("Claude Code", "Supported"), ("Cursor", "Supported"),
                          ("Codex", "Tested"), ("openai-compatible", "Tested")])
        self.assertEqual(rows[2]["model"], "gpt-x")
        self.assertEqual(rows[2]["runs"], 2)


class RenderAndCheckTest(MatrixTestCase):
    def test_render_is_deterministic_with_definitions_above_the_table(self):
        root = self.make_root()
        self.add_run(root, evaluated_gens())
        text = matrix.render_matrix(root)
        self.assertEqual(matrix.render_matrix(root), text)
        self.assertTrue(text.startswith(matrix.HEADER + "\n"))
        table = text.index("| Agent | Model | Skill version | Configuration | Runs | Status | "
                           "Measured result |")
        for status in ("**Supported**", "**Tested**", "**Experimentally evaluated**"):
            self.assertLess(text.index(status), table)
        self.assertIn("a committed run with ≥ 5 prompts, ≥ 3 runs per\n  (prompt, condition) cell",
                      text)
        self.assertIn("| Claude Code | claude-opus-5-5 (effort high) | 2.0.0 |", text)
        self.assertIn("| 30 | Experimentally evaluated | dfa − baseline: adherence", text)
        self.assertIn("| Cursor | — | 2.0.0 |", text)
        self.assertIn("## Install artifacts (skill version 2.0.0)", text)

    def test_write_then_check(self):
        root = self.make_root()
        ok, diff = matrix.check_matrix(root)
        self.assertFalse(ok)
        self.assertIn("docs/evaluation-matrix.md is missing", diff)
        text = matrix.write_matrix(root)
        path = root / "docs" / "evaluation-matrix.md"
        self.assertEqual(path.read_bytes(), text.encode("utf-8"))
        self.assertEqual(matrix.check_matrix(root), (True, ""))
        path.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
        self.assertEqual(matrix.check_matrix(root), (True, ""))
        path.write_bytes(text.replace("| Tested |", "| Experimentally evaluated |").encode("utf-8"))
        ok, diff = matrix.check_matrix(root)
        self.assertFalse(ok)
        self.assertIn("docs/evaluation-matrix.md (committed)", diff)
        # A new committed run makes the committed matrix stale.
        matrix.write_matrix(root)
        self.add_run(root, evaluated_gens(runs=1), runs_per_cell=1)
        self.assertFalse(matrix.check_matrix(root)[0])

    def test_real_repository_renders(self):
        text = matrix.render_matrix(ROOT)
        pilot = next(line for line in text.splitlines() if "v1.0.0 pilot" in line)
        self.assertIn("| Tested |", pilot)
        for agent in ("Claude Code", "Cursor", "Codex"):
            self.assertIn(f"| {agent} |", text)


if __name__ == "__main__":
    unittest.main()
