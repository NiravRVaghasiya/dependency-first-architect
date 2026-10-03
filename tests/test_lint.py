"""Tests for eval/dfa_eval/lint.py: plan-template v2 format checks.

Two complete fixture plans pass every check; for each check, one targeted edit of a complete
plan fails that check and no other. The committed v1 plans (old template) and the plans written
without the skill fail most checks. run_lint writes one record per ok generation.
Standard library only:  python -m unittest discover -s tests -v
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
from dfa_eval import lint, records, schema, schemas  # noqa: E402
import runbuilder as rb  # noqa: E402

PLANS = ROOT / "tests" / "fixtures" / "plans"
AI_PLAN = (PLANS / "v2-complete-ai.md").read_text(encoding="utf-8")
NON_AI_PLAN = (PLANS / "v2-complete-non-ai.md").read_text(encoding="utf-8")
EXAMPLE_KINDS = {"p1": "AI", "p2": "non-AI", "p3": "non-AI", "p4": "AI", "p5": "non-AI"}


def edit(text, *pairs):
    for old, new in pairs:
        if text.count(old) != 1:
            raise AssertionError(f"fixture edit target must occur exactly once: {old!r}")
        text = text.replace(old, new)
    return text


def without_v0(text):
    """Drop the V0 row and every other citation of V0."""
    text = edit(text, ("| V0 records the floor;", "| Phase 0 records the floor;"),
                ("Exit check (V0):", "Exit check:"),
                ("- Depends on: Phase 0 and V0.", "- Depends on: Phase 0."))
    lines = [line for line in text.split("\n") if not line.startswith("| **V0** |")]
    text = "\n".join(lines)
    assert "V0" not in text
    return text


def swap_last_two_sections(text):
    head, tail = text.split("## 9. Methodology exceptions", 1)
    exceptions, deferred = tail.split("## 10. Deliberately deferred", 1)
    return (head + "## 10. Deliberately deferred" + deferred.rstrip("\n") + "\n\n"
            + "## 9. Methodology exceptions" + exceptions.rstrip("\n") + "\n")


EXCEPTION_TABLE = (
    "| ID | Rule bypassed | Why it does not apply | Replacement validation | Evidence required "
    "| Resumes when |\n|---|---|---|---|---|---|\n")

# (check that must fail, plan, kind, transform): each transform breaks exactly one rule.
VARIANTS = [
    ("sections.present", AI_PLAN, "AI",
     lambda t: edit(t, ("## 4. Walking skeleton (Phase 0)", "## 4. Phase 0 first"))),
    ("self_score.absent", AI_PLAN, "AI",
     lambda t: t + "\n## 11. Self-score\n\nEvery self-check holds.\n"),
    ("budgets.labeled", AI_PLAN, "AI",
     lambda t: edit(t, ("| ASSUMPTION: typical for hosted-LLM chat;", "| typical for hosted-LLM chat;"))),
    ("budgets.unknown_has_no_number", AI_PLAN, "AI",
     lambda t: edit(t, ("| Availability / SLO | — | UNKNOWN", "| Availability / SLO | 99.9% | UNKNOWN"))),
    ("dependencies.typed", AI_PLAN, "AI",
     lambda t: edit(t, (" | validation | specified", " | structural | specified"),
                    (" | risk-security | exposed", " | runtime | exposed"),
                    (" | economic | scaled", " | structural | scaled"),
                    (" | organizational | built", " | runtime | built"))),
    ("gates.columns", AI_PLAN, "AI",
     lambda t: edit(t, ("| Assumption | Validated by |", "| Premise | Validated by |"))),
    ("gates.no_r1_rows", AI_PLAN, "AI",
     lambda t: edit(t, ("| N/A — consistency vs availability |",
                        "| Widget framework | R1: a styling swap | Plain HTML | One page "
                        "| Nothing to check | Never |\n| N/A — consistency vs availability |"))),
    ("gates.r3_backed", AI_PLAN, "AI",
     lambda t: edit(t, ("| [V1](#6-validation-gates) | V1 fails, or",
                        "| A privacy review next quarter | V1 fails, or"))),
    ("validation.complete", AI_PLAN, "AI",
     lambda t: edit(t, ("| Red-team report per release |", "| — |"))),
    ("validation.v0", AI_PLAN, "AI", without_v0),
    ("validation.no_dangling", AI_PLAN, "AI",
     lambda t: edit(t, ("- Depends on: V2 and V3.", "- Depends on: V2, V3 and V9."))),
    ("validation.labeled", AI_PLAN, "AI",
     lambda t: edit(t, ("hit rate@5 ≥ 0.90 (ASSUMPTION: about ±4 points of sampling error at 200 "
                        "questions)", "hit rate@5 ≥ 0.90 on the labeled set"))),
    ("crosscutting.complete", AI_PLAN, "AI",
     lambda t: edit(t, ("| Rate limits per visitor |", "| … |"))),
    ("exceptions.complete", AI_PLAN, "AI",
     lambda t: edit(t, ("## 9. Methodology exceptions\n\nNone.\n",
                        "## 9. Methodology exceptions\n\n" + EXCEPTION_TABLE
                        + "| E1 | Walking skeleton first (principle 6) | Feasibility is the main "
                          "risk | A two-week offline spike; integration risk stays open | Spike "
                          "report | later |\n"))),
    ("deferred.present", AI_PLAN, "AI",
     lambda t: edit(t, ("- Multilingual answers: pulled forward when non-English tickets exceed "
                        "10% of volume.\n- Account-specific answers: pulled forward after a "
                        "privacy review approves account data access.\n",
                        "Multilingual and account-specific answers come afterwards.\n"))),
    ("ai_layer.consistent", AI_PLAN, "AI",
     lambda t: edit(t, ("| 1. Prompt-injection / guardrail defense |", "| 1. Input defense |"))),
]

# More ways to break a check, covering the other branch of its rule.
MORE_VARIANTS = [
    ("sections out of order", "sections.present", AI_PLAN, "AI", swap_last_two_sections),
    ("a stated /20 total without a heading", "self_score.absent", AI_PLAN, "AI",
     lambda t: t + "\nSelf-check total: 19/20\n"),
    ("dependency table without a Kind column", "dependencies.typed", AI_PLAN, "AI",
     lambda t: edit(t, ("What waits | Depends on | Kind |", "What waits | Depends on | Type |"))),
    ("an exception with an empty field", "exceptions.complete", NON_AI_PLAN, "non-AI",
     lambda t: edit(t, ("| Contract tests pass against the stable API |", "| – |"))),
    ("exceptions neither None nor a table", "exceptions.complete", AI_PLAN, "AI",
     lambda t: edit(t, ("## 9. Methodology exceptions\n\nNone.\n",
                        "## 9. Methodology exceptions\n\nWe skip the skeleton for now.\n"))),
    ("exceptions 'None' with more text", "exceptions.complete", AI_PLAN, "AI",
     lambda t: edit(t, ("## 9. Methodology exceptions\n\nNone.\n",
                        "## 9. Methodology exceptions\n\nNone, except that we skip the "
                        "skeleton.\n"))),
    ("no row for phase 0", "crosscutting.complete", NON_AI_PLAN, "non-AI",
     lambda t: edit(t, ("| Phase 0 (skeleton) |", "| Setup |"))),
    ("a non-AI plan whose AI layer is not N/A", "ai_layer.consistent", NON_AI_PLAN, "non-AI",
     lambda t: edit(t, ("N/A — no AI component.", "No AI component in this tool."))),
    ("an R2 gate tier in an R1/R2 cell", "gates.no_r1_rows", NON_AI_PLAN, "non-AI",
     lambda t: edit(t, ("| R2: changing it later", "| R1/R2: changing it later"))),
]


def failing(result):
    return [c["id"] for c in result["checks"] if not c["ok"]]


class CompletePlansTest(unittest.TestCase):
    def test_complete_ai_plan_passes_every_check(self):
        result = lint.lint_plan(AI_PLAN, "AI")
        self.assertEqual(failing(result), [], result["checks"])
        self.assertEqual([c["id"] for c in result["checks"]], list(lint.CHECK_IDS))
        self.assertEqual((result["passed"], result["total"], result["score"]), (16, 16, 1.0))

    def test_complete_non_ai_plan_passes_every_check(self):
        # Unnumbered headings one level down, reordered budget columns, an exceptions table.
        result = lint.lint_plan(NON_AI_PLAN, "non-AI")
        self.assertEqual(failing(result), [], result["checks"])
        self.assertEqual(result["total"], 16)

    def test_kind_none_skips_the_ai_layer_check(self):
        for plan in (AI_PLAN, NON_AI_PLAN):
            result = lint.lint_plan(plan)
            self.assertEqual(result["total"], 15)
            self.assertEqual(result["passed"], 15)
            self.assertNotIn("ai_layer.consistent", [c["id"] for c in result["checks"]])

    def test_ai_plan_linted_as_non_ai_fails_only_the_ai_layer_check(self):
        self.assertEqual(failing(lint.lint_plan(AI_PLAN, "non-AI")), ["ai_layer.consistent"])

    def test_exceptions_may_be_none_in_any_case_with_an_optional_period(self):
        section = "## 9. Methodology exceptions\n\nNone.\n"
        for body in ("None", "**None.**", "none."):
            text = edit(AI_PLAN, (section, f"## 9. Methodology exceptions\n\n{body}\n"))
            self.assertEqual(failing(lint.lint_plan(text, "AI")), [], body)
        text = edit(AI_PLAN, (section, "## 9. Methodology exceptions: None\n"))
        self.assertEqual(failing(lint.lint_plan(text, "AI")), [])

    def test_unknown_targets_may_name_the_phase_and_gate_that_need_them(self):
        # The template asks an UNKNOWN target for the phase that needs it: not a number.
        row = "| Availability / SLO | — | UNKNOWN"
        for target in ("UNKNOWN: supplied before Phase 4", "Needed by Phase 1 (V3 load test)",
                       "Phases 2-3; see §6"):
            text = edit(AI_PLAN, (row, f"| Availability / SLO | {target} | UNKNOWN"))
            self.assertEqual(failing(lint.lint_plan(text, "AI")), [], target)
        text = edit(AI_PLAN, (row, "| Availability / SLO | 99.9% before Phase 4 | UNKNOWN"))
        self.assertEqual(failing(lint.lint_plan(text, "AI")), ["budgets.unknown_has_no_number"])

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            lint.lint_plan(AI_PLAN, "ai")

    def test_result_is_a_valid_lint_record_with_one_line_details(self):
        result = lint.lint_plan(AI_PLAN, "AI")
        record = dict(result, gen_id="P1.dfa.g1.r01")
        self.assertEqual(schema.validate(record, schemas.LINT), [])
        self.assertEqual(result["template"], "plan-template-v2")
        for check in lint.lint_plan("no plan here", "AI")["checks"] + result["checks"]:
            self.assertNotIn("\n", check["detail"])
            self.assertTrue(check["detail"])

    def test_crlf_line_endings_do_not_change_the_result(self):
        crlf = AI_PLAN.replace("\n", "\r\n")
        self.assertEqual(lint.lint_plan(crlf, "AI"), lint.lint_plan(AI_PLAN, "AI"))

    def test_score_is_passed_over_total(self):
        result = lint.lint_plan("# A plan\n\nJust prose.\n", "AI")
        self.assertEqual(result["passed"], 1)  # only self_score.absent
        self.assertEqual(result["score"], 1 / 16)


class SelfScoreRuleTest(unittest.TestCase):
    """self_score.absent fails a stated self-score, not every line with N/20 and 'score' in it."""

    V1_THRESHOLD = ("Zero leaks in 200 attempts (ASSUMPTION: suite size from the security team's "
                    "guidance)")

    def self_score_check(self, text):
        result = lint.lint_plan(text, "AI")
        return failing(result), next(c for c in result["checks"] if c["id"] == "self_score.absent")

    def test_out_of_20_validation_thresholds_are_not_self_scores(self):
        threshold = "at least 18/20 golden questions scored correct by two reviewers (ASSUMPTION)"
        edits = [
            # A table cell (the review's case: it used to fail on this line).
            edit(AI_PLAN, (self.V1_THRESHOLD, threshold)),
            # A bullet and a prose line that name a score threshold out of 20.
            edit(AI_PLAN, ("- Depends on: V2 and V3.", "- Depends on: V2 and V3.\n- Acceptance: "
                           "the golden set scores 18/20 or better (ASSUMPTION)")),
            AI_PLAN + "\nAt least 18/20 sampled answers are scored correct by the reviewers.\n",
            AI_PLAN + "\nThe total is 19/20 checks passing in the smoke suite.\n",
        ]
        for text in edits:
            with self.subTest(text=text[-120:]):
                failed, check = self.self_score_check(text)
                self.assertEqual(failed, [], check)
                self.assertTrue(check["ok"])

    def test_stated_self_scores_still_fail(self):
        for line in ("**Total: 19/20, so it ships.**", "Total 19 / 20", "- Score: 18/20",
                     "1. **Total score:** 19/20", "> Self-assessment: 18/20",
                     "Self-graded total: 17/20", "**8. Eval score: 19/20**",
                     "| **Total** | | **19/20** | |", "## 8) Eval score: 19/20",
                     "### Self-score (19/20)"):
            with self.subTest(line=line):
                text = AI_PLAN + "\n" + line + "\n"
                failed, check = self.self_score_check(text)
                self.assertEqual(failed, ["self_score.absent"], check)
                self.assertIn(f"line {text.split(chr(10)).index(line) + 1}", check["detail"])

    def test_fenced_code_is_not_a_self_score(self):
        failed, _ = self.self_score_check(AI_PLAN + "\n```text\nTotal: 19/20\n## Eval score\n```\n")
        self.assertEqual(failed, [])


class BrokenVariantsTest(unittest.TestCase):
    def test_each_variant_fails_exactly_its_check(self):
        self.assertEqual(sorted(v[0] for v in VARIANTS), sorted(lint.CHECK_IDS))
        for check_id, plan, kind, transform in VARIANTS:
            with self.subTest(check=check_id):
                result = lint.lint_plan(transform(plan), kind)
                self.assertEqual(failing(result), [check_id],
                                 [c for c in result["checks"] if not c["ok"]])
                self.assertEqual(result["passed"], 15)

    def test_other_ways_to_break_a_check(self):
        for name, check_id, plan, kind, transform in MORE_VARIANTS:
            with self.subTest(name):
                result = lint.lint_plan(transform(plan), kind)
                self.assertEqual(failing(result), [check_id],
                                 [c for c in result["checks"] if not c["ok"]])

    def test_details_name_what_is_wrong(self):
        details = {}
        for check_id, plan, kind, transform in VARIANTS:
            result = lint.lint_plan(transform(plan), kind)
            details[check_id] = next(c["detail"] for c in result["checks"] if c["id"] == check_id)
        self.assertIn("missing: walking skeleton", details["sections.present"])
        self.assertIn("Self-score", details["self_score.absent"])
        self.assertIn("Latency", details["budgets.labeled"])
        self.assertIn("Availability / SLO", details["budgets.unknown_has_no_number"])
        self.assertIn("structural", details["dependencies.typed"])
        self.assertIn("Assumption", details["gates.columns"])
        self.assertIn("Widget framework", details["gates.no_r1_rows"])
        self.assertIn("Data-privacy boundary", details["gates.r3_backed"])
        self.assertIn("V1 Evidence", details["validation.complete"])
        self.assertIn("V1, V2, V3", details["validation.v0"])
        self.assertIn("V9", details["validation.no_dangling"])
        self.assertIn("V2", details["validation.labeled"])
        self.assertIn("Security", details["crosscutting.complete"])
        self.assertIn("later", details["exceptions.complete"])
        self.assertIn("no bullet", details["deferred.present"])
        self.assertIn("injection", details["ai_layer.consistent"])
        swapped = lint.lint_plan(swap_last_two_sections(AI_PLAN), "AI")
        self.assertIn("out of order", swapped["checks"][0]["detail"])


class MarkdownParsingTest(unittest.TestCase):
    def test_split_row(self):
        self.assertEqual(lint.split_row("| a | b |"), ["a", "b"])
        self.assertEqual(lint.split_row("a | b"), ["a", "b"])
        self.assertEqual(lint.split_row("| a | b"), ["a", "b"])
        self.assertEqual(lint.split_row("| `x|y` | b |"), ["`x|y`", "b"])
        self.assertEqual(lint.split_row("| ``a`|`b`` | c |"), ["``a`|`b``", "c"])
        self.assertEqual(lint.split_row(r"| a \| b | c |"), [r"a \| b", "c"])
        self.assertEqual(lint.split_row("| unclosed ` tick | c |"), ["unclosed ` tick", "c"])
        self.assertEqual(lint.split_row("| a |  |"), ["a", ""])
        self.assertIsNone(lint.split_row("no pipes here"))

    def test_plain_strips_markup(self):
        self.assertEqual(lint.plain("**ASSUMPTION**: x"), "ASSUMPTION: x")
        self.assertEqual(lint.plain("*UNKNOWN* — _needs_ input"), "UNKNOWN — needs input")
        self.assertEqual(lint.plain("[V1](#6-validation-gates) or [V2][ref]"), "V1 or V2")
        self.assertEqual(lint.plain("`tenant_id` first<br/>then"), "tenant_id first then")
        self.assertEqual(lint.plain(r"a \| b"), "a | b")
        self.assertEqual(lint.plain("latency < 2 s and errors > 1%"), "latency < 2 s and errors > 1%")

    def test_empty_cells(self):
        for cell in ("", " ", "…", "...", "-", "—", "–", "**—**", "<br>", "` `"):
            self.assertTrue(lint.is_empty(cell), repr(cell))
        for cell in ("N/A", "0", "None", "- x"):
            self.assertFalse(lint.is_empty(cell), repr(cell))

    def test_headings_and_tables_inside_fences_are_ignored(self):
        doc = lint.Document("# Title\n\n```\n# not a heading\n| a | b |\n|---|---|\n| 1 | 2 |\n"
                            "```\n\n~~~\n## nor this\n~~~\n## Real\n")
        self.assertEqual([h.text for h in doc.headings], ["Title", "Real"])
        self.assertEqual(doc.tables(0, len(doc.lines)), [])

    def test_heading_text_and_levels(self):
        doc = lint.Document("## 3. **Tradeoff gates** (resolved up front) ##\n#hashtag\n####### seven\n")
        self.assertEqual([(h.level, h.text) for h in doc.headings],
                         [(2, "3. **Tradeoff gates** (resolved up front)")])
        self.assertIn("tradeoff gates", doc.headings[0].norm)

    def test_table_rows_are_padded_and_cut_to_the_header(self):
        doc = lint.Document("| A | B | C |\n|:--|:-:|--:|\n| 1 |\n| 1 | 2 | 3 | 4 |\nafter\n")
        (table,) = doc.tables(0, len(doc.lines))
        self.assertEqual(table.rows, [["1", "", ""], ["1", "2", "3"]])
        self.assertEqual(table.col("b"), 1)
        self.assertIsNone(table.col("d"))


class CommittedPlansTest(unittest.TestCase):
    """The v1 plans use the old template; the plans without the skill use none."""

    def lint_examples(self, arm):
        out = {}
        for folder in sorted((ROOT / "examples").glob("p*-*")):
            text = (folder / f"{arm}-skill.md").read_text(encoding="utf-8")
            out[folder.name] = lint.lint_plan(text, EXAMPLE_KINDS[folder.name[:2]])
        self.assertEqual(len(out), 5)
        return out

    def test_v1_with_skill_plans_fail_the_v2_checks(self):
        for name, result in self.lint_examples("with").items():
            with self.subTest(name):
                failed = failing(result)
                # Structures v2 added or changed, and the v1 self-score section.
                for check_id in ("sections.present", "self_score.absent", "budgets.labeled",
                                 "dependencies.typed", "gates.columns", "validation.v0",
                                 "exceptions.complete"):
                    self.assertIn(check_id, failed)
                self.assertLessEqual(result["passed"], 3)

    def test_v1_self_scores_fail_even_without_the_v1_heading(self):
        # The five plans print their total ("**Total: 20/20.**", or a "/20" in the section
        # heading). With a heading the v1 pattern does not know, the /20 statement alone fails.
        for folder in sorted((ROOT / "examples").glob("p*-*")):
            with self.subTest(folder.name):
                text = (folder / "with-skill.md").read_text(encoding="utf-8")
                renamed = text.replace("## 8. Eval score", "## 8) Eval score")
                self.assertNotEqual(renamed, text)
                result = lint.lint_plan(renamed, EXAMPLE_KINDS[folder.name[:2]])
                check = next(c for c in result["checks"] if c["id"] == "self_score.absent")
                self.assertFalse(check["ok"])
                self.assertIn("states a /20 total", check["detail"])
                self.assertRegex(check["detail"], r"\b(?:19|20)/20\b")

    def test_plans_without_the_skill_fail_most_checks(self):
        for name, result in self.lint_examples("without").items():
            with self.subTest(name):
                self.assertGreaterEqual(len(failing(result)), 14, result["checks"])


class RunLintTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        baseline = (ROOT / "examples" / "p1-rag-chatbot" / "without-skill.md").read_text(
            encoding="utf-8")
        self.run_dir = rb.make_run(self.tmp, [
            rb.gen("P1", "dfa", 1, text=AI_PLAN),
            rb.gen("P1", "baseline", 1, text=baseline),
            rb.gen("P2", "dfa", 1, text=NON_AI_PLAN),
            rb.gen("P2", "baseline", 1, status="error"),
        ], runs_per_cell=1, write_lint=False)

    def test_writes_one_valid_record_per_ok_generation(self):
        written = lint.run_lint(self.run_dir)
        self.assertEqual([r["gen_id"] for r in written],
                         ["P1.baseline.g1.r01", "P1.dfa.g1.r01", "P2.dfa.g1.r01"])
        files = sorted(p.name for p in (self.run_dir / "lint").glob("*.json"))
        self.assertEqual(files, [f"{r['gen_id']}.json" for r in written])
        by_id = {}
        for path in (self.run_dir / "lint").glob("*.json"):
            record = records.read_json(path)
            self.assertEqual(schema.validate(record, schemas.LINT), [])
            by_id[record["gen_id"]] = record
        # Kind comes from the manifest: P1 is an AI prompt, P2 is not.
        self.assertEqual(by_id["P1.dfa.g1.r01"]["score"], 1.0)
        self.assertEqual(by_id["P2.dfa.g1.r01"]["score"], 1.0)
        self.assertEqual(by_id["P1.dfa.g1.r01"]["total"], 16)
        self.assertLess(by_id["P1.baseline.g1.r01"]["score"], 0.2)

    def test_rerun_is_byte_identical_and_check_detects_edits(self):
        lint.run_lint(self.run_dir)
        path = self.run_dir / "lint" / "P1.dfa.g1.r01.json"
        before = path.read_bytes()
        lint.run_lint(self.run_dir)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(lint.check_lint(self.run_dir), (True, ""))
        path.write_bytes(before.replace(b'"passed": 16', b'"passed": 15'))
        ok, diff = lint.check_lint(self.run_dir)
        self.assertFalse(ok)
        self.assertIn("lint/P1.dfa.g1.r01.json", diff)
        path.unlink()
        ok, diff = lint.check_lint(self.run_dir)
        self.assertFalse(ok)
        self.assertIn("is missing", diff)


if __name__ == "__main__":
    unittest.main()
