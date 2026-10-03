"""Benchmark config, experiment matrix, and what a generation session is given.

The committed benchmark must load and expand to the documented matrix; a broken config must be
rejected with every problem listed at once; skill packaging must ship exactly the reference files
SKILL.md names, minus the scoring rubric. Standard library only:
    python -m unittest discover -s tests -v
"""

import hashlib
import importlib.util
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))

from dfa_eval import config, records, workspace  # noqa: E402

EVALS = (ROOT / "reference" / "evals.md").read_text(encoding="utf-8")
EVALS_PROMPTS = dict(re.findall(r"\*\*(P\d) \([^)]*\):\*\* \"([^\"]+)\"", EVALS))


def load_run_eval():
    sys.path.insert(0, str(ROOT / "examples" / "scoring"))
    try:
        import run_eval
    finally:
        sys.path.pop(0)
    return run_eval


class BenchmarkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = config.load_config(ROOT / "eval" / "benchmark.json")

    def test_identity_limits_and_defaults(self):
        cfg = self.cfg
        self.assertEqual(cfg["name"], "core-v2")
        self.assertEqual(cfg["runs_per_cell"], 5)
        self.assertEqual(cfg["judge_repeats"], 1)
        self.assertEqual(cfg["limits"], {"max_cost_usd": 400, "jobs": 8, "timeout_s": 3600})
        self.assertEqual(cfg["blinding"], {"strip_self_score": True,
                                           "neutralize_terms_for": ["engineering-quality-v1"]})
        self.assertEqual(cfg["probe"], {"enabled": True, "judges": ["sonnet-5.5"]})
        self.assertEqual(cfg["workspace"],
                         {"readme": "eval/conditions/neutral-workspace-README.md"})
        self.assertEqual(cfg["rubrics"], ["methodology-adherence-v1", "engineering-quality-v1"])

    def test_prompts(self):
        prompts = {p["id"]: p for p in self.cfg["prompts"]}
        self.assertEqual(list(prompts), [f"P{i}" for i in range(1, 9)])
        for pid, request in EVALS_PROMPTS.items():  # byte-identical to reference/evals.md
            self.assertEqual(prompts[pid]["request"], request)
        run_eval = load_run_eval()
        for pid, (_, title, kind, request) in run_eval.PROMPTS.items():
            self.assertEqual((prompts[pid]["title"], prompts[pid]["kind"], prompts[pid]["request"]),
                             (title, kind, request))
        self.assertEqual(prompts["P6"]["request"], "Plan migrating our hospital's on-premises "
                         "patient scheduling system to the cloud without downtime.")
        self.assertEqual(prompts["P7"]["request"], "Plan a command-line tool that renames a "
                         "folder of photos by their EXIF capture date.")
        self.assertEqual(prompts["P8"]["request"],
                         "Plan a real-time fraud-detection pipeline for card transactions.")
        self.assertEqual({pid: p["kind"] for pid, p in prompts.items()},
                         {"P1": "AI", "P2": "non-AI", "P3": "non-AI", "P4": "AI", "P5": "non-AI",
                          "P6": "non-AI", "P7": "non-AI", "P8": "AI"})
        self.assertEqual([prompts[p]["title"] for p in ("P6", "P7", "P8")],
                         ["Hospital scheduling cloud migration", "Photo renaming CLI",
                          "Card fraud detection"])
        self.assertEqual([prompts[p]["tags"] for p in ("P6", "P7", "P8")],
                         [["brownfield", "regulated"], ["small"], ["latency", "ml"]])
        for pid, prompt in prompts.items():
            self.assertEqual(prompt["checklist"], f"eval/rubrics/checklists/{pid}.json")

    def test_conditions(self):
        conds = self.cfg["conditions"]
        self.assertEqual(list(conds), ["baseline", "dfa", "generic-control", "baseline-capped",
                                       "dfa-capped"])
        self.assertEqual({k: (c["kind"], c.get("skill_dir"), c.get("skill_name"),
                              c["length_cap_words"]) for k, c in conds.items()},
                         {"baseline": ("plain", None, None, None),
                          "dfa": ("skill", ".", "dependency-first-architect", None),
                          "generic-control": ("skill", "eval/conditions/generic-architect",
                                              "architecture-planner", None),
                          "baseline-capped": ("plain", None, None, 1500),
                          "dfa-capped": ("skill", ".", "dependency-first-architect", 1500)})
        self.assertEqual(conds["baseline"]["description"],
                         "The request alone, no planning instructions.")
        self.assertEqual(conds["generic-control"]["description"], "Active control: an "
                         "independently written, comparably long generic planning skill.")

    def test_models_and_contrasts(self):
        strip = lambda m: {k: m[k] for k in ("id", "provider", "model", "effort")}  # noqa: E731
        self.assertEqual([strip(g) for g in self.cfg["generators"]],
                         [{"id": "opus-5.5", "provider": "claude-cli", "model": "claude-opus-5-5",
                           "effort": "high"}])
        self.assertEqual([strip(j) for j in self.cfg["judges"]], [
            {"id": "sonnet-5.5", "provider": "claude-cli", "model": "claude-sonnet-5-5",
             "effort": "high"},
            {"id": "fable-5", "provider": "claude-cli", "model": "claude-fable-5",
             "effort": "high"}])
        self.assertEqual([(c["treatment"], c["control"]) for c in self.cfg["contrasts"]],
                         [("dfa", "baseline"), ("dfa", "generic-control"),
                          ("dfa-capped", "baseline-capped"), ("generic-control", "baseline")])

    def test_matrix_order_ids_and_seeds(self):
        gens = config.expand_generations(self.cfg)
        self.assertEqual(len(gens), 8 * 5 * 1 * 5)
        self.assertEqual([g["gen_id"] for g in gens[:6]],
                         ["P1.baseline.opus-5.5.r01", "P1.baseline.opus-5.5.r02",
                          "P1.baseline.opus-5.5.r03", "P1.baseline.opus-5.5.r04",
                          "P1.baseline.opus-5.5.r05", "P1.dfa.opus-5.5.r01"])
        self.assertEqual(gens[-1]["gen_id"], "P8.dfa-capped.opus-5.5.r05")
        order = [(g["prompt_id"], g["condition"], g["run_index"]) for g in gens]
        prompt_rank = {p["id"]: n for n, p in enumerate(self.cfg["prompts"])}
        cond_rank = {c: n for n, c in enumerate(self.cfg["conditions"])}
        self.assertEqual(order, sorted(order, key=lambda o: (prompt_rank[o[0]], cond_rank[o[1]],
                                                             o[2])))
        for gen in gens:
            digest = hashlib.sha256(f"20261002:{gen['gen_id']}".encode("utf-8")).hexdigest()
            self.assertEqual(gen["seed"], int(digest[:8], 16))
        self.assertEqual(len({g["seed"] for g in gens}), len(gens))
        self.assertEqual(gens, config.expand_generations(self.cfg))

    def test_derive_seed_matches_its_definition(self):
        self.assertEqual(config.derive_seed(7, "judge-order"),
                         int(hashlib.sha256(b"7:judge-order").hexdigest()[:8], 16))
        self.assertNotEqual(config.derive_seed(7, "a"), config.derive_seed(8, "a"))

    def test_config_hash_is_semantic(self):
        raw = json.loads((ROOT / "eval" / "benchmark.json").read_text(encoding="utf-8"))
        explicit = dict(raw, judge_repeats=1)  # writing a default out does not change the run
        reordered = dict(reversed(list(raw.items())))
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        shas = set()
        for n, data in enumerate((raw, explicit, reordered)):
            path = tmp / f"c{n}.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            shas.add(config.config_sha256(config.load_config(path)))
        self.assertEqual(len(shas), 1)
        changed = dict(raw, runs_per_cell=4)
        path = tmp / "changed.json"
        path.write_text(json.dumps(changed), encoding="utf-8")
        self.assertNotIn(config.config_sha256(config.load_config(path)), shas)


