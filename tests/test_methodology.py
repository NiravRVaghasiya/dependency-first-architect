"""Structural contract of the methodology: SKILL.md, the plan template, and the eval prompts.

These tests check that the documents agree with each other and keep the rules the project
promises (the strengths kept from v1 and the v2 additions). They cannot show that a model follows
the rules; that is what eval/ measures. Standard library only:
    python -m unittest discover -s tests -v
"""

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = (ROOT / "SKILL.md").read_text(encoding="utf-8")
TEMPLATE = (ROOT / "reference" / "plan-template.md").read_text(encoding="utf-8")
EVALS = (ROOT / "reference" / "evals.md").read_text(encoding="utf-8")
CC_OPEN, CC_CLOSE = "<!-- claude-code-only -->", "<!-- /claude-code-only -->"


def section(text, heading):
    """The text under a `## heading` up to the next `## ` heading, whitespace collapsed."""
    match = re.search(rf"^## {re.escape(heading)}.*?$(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not match:
        raise AssertionError(f"missing section: {heading}")
    return re.sub(r"\s+", " ", match.group(1))


def table_header(text, first_column):
    """The header cells of the first pipe table whose first header cell is `first_column`."""
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.lstrip().startswith("|") and cells and cells[0] == first_column:
            return cells
    raise AssertionError(f"no table starting with column {first_column!r}")


def normalize(title):
    title = re.sub(r"\(.*?\)", "", title).lower()
    return re.sub(r"\s+", " ", title).strip(" .;")


class FrontmatterTest(unittest.TestCase):
    def test_description_fits_the_agent_skills_limit(self):
        sys.path.insert(0, str(ROOT))
        try:
            import build
        finally:
            sys.path.pop(0)
        name, description, _ = build.parse_skill(SKILL)
        self.assertEqual(name, "dependency-first-architect")
        self.assertLessEqual(len(description), 1024)
        for word in ("plan", "architect", "sequence", "order"):
            self.assertIn(word, description)


class ReferencesTest(unittest.TestCase):
    def test_every_reference_named_in_skill_exists(self):
        names = set(re.findall(r"reference/[A-Za-z0-9_.-]+\.md", SKILL))
        self.assertTrue(names)
        for name in sorted(names):
            self.assertTrue((ROOT / name).is_file(), name)

    def test_relative_links_in_reference_files_resolve(self):
        for path in sorted((ROOT / "reference").glob("*.md")):
            text = path.read_text(encoding="utf-8")
            for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", text):
                if re.match(r"[a-z]+://", target):
                    continue
                self.assertTrue((path.parent / target).exists(), f"{path.name}: {target}")

    def test_claude_code_only_markers_are_balanced(self):
        depth = 0
        for token in re.findall(re.escape(CC_OPEN) + "|" + re.escape(CC_CLOSE), SKILL):
            depth += 1 if token == CC_OPEN else -1
            self.assertIn(depth, (0, 1), "nested or unbalanced claude-code-only markers")
        self.assertEqual(depth, 0)

    def test_reference_loads_are_claude_code_only(self):
        # The Cursor and Codex adapters do not ship reference/, so every instruction that loads a
        # reference file must sit inside a span that build.py strips from the adapters.
        outside = re.sub(re.escape(CC_OPEN) + ".*?" + re.escape(CC_CLOSE), "", SKILL, flags=re.S)
        self.assertNotIn("reference/", outside)


class TemplateTest(unittest.TestCase):
    def test_step_10_section_list_matches_the_template(self):
        step10 = SKILL[SKILL.index("### Step 10"):]
        listed = re.search(r"Sections, in order:(.*?)\.\s", step10, re.S).group(1)
        names = [normalize(re.sub(r"^\s*\d+\s+", "", part)) for part in listed.split(";")]
        headings = [normalize(h) for h in re.findall(r"^## \d+\. (.+)$", TEMPLATE, re.M)]
        self.assertEqual(len(headings), 10)
        self.assertEqual(names, headings)

    def test_template_has_no_score_section(self):
        self.assertNotRegex(TEMPLATE, r"(?im)^#+.*\b(eval score|self[- ]?score)\b")
        self.assertNotIn("/20", TEMPLATE)
        self.assertNotIn("/20", SKILL)
        self.assertIn("Do not print a score", SKILL)

    def test_validation_gate_fields_agree(self):
        fields = ["Hypothesis", "Method", "Acceptance threshold", "Evidence", "Unlocks"]
        gates = section(SKILL, "Validation gates")
        for field in fields:
            self.assertIn(f"**{field}**", gates)
        header = table_header(TEMPLATE, "ID")
        for field in ["ID", *fields, "Phase"]:
            self.assertTrue(any(h.startswith(field) for h in header), (field, header))

    def test_exception_fields_agree(self):
        fields = ["Rule bypassed", "Why it does not apply", "Replacement validation",
                  "Evidence required", "Resumes when"]
        exceptions = section(SKILL, "Methodology exceptions")
        for field in fields:
            self.assertIn(f"**{field}**", exceptions)
        header = [h for h in table_header(TEMPLATE, "ID") if h]
        exception_header = None
        for line in TEMPLATE.splitlines():
            if line.startswith("| ID | Rule bypassed"):
                exception_header = [c.strip() for c in line.strip().strip("|").split("|")]
        self.assertIsNotNone(exception_header, header)
        self.assertEqual(exception_header, ["ID", *fields])

    def test_tradeoff_gate_columns_match_step_3(self):
        header = table_header(TEMPLATE, "Decision")
        self.assertEqual([normalize(h) for h in header],
                         ["decision", "reversibility", "default", "assumption", "validated by",
                          "flip condition"])
        step3 = SKILL[SKILL.index("### Step 3"):SKILL.index("### Step 4")]
        for part in ("Decision", "Reversibility", "Default", "Assumption", "Validated by",
                     "Flip condition"):
            self.assertIn(part, step3)

    def test_labels_are_defined_and_used(self):
        for label in ("REQUIREMENT", "BASELINE", "ASSUMPTION", "UNKNOWN"):
            self.assertIn(label, SKILL)
            self.assertIn(label, TEMPLATE)


class MethodologyContentTest(unittest.TestCase):
    def test_seven_dependency_kinds(self):
        start = SKILL.index("## Dependencies are not only code")
        table = SKILL[start:SKILL.index("\n## ", start + 1)]
        kinds = re.findall(r"^\| ([A-Z][A-Za-z /]+?) \|", table, re.M)
        self.assertEqual(kinds[1:], ["Structural", "Runtime", "Decision", "Validation",
                                     "Risk / security", "Organizational", "Economic"])

    def test_reversibility_tiers_have_cost_definitions(self):
        gates = section(SKILL, "Validation gates")
        self.assertRegex(gates, r"\*\*R1\*\* \(hours to days")
        self.assertRegex(gates, r"\*\*R2\*\* \(weeks")
        self.assertRegex(gates, r"\*\*R3\*\* \(months")

    def test_budgets_named(self):
        principle = re.search(r"^8\. \*\*Budgets.*?(?=^9\. )", SKILL, re.M | re.S).group(0)
        principle = re.sub(r"\s+", " ", principle)
        for budget in ("latency", "throughput", "availability/SLO", "RPO/RTO", "resource use",
                       "AI inference cost", "storage cost", "operational complexity"):
            self.assertIn(budget, principle)

    def test_v1_strengths_are_kept(self):
        for phrase in ("BUILD PLAN ordered by dependency, not visibility", "walking skeleton",
                       "blast radius", "Tradeoff gates", "Deliberately deferred",
                       "retrieval → model access → memory → orchestration → routing → feedback",
                       "Observability is day-zero"):
            self.assertIn(phrase, SKILL)
        for concern in ("security", "observability", "reproducibility", "resilience"):
            self.assertIn(concern, SKILL.lower())

    def test_exceptions_cannot_expose(self):
        exceptions = section(SKILL, "Methodology exceptions")
        self.assertIn("never exposes real users, data, or money", exceptions)
        self.assertIn("never N/A", exceptions)


class PromptSyncTest(unittest.TestCase):
    """The five original prompts must stay identical everywhere they are used."""

    def evals_prompts(self):
        return dict(re.findall(r"\*\*(P\d) \([^)]*\):\*\* \"([^\"]+)\"", EVALS))

    def test_evals_md_lists_five_prompts(self):
        self.assertEqual(sorted(self.evals_prompts()), ["P1", "P2", "P3", "P4", "P5"])

    def test_v1_harness_uses_the_same_prompts(self):
        sys.path.insert(0, str(ROOT / "examples" / "scoring"))
        try:
            import run_eval
        finally:
            sys.path.pop(0)
        self.assertEqual({pid: spec[3] for pid, spec in run_eval.PROMPTS.items()},
                         self.evals_prompts())

    def test_benchmark_uses_the_same_prompts(self):
        config = json.loads((ROOT / "eval" / "benchmark.json").read_text(encoding="utf-8"))
        requests = {p["id"]: p["request"] for p in config["prompts"]}
        for pid, request in self.evals_prompts().items():
            self.assertEqual(requests.get(pid), request, pid)


if __name__ == "__main__":
    unittest.main()
