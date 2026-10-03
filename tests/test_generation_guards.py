"""Guards that keep a generation from being counted when its conditions were not what the config
says: another condition's skill visible to the session, or a second copy of its own
(contamination), or its own skill missing from what the session offered. Standard library only:
    python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))
from dfa_eval import generate  # noqa: E402

CONFIG = {"conditions": {
    "baseline": {"kind": "plain", "description": "x"},
    "dfa": {"kind": "skill", "description": "x", "skill_dir": ".",
            "skill_name": "dependency-first-architect"},
    "dfa-v1": {"kind": "skill", "description": "x", "skill_dir": "eval/conditions/dfa-v1.0.0",
               "skill_name": "dependency-first-architect"},
    "generic-control": {"kind": "skill", "description": "x",
                        "skill_dir": "eval/conditions/generic-architect",
                        "skill_name": "architecture-planner"},
}}


class ContaminationTest(unittest.TestCase):
    def test_baseline_with_the_skill_installed_is_contaminated(self):
        listed = ["code-review", "dependency-first-architect"]
        self.assertEqual(generate.contamination(listed, CONFIG, "baseline"),
                         "dependency-first-architect")

    def test_plugin_qualified_names_count(self):
        listed = ["architecture-planner:architecture-planner"]
        self.assertEqual(generate.contamination(listed, CONFIG, "dfa"), "architecture-planner")
        self.assertEqual(generate.contamination(listed, CONFIG, "baseline"),
                         "architecture-planner")

    def test_a_session_may_see_its_own_skill(self):
        listed = ["code-review", "dependency-first-architect:dependency-first-architect"]
        self.assertIsNone(generate.contamination(listed, CONFIG, "dfa"))
        self.assertIsNone(generate.contamination(listed, CONFIG, "dfa-v1"))  # same skill name

    def test_unrelated_skills_and_unknown_lists_are_fine(self):
        self.assertIsNone(generate.contamination(["code-review", "deep-research"], CONFIG,
                                                 "baseline"))
        self.assertIsNone(generate.contamination(None, CONFIG, "baseline"))
        self.assertIsNone(generate.contamination([], CONFIG, "generic-control"))

    def test_a_same_named_personal_skill_next_to_the_plugin_is_contamination(self):
        # The plugin's skill is listed as <plugin>:<skill>; a plain <skill> beside it is another
        # copy (the README's ~/.claude/skills install, perhaps another version), which the
        # `/<skill>` command may run instead of the packaged one.
        listed = ["compact", "dependency-first-architect",
                  "dependency-first-architect:dependency-first-architect"]
        for condition in ("dfa", "dfa-v1", "baseline", "generic-control"):
            self.assertEqual(generate.contamination(listed, CONFIG, condition),
                             "dependency-first-architect", condition)
        self.assertIn("second copy of its own skill",
                      generate.contamination_error("dependency-first-architect",
                                                   "dependency-first-architect"))
        self.assertIn("belongs to another condition",
                      generate.contamination_error("dependency-first-architect", None))
        # Its own skill listed once, either way, is not contamination.
        self.assertIsNone(generate.contamination(["dependency-first-architect"], CONFIG, "dfa"))


class SkillMissingTest(unittest.TestCase):
    PACKAGE = {"skill_name": "dependency-first-architect"}

    def test_a_listed_skill_set_without_the_conditions_skill_means_no_skill(self):
        why = generate.skill_missing(["compact", "context", "cost", "init", "review"],
                                     self.PACKAGE)
        self.assertIn("skill not offered", why)
        self.assertIn("'dependency-first-architect'", why)

    def test_the_skill_offered_plain_or_namespaced_or_unknown_is_fine(self):
        for listed in (["compact", "dependency-first-architect"],
                       ["dependency-first-architect:dependency-first-architect"], None):
            self.assertIsNone(generate.skill_missing(listed, self.PACKAGE), listed)
        self.assertIsNone(generate.skill_missing(["compact"], None))  # a plain condition

    def test_a_session_that_loaded_the_skill_had_it_whatever_it_listed(self):
        # The transcript shows the skill's files (an injected SKILL.md, a reference file read):
        # only the listing was incomplete, and the plan was written with the skill.
        loaded = [{"path": "SKILL.md", "sha256": "0" * 64, "words": 10}]
        self.assertIsNone(generate.skill_missing(["compact"], self.PACKAGE, loaded))
        self.assertIsNotNone(generate.skill_missing(["compact"], self.PACKAGE, []))


if __name__ == "__main__":
    unittest.main()


class SkillReadsDeniedTest(unittest.TestCase):
    """A skill session whose every read inside the skill's directory failed wrote its plan from
    SKILL.md alone (pilot-models, Haiku 4.5 in permission mode `default`): not counted."""

    PACKAGE = {"skill_name": "dependency-first-architect"}
    INSIDE = "plugins/p1/skills/dependency-first-architect/reference/plan-template.md"

    def test_all_reads_inside_the_skill_failed(self):
        calls = [{"tool": "Read", "path": self.INSIDE, "ok": False}]
        self.assertIn("skill files not readable", generate.skill_reads_denied(calls, self.PACKAGE))

    def test_one_successful_read_is_enough(self):
        calls = [{"tool": "Read", "path": self.INSIDE, "ok": False},
                 {"tool": "Read", "path": self.INSIDE.replace("plan-template", "validation"),
                  "ok": True}]
        self.assertIsNone(generate.skill_reads_denied(calls, self.PACKAGE))

    def test_reading_a_directory_is_the_models_mistake_not_a_denial(self):
        # A Read of a directory fails whatever the permissions; the plan still counts.
        folder = self.INSIDE.rsplit("/", 2)[0]
        calls = [{"tool": "Read", "path": folder + "/reference", "ok": False},
                 {"tool": "Read", "path": folder, "ok": False}]
        self.assertIsNone(generate.skill_reads_denied(calls, self.PACKAGE))

    def test_reads_elsewhere_and_plain_conditions_do_not_count(self):
        calls = [{"tool": "Read", "path": "w1234abcd/README.md", "ok": False}]
        self.assertIsNone(generate.skill_reads_denied(calls, self.PACKAGE))
        self.assertIsNone(generate.skill_reads_denied([{"tool": "Read", "path": self.INSIDE,
                                                        "ok": False}], None))
        self.assertIsNone(generate.skill_reads_denied([], self.PACKAGE))