def minimal(**overrides):
    cfg = {
        "schema": "dfa-eval/config@1",
        "name": "mini",
        "seed": 1,
        "prompts": [{"id": "P1", "title": "t", "kind": "AI",
                     "request": "Plan a customer-support RAG chatbot over our help-center docs.",
                     "checklist": "eval/rubrics/checklists/P1.json"}],
        "conditions": {"baseline": {"kind": "plain", "description": "plain"},
                       "dfa": {"kind": "skill", "description": "skill", "skill_dir": ".",
                               "skill_name": "dependency-first-architect"}},
        "generators": [{"id": "g", "provider": "fake"}],
        "judges": [{"id": "j", "provider": "fake"}],
        "rubrics": ["methodology-adherence-v1", "engineering-quality-v1"],
        "runs_per_cell": 2,
    }
    cfg.update(overrides)
    return cfg


class ProblemsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def problems(self, cfg, repo_root=ROOT):
        path = self.tmp / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        with self.assertRaises(config.ConfigError) as caught:
            config.load_config(path, repo_root)
        self.assertIn("problem", str(caught.exception))
        return caught.exception.problems

    def test_minimal_config_loads_with_defaults(self):
        path = self.tmp / "config.json"
        path.write_text(json.dumps(minimal()), encoding="utf-8")
        cfg = config.load_config(path)
        self.assertEqual(cfg["contrasts"], [])
        self.assertEqual(cfg["probe"], {"enabled": False, "judges": []})
        self.assertEqual(cfg["limits"], {"max_cost_usd": None, "jobs": 4, "timeout_s": 3600})
        self.assertEqual(cfg["workspace"], {"readme": None})
        self.assertEqual(cfg["prompts"][0]["tags"], [])
        self.assertEqual(cfg["generators"][0]["options"], {})
        self.assertEqual(len(config.expand_generations(cfg)), 4)

    def test_partial_sections_keep_their_other_defaults(self):
        path = self.tmp / "config.json"
        path.write_text(json.dumps(minimal(limits={"jobs": 2})), encoding="utf-8")
        self.assertEqual(config.load_config(path)["limits"],
                         {"max_cost_usd": None, "jobs": 2, "timeout_s": 3600})

    def test_schema_errors_are_reported(self):
        cfg = minimal()
        del cfg["seed"]
        cfg["runs_per_cell"] = 0
        problems = self.problems(cfg)
        self.assertIn('$: missing required property "seed"', problems)
        self.assertTrue(any("runs_per_cell" in p and "minimum" in p for p in problems), problems)

    def test_unreadable_and_invalid_files(self):
        with self.assertRaises(config.ConfigError):
            config.load_config(self.tmp / "missing.json")
        bad = self.tmp / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        with self.assertRaises(config.ConfigError) as caught:
            config.load_config(bad)
        self.assertIn("not valid JSON", str(caught.exception))

    def test_every_semantic_problem_is_listed_at_once(self):
        cfg = minimal(
            prompts=[{"id": "P1", "title": "t", "kind": "AI", "request": "r",
                      "checklist": "eval/rubrics/checklists/P2.json"},
                     {"id": "P1", "title": "t", "kind": "AI", "request": "r",
                      "checklist": "eval/rubrics/checklists/missing.json"}],
            conditions={"baseline": {"kind": "plain", "description": "d", "skill_name": "x"},
                        "dfa": {"kind": "skill", "description": "d", "skill_dir": ".",
                                "skill_name": "wrong-name"},
                        "nodir": {"kind": "skill", "description": "d", "skill_dir": "no/such/dir",
                                  "skill_name": "x"},
                        "half": {"kind": "skill", "description": "d"},
                        "bad id": {"kind": "plain", "description": "d"}},
            generators=[{"id": "g", "provider": "fake", "conditions": ["dfa", "ghost"]},
                        {"id": "g", "provider": "claude-cli"},
                        {"id": "c", "provider": "command"}],
            judges=[{"id": "j", "provider": "fake"}],
            rubrics=["methodology-adherence-v1", "no-such-rubric"],
            contrasts=[{"treatment": "dfa", "control": "ghost"},
                       {"treatment": "dfa", "control": "dfa"}],
            probe={"enabled": True, "judges": ["nobody"]},
            blinding={"neutralize_terms_for": ["no-such-rubric-either"]},
            workspace={"readme": "eval/conditions/missing-README.md"},
        )
        problems = self.problems(cfg)
        expected = [
            "duplicate prompt id 'P1'",
            "duplicate generator id 'g'",
            "condition 'baseline' is plain but sets skill_dir/skill_name",
            "is named 'dependency-first-architect', not skill_name 'wrong-name'",
            "condition 'nodir': skill_dir 'no/such/dir' has no SKILL.md",
            "condition 'half' is a skill condition and needs both skill_dir and skill_name",
            "condition id 'bad id' is not a valid id",
            "generator 'g': condition 'ghost' does not exist",
            "generator 'g': provider claude-cli needs a model",
            "generator 'c': provider command needs options.argv",
            "rubric 'no-such-rubric'",
            "contrasts[0]: control 'ghost' is not a condition",
            "contrasts[1]: treatment and control are the same condition",
            "prompt 'P1': checklist 'eval/rubrics/checklists/P2.json' is for prompt 'P2'",
            "checklist 'eval/rubrics/checklists/missing.json' does not exist",
            "probe.judges: 'nobody' is not a configured judge",
            "blinding.neutralize_terms_for: 'no-such-rubric-either' is not a known rubric",
            "workspace.readme: 'eval/conditions/missing-README.md' does not exist",
        ]
        for fragment in expected:
            self.assertTrue(any(fragment in p for p in problems), f"{fragment!r} not in {problems}")

    def test_checklist_rubric_needs_a_checklist_per_prompt(self):
        cfg = minimal()
        cfg["prompts"][0]["checklist"] = None
        problems = self.problems(cfg)
        self.assertEqual(problems, ["prompt 'P1' has no checklist, but rubric "
                                    "'engineering-quality-v1' scores one per prompt"])

    def test_empty_generator_condition_list(self):
        cfg = minimal(generators=[{"id": "g", "provider": "fake", "conditions": []}])
        self.assertTrue(any("conditions is empty" in p for p in self.problems(cfg)))

    def test_skill_naming_a_missing_reference_file(self):
        skill = self.tmp / "repo" / "skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\n\n# Demo\n\nLoad "
                                        "`reference/missing.md` first.\n", encoding="utf-8")
        cfg = minimal(conditions={"s": {"kind": "skill", "description": "d", "skill_dir": "skill",
                                        "skill_name": "demo"}}, rubrics=[], judges=[])
        cfg["prompts"][0]["checklist"] = None
        problems = self.problems(cfg, repo_root=self.tmp / "repo")
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("reference/missing.md, which does not exist", problems[0])

    def test_generator_conditions_restrict_the_matrix(self):
        path = self.tmp / "config.json"
        path.write_text(json.dumps(minimal(generators=[
            {"id": "g", "provider": "fake"}, {"id": "h", "provider": "fake",
                                              "conditions": ["dfa"]}])), encoding="utf-8")
        gens = config.expand_generations(config.load_config(path))
        self.assertEqual([g["gen_id"] for g in gens],
                         ["P1.baseline.g.r01", "P1.baseline.g.r02", "P1.dfa.g.r01", "P1.dfa.g.r02",
                          "P1.dfa.h.r01", "P1.dfa.h.r02"])


class PackagingTest(unittest.TestCase):
    """What a skill condition's session gets: SKILL.md plus exactly the files it names."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_dfa_plugin_ships_exactly_the_named_references(self):
        skill_md = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        _, body = workspace.split_frontmatter(skill_md)
        named = set(re.findall(r"reference/[A-Za-z0-9_.-]+\.md", body))
        plugin, files = workspace.build_plugin(ROOT, "dependency-first-architect",
                                               self.tmp / "plugin")
        packaged = sorted(f["path"] for f in files)
        self.assertEqual(packaged, sorted({"SKILL.md", *named} - {"reference/evals.md"}))
        self.assertEqual(packaged, ["SKILL.md", "reference/ai-systems.md",
                                    "reference/layer-map.md", "reference/plan-template.md",
                                    "reference/validation.md"])
        self.assertTrue((ROOT / "reference" / "evals.md").is_file())  # exists, never shipped
        on_disk = sorted(p.relative_to(plugin).as_posix() for p in plugin.rglob("*")
                         if p.is_file())
        self.assertEqual(on_disk, [".claude-plugin/plugin.json"]
                         + [f"skills/dependency-first-architect/{p}" for p in packaged])
        self.assertEqual(json.loads((plugin / ".claude-plugin" / "plugin.json").read_text()),
                         {"name": "dependency-first-architect", "version": "0.0.0"})
        for f in files:
            data = (ROOT / f["path"]).read_bytes().replace(b"\r\n", b"\n")
            self.assertEqual(f["sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual((plugin / f["packaged"]).read_bytes(), data)
            self.assertEqual(f["words"], len(data.decode("utf-8").split()))

    def test_a_skill_ships_exactly_what_it_names(self):
        skill = self.tmp / "skill"
        (skill / "reference").mkdir(parents=True)
        for name in ("plan-template.md", "evals.md", "unused.md"):
            (skill / "reference" / name).write_text(f"# {name}\n", encoding="utf-8")
        (skill / "SKILL.md").write_text(
            "---\nname: dependency-first-architect\ndescription: >-\n  d\n---\n\n# Skill\n\n"
            "Load `reference/plan-template.md`, then score with `reference/evals.md`.\n",
            encoding="utf-8")
        self.assertEqual(workspace.package_contents(skill),
                         (["reference/plan-template.md", "reference/evals.md"], []))
        plugin, files = workspace.build_plugin(skill, "dependency-first-architect",
                                               self.tmp / "plugin")
        self.assertEqual([f["path"] for f in files],
                         ["SKILL.md", "reference/plan-template.md", "reference/evals.md"])
        self.assertFalse(list(plugin.rglob("unused.md")))  # not named, not shipped

    def test_v2_never_ships_the_rubric_and_v1_ships_what_it_used(self):
        _, v2_files = workspace.build_plugin(ROOT, "dependency-first-architect", self.tmp / "v2")
        self.assertNotIn("reference/evals.md", [f["path"] for f in v2_files])
        v1_dir = ROOT / "eval" / "conditions" / "dfa-v1.0.0"
        _, v1_files = workspace.build_plugin(v1_dir, "dependency-first-architect",
                                             self.tmp / "v1")
        self.assertEqual([f["path"] for f in v1_files],
                         ["SKILL.md", "reference/ai-systems.md", "reference/plan-template.md",
                          "reference/evals.md", "reference/layer-map.md"])  # first-mention order

    def test_control_plugin(self):
        source = ROOT / "eval" / "conditions" / "generic-architect"
        _, files = workspace.build_plugin(source, "architecture-planner", self.tmp / "p")
        self.assertEqual([f["path"] for f in files], ["SKILL.md", "reference/document-template.md",
                                                      "reference/guidance.md"])
        self.assertEqual(workspace.source_path("eval/conditions/generic-architect", "SKILL.md"),
                         "eval/conditions/generic-architect/SKILL.md")
        self.assertEqual(workspace.source_path(".", "reference/x.md"), "reference/x.md")

    def test_wrong_skill_name_and_unsafe_destination(self):
        with self.assertRaises(ValueError):
            workspace.build_plugin(ROOT, "another-name", self.tmp / "p")
        occupied = self.tmp / "occupied"
        occupied.mkdir()
        (occupied / "keep.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(ValueError):
            workspace.build_plugin(ROOT, "dependency-first-architect", occupied)
        self.assertTrue((occupied / "keep.txt").is_file())

    def test_render_instructions_inlines_the_same_files(self):
        text = workspace.render_instructions(ROOT)
        _, body = workspace.split_frontmatter((ROOT / "SKILL.md").read_text(encoding="utf-8"))
        self.assertTrue(text.startswith(body.strip("\n")))
        self.assertNotIn("name: dependency-first-architect", text)
        headings = re.findall(r"^## Reference: (\S+)$", text, re.M)
        self.assertEqual(headings, ["reference/plan-template.md", "reference/ai-systems.md",
                                    "reference/validation.md", "reference/layer-map.md"])
        self.assertIn("already loaded", text)
        rubric_line = "Methodology-adherence rubric (v1)"
        self.assertNotIn(rubric_line, text)  # reference/evals.md is never inlined
        for rel in headings:
            self.assertIn((ROOT / rel).read_text(encoding="utf-8").strip("\n"), text)

    def test_workdirs_are_fresh_empty_and_neutral(self):
        gid = records.gen_id("P1", "dfa-capped", "opus-5.5", 1)
        path = workspace.prepare_workdir(self.tmp, gid, None, salt="s")
        self.assertRegex(path.name, r"^w[0-9a-f]{8}$")
        self.assertEqual(path.name, "w" + hashlib.sha256(f"{gid}s".encode()).hexdigest()[:8])
        for word in ("dfa", "capped", "P1", "opus"):
            self.assertNotIn(word, path.name)
        self.assertEqual(list(path.iterdir()), [])
        (path / "left-over.txt").write_text("x", encoding="utf-8")
        again = workspace.prepare_workdir(self.tmp, gid, "# Workspace\n", salt="s")
        self.assertEqual(again, path)
        self.assertEqual(sorted(p.name for p in again.iterdir()), ["README.md"])
        self.assertNotEqual(workspace.prepare_workdir(self.tmp, gid, None, salt="t"), path)
        readme = (ROOT / "eval" / "conditions" / "neutral-workspace-README.md").read_text(
            encoding="utf-8").lower()
        for word in ("skill", "methodology", "dependency", "template", "baseline", "condition"):
            self.assertNotIn(word, readme)

    def test_skill_descriptions_are_read_as_build_py_reads_them(self):
        spec = importlib.util.spec_from_file_location("dfa_build_for_config_test",
                                                      ROOT / "build.py")
        build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        for folder in (ROOT, ROOT / "eval" / "conditions" / "dfa-v1.0.0",
                       ROOT / "eval" / "conditions" / "generic-architect"):
            text = (folder / "SKILL.md").read_bytes().decode("utf-8")
            self.assertEqual(workspace.skill_description(folder), build.parse_skill(text)[1],
                             folder)
        skill = self.tmp / "s"
        skill.mkdir()
        (skill / "SKILL.md").write_text("---\nname: s\ndescription: |\n  Two\n  lines.\nother: x\n"
                                        "---\n\nBody.\n", encoding="utf-8")
        self.assertEqual(workspace.skill_description(skill), "Two lines.")

    def test_cursor_rules_get_the_adapters_frontmatter(self):
        adapter = (ROOT / "adapters" / "cursor-dependency-first-architect.mdc").read_bytes()
        description = workspace.skill_description(ROOT)
        rule = workspace.cursor_rule("Body.\n\n", description)
        self.assertEqual(rule, f"---\ndescription: {workspace.yaml_value(description)}\nglobs:\n"
                               "alwaysApply: false\n---\n\nBody.\n")
        # For the root skill this is exactly the adapter's frontmatter.
        self.assertTrue(adapter.decode("utf-8").replace("\r\n", "\n").startswith(
            rule.split("\n\nBody.")[0] + "\n\n"))
        self.assertEqual(workspace.yaml_value("plain words"), "plain words")
        self.assertEqual(workspace.yaml_value("key: value"), '"key: value"')
        self.assertEqual(workspace.yaml_value(""), '""')

    def test_build_request(self):
        prompt = {"request": "Plan X."}
        plain = {"kind": "plain"}
        skill = {"kind": "skill", "skill_name": "demo", "length_cap_words": 1500}
        self.assertEqual(workspace.build_request(prompt, plain), ("Plan X.", "Plan X."))
        capped = "Plan X.\n\nLength limit: your entire answer must be at most 1500 words."
        self.assertEqual(workspace.build_request(prompt, skill), (f"/demo {capped}",) * 2)
        self.assertEqual(workspace.build_request(prompt, skill, slash_command=False),
                         (capped, capped))


if __name__ == "__main__":
    unittest.main()
