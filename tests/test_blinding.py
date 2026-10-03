"""Blinding: what judges see must not reveal how a plan was made, and nothing else may change.

Self-score sections must go (all five committed with-skill examples have one), plans without one
must pass through untouched, neutralization must apply the same table to every condition and
leave ordinary words alone, and blind copies plus their key must be deterministic.
Standard library only:  python -m unittest discover -s tests -v
"""

import hashlib
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))

from dfa_eval import blinding, config, generate, records, schema, schemas  # noqa: E402

EXAMPLES = sorted((ROOT / "examples").glob("p*/with-skill.md"))
WITHOUT = sorted((ROOT / "examples").glob("p*/without-skill.md"))
SELF_SCORE = re.compile(r"^#{1,6}\s*(?:\d+\.\s*)?(?:Eval score|Self[- ]?score)\b", re.M | re.I)
TOTAL = re.compile(r"(?im)^\s*\**\s*(total|self-score|score)\b.*\b\d{1,2}\s*/\s*20\b")
V1_PLAN = "# BUILD PLAN: X\n\n## 7. Deliberately deferred\n- Multi-region: later.\n\n"
V1_SCORE_TABLE = ("| # | Dimension | Score | Note |\n|---|---|---|---|\n"
                  "| 1 | Dependency ordering | 2 | Built bottom-up. |\n"
                  "| 2 | Walking skeleton | 2 | Phase 0 runs end to end. |\n\n"
                  "**Total: 19/20, so it ships.**\n")
SUMMARY_PLAN = ("> **Summary:** one service and a ledger.\n> **Assumptions:** 50 requests a "
                "second.\n\n---\n\n## Phase 1\nBuild the ledger first.\n")


def read(path):
    return path.read_bytes().decode("utf-8")


def load_run_eval():
    sys.path.insert(0, str(ROOT / "examples" / "scoring"))
    try:
        import run_eval
    finally:
        sys.path.pop(0)
    return run_eval


class SelfScoreTest(unittest.TestCase):
    def test_all_five_committed_examples_lose_exactly_their_self_score(self):
        self.assertEqual(len(EXAMPLES), 5)
        run_eval = load_run_eval()
        for path in EXAMPLES:
            text = blinding.strip_provenance(read(path))
            stripped, removed = blinding.strip_self_score(text)
            self.assertTrue(removed, path)
            self.assertIsNone(SELF_SCORE.search(stripped), path)
            self.assertIsNone(TOTAL.search(stripped), path)
            self.assertNotRegex(stripped, r"\b\d{1,2}\s*/\s*20\b")
            heading = SELF_SCORE.search(text).start()
            self.assertEqual(stripped, text[:heading].rstrip() + "\n", path)  # the rest is intact
            # Same result as the v1.0.0 blinding the published scores were judged on.
            pid = next(p for p, spec in run_eval.PROMPTS.items() if spec[0] == path.parent.name)
            self.assertEqual(stripped, run_eval.blind_copy(run_eval.plan_body(pid, "with")))

    def test_plans_without_a_self_score_pass_through_unchanged(self):
        for path in WITHOUT:
            text = blinding.strip_provenance(read(path))
            self.assertEqual(blinding.strip_self_score(text), (text, False), path)

    def test_a_section_in_the_middle_ends_at_the_next_heading_of_its_level(self):
        text = ("# Plan\n\n## 1. Phases\nBuild it.\n\n## Self-score\n18 of 20.\n\n"
                "### Detail\nMore scoring.\n\n## 2. Deferred\n- Later.\n")
        self.assertEqual(blinding.strip_self_score(text),
                         ("# Plan\n\n## 1. Phases\nBuild it.\n\n## 2. Deferred\n- Later.\n", True))
        nested = "## Phases\n### Self score\nmine\n## Next\nkept\n"
        self.assertEqual(blinding.strip_self_score(nested), ("## Phases\n## Next\nkept\n", True))

    def test_total_lines_are_removed_wherever_they_stand(self):
        text = ("Intro.\n**Total: 18/20, ships.**\nSelf-score: 17 / 20\n  score 19/20\n"
                "Scored 3/20 shards first.\nKept line.\n")
        self.assertEqual(blinding.strip_self_score(text),
                         ("Intro.\nScored 3/20 shards first.\nKept line.\n", True))

    def test_code_fences_are_not_markdown(self):
        text = ("## Setup\n```bash\n# Self-score helper\necho hi\n```\n## Eval score: 20/20\n"
                "```\n# comment, not a heading\n```\nstill the score\n## After\nkept\n")
        stripped, removed = blinding.strip_self_score(text)
        self.assertTrue(removed)
        self.assertEqual(stripped, "## Setup\n```bash\n# Self-score helper\necho hi\n```\n"
                                   "## After\nkept\n")
        fenced_total = "```\nTotal: 18/20\n```\n"
        self.assertEqual(blinding.strip_self_score(fenced_total), (fenced_total, False))

    def test_provenance_header(self):
        header = "> **P1** · generated\n> Prompt: x\n\n---\n\nBody line.\n"
        self.assertEqual(blinding.strip_provenance(header), "Body line.\n")
        for text in ("Body only.\n", "> quote with no rule\n\nBody.\n",
                     "> quote\nnot quoted\n\n---\n\nBody.\n"):
            self.assertEqual(blinding.strip_provenance(text), text)
        for path in EXAMPLES + WITHOUT:
            self.assertFalse(blinding.strip_provenance(read(path)).startswith("> "))

    def test_self_score_heading_variants_lose_the_whole_section(self):
        # v1.0.0's Step 8 under headings other than "## 8. Eval score": before, only the Total
        # line went, and the per-dimension table reached the judges of the dfa-v1 arm.
        for heading in ("## 8. Eval score", "## 8) Eval score", "## 8 — Eval score",
                        "## 8 - Eval score", "## Step 8: Eval score", "## 8. Evaluation score",
                        "## 8. Self-check against the rubric", "### 8. Self-assessment",
                        "## 8. Score (self-assessed): 19/20", "## Total: 19/20",
                        "## **8. Eval score**", "**8. Eval score**", "__Self-score__",
                        "**Eval score: 19/20**", "## Section 8: Self-score", "# 8. Self-grading"):
            with self.subTest(heading):
                text = V1_PLAN + heading + "\n\n" + V1_SCORE_TABLE
                self.assertEqual(blinding.strip_self_score(text), (V1_PLAN.rstrip() + "\n", True))
        # The section ends at the next heading of its level (a bold one at any heading).
        for heading in ("## 8) Eval score", "**8. Eval score**"):
            text = V1_PLAN + heading + "\n\n" + V1_SCORE_TABLE + "\n## 9. Appendix\nKept.\n"
            self.assertEqual(blinding.strip_self_score(text),
                             (V1_PLAN + "## 9. Appendix\nKept.\n", True))

    def test_sections_that_only_resemble_a_self_score_are_kept(self):
        for text in ("## Self-assessment questionnaire for vendors\n- SOC 2 forms.\n",
                     "## Evaluation score thresholds\nA release needs 0.8.\n",
                     "## Self-checkout flow\nKiosks.\n", "## Self-service onboarding\nForms.\n",
                     "**Self-hosted models**\nLater.\n", "## Scoring model\nScores 0-1000.\n",
                     "## Acceptance: at least 18/20 golden answers scored correct\nTwo raters.\n",
                     "**Total cost**\n$40 a month.\n", "```\n**Eval score**\n```\nKept.\n"):
            with self.subTest(text):
                self.assertEqual(blinding.strip_self_score(text), (text, False))

    def test_a_score_out_of_20_left_after_stripping_is_residue(self):
        clean = blinding.strip_self_score(V1_PLAN + "## 8. Eval score\n\n" + V1_SCORE_TABLE)[0]
        self.assertEqual(blinding.self_score_residue(clean), [])
        left = V1_PLAN + "## 8. Eval score\n" + V1_SCORE_TABLE + "## 9. Notes\nMy score: 19/20.\n"
        stripped, removed = blinding.strip_self_score(left)
        self.assertTrue(removed)
        self.assertEqual(blinding.self_score_residue(stripped), ["My score: 19/20."])
        variants = blinding.blind_variants(left)
        self.assertEqual([v[3] for v in variants], [False, False])  # removed, but not all of it
        self.assertEqual([v[3] for v in blinding.blind_variants(V1_PLAN + "## 8. Eval score\n"
                                                                 + V1_SCORE_TABLE)], [True, True])
        for path in EXAMPLES:  # the committed self-scores go completely
            self.assertEqual([v[3] for v in blinding.blind_variants(read(path), True, True)],
                             [True, True], path)
        self.assertEqual(blinding.self_score_residue("Scored 3/20 shards first.\n"), [])


class NeutralizeTest(unittest.TestCase):
    def test_the_replacement_table(self):
        cases = {
            "Tradeoff gates": "Key decisions", "trade-off gates": "key decisions",
            "a tradeoff gate": "a key decision", "Flip condition": "Revisit trigger",
            "flip conditions": "revisit triggers", "Walking skeleton": "First end-to-end slice",
            "walking skeletons": "first end-to-end slices", "blast radius": "impact",
            "Blast-radius ordering": "Impact ordering", "widest-blast-radius": "widest-impact",
            "Deliberately deferred": "Deferred", "validation gates": "validation checks",
            "Validation gate V1": "Validation check V1", "methodology exceptions": "exceptions",
            "## 2. Dependency map": "## 2. Dependencies", "**R1 defaults:**": "**minor defaults:**",
            "# BUILD PLAN": "# Plan", "via /dependency-first-architect": "via the plan",
            "dependency-first-architect": "the plan", "Dependency-First Architect": "The plan",
            "a dependency-first order": "a the plan order", "R1 easy": "easy to reverse",
            "R2 costly": "costly to reverse", "R3 hard": "hard to reverse",
            "as the template says": "as planned", "per the template": "as planned",
            "as the skill requires": "as planned", "walking\nskeleton": "first end-to-end slice",
        }
        for source, expected in cases.items():
            self.assertEqual(blinding.neutralize(source)[0], expected, source)
        text, count = blinding.neutralize("Tradeoff gates, a walking skeleton and blast radius.")
        self.assertEqual((text, count), ("Key decisions, a first end-to-end slice and impact.", 3))

    def test_what_it_must_not_touch(self):
        for text in ("Store blobs in Cloudflare R2.", "R1, R2 and R3 tiers", "an R3 decision",
                     "Write a skill for it; the skill library", "a build plan for the team",
                     "Build plan", "flip conditional logic", "skeletal walking", "gateway",
                     "tradeoff gatekeeper", "dependency mapping"):
            self.assertEqual(blinding.neutralize(text), (text, 0), text)

    def test_same_table_for_every_condition(self):
        # neutralize sees only text: a baseline plan using the vocabulary is treated identically.
        sentence = "Phase 0 is a walking skeleton; tradeoff gates come with flip conditions.\n"
        self.assertEqual(blinding.neutralize(sentence), blinding.neutralize(sentence))
        self.assertEqual(blinding.blind_variants("Plan: " + sentence)[1][1],
                         blinding.neutralize(blinding.blind_variants("Plan: " + sentence)[0][1])[0])

    def test_committed_examples_lose_the_vocabulary(self):
        terms = ["walking skeleton", "tradeoff gate", "trade-off gate", "flip condition",
                 "blast radius", "blast-radius", "dependency-first", "deliberately deferred"]
        for path in EXAMPLES:
            plain = blinding.blind_variants(read(path))[0][1]
            neutral, count = blinding.neutralize(plain)
            self.assertGreater(count, 0, path)
            for term in terms:
                self.assertNotIn(term, neutral.lower(), (path, term))
            self.assertNotIn("BUILD PLAN", neutral)


class BlindIdsTest(unittest.TestCase):
    def test_ids_are_derived_not_random(self):
        salt = blinding.blind_salt(20261002, "core-v2-x")
        self.assertEqual(salt, hashlib.sha256(b"blind:20261002:core-v2-x").hexdigest()[:16])
        bid = blinding.blind_id(salt, "P1.dfa.g.r01", "plain")
        expected = hashlib.sha256(f"{salt}:P1.dfa.g.r01:plain".encode()).hexdigest()[:12]
        self.assertEqual(bid, expected)
        self.assertNotEqual(bid, blinding.blind_id(salt, "P1.dfa.g.r01", "neutralized"))
        self.assertNotEqual(salt, blinding.blind_salt(20261002, "core-v2-y"))


def tiny_config(**overrides):
    cfg = {
        "schema": "dfa-eval/config@1", "name": "blind-test", "seed": 5,
        "prompts": [{"id": "P1", "title": "t", "kind": "AI",
                     "request": "Plan a customer-support RAG chatbot over our help-center docs."},
                    {"id": "P7", "title": "t", "kind": "non-AI",
                     "request": "Plan a command-line tool that renames a folder of photos by "
                                "their EXIF capture date."}],
        "conditions": {"baseline": {"kind": "plain", "description": "plain"},
                       "dfa": {"kind": "skill", "description": "skill", "skill_dir": ".",
                               "skill_name": "dependency-first-architect"}},
        "generators": [{"id": "fake", "provider": "fake"}],
        "runs_per_cell": 1,
        "blinding": {"strip_self_score": True, "neutralize_terms_for": []},
    }
    cfg.update(overrides)
    return config.check_config(cfg, ROOT)


def make_run(tmp, cfg, name="run1"):
    run_dir = tmp / name
    generate.run_generate(run_dir, cfg, None, 2, None, work_root=tmp / "work",
                          log=lambda message: None)
    return records.RunDir(run_dir)


def replace_plan(run, gid, text):
    """Swap a generation's plan text, keeping its record consistent (as if generated so)."""
    body = text if text.endswith("\n") else text + "\n"
    records.write_text(run.generation_text(gid), body)
    record = records.read_json(run.generation_json(gid))
    record["output_sha256"] = records.sha256_text(body)
    record["metrics"] = generate.text_metrics(body)
    records.write_json(run.generation_json(gid), record)


def snapshot(folder):
    return {p.name: p.read_bytes() for p in sorted(Path(folder).iterdir())}


class RunBlindTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.cfg = tiny_config()
        self.run = make_run(self.tmp, self.cfg)

    def test_key_and_copies(self):
        example = read(EXAMPLES[0]).split(blinding.HEADER_END, 1)[1]  # the model's output
        replace_plan(self.run, "P1.dfa.fake.r01", example)
        key = blinding.run_blind(self.run.root)
        self.assertEqual(schema.validate(key, schemas.BLIND_KEY), [])
        self.assertEqual(key, records.read_json(self.run.blind_key_path))
        ids = [e["blind_id"] for e in key["entries"]]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(ids), 2 * 4)
        self.assertEqual(sorted((e["gen_id"], e["variant"]) for e in key["entries"]),
                         sorted((g, v) for g in ("P1.baseline.fake.r01", "P1.dfa.fake.r01",
                                                 "P7.baseline.fake.r01", "P7.dfa.fake.r01")
                                for v in ("plain", "neutralized")))
        by = {(e["gen_id"], e["variant"]): e for e in key["entries"]}
        plain = by[("P1.dfa.fake.r01", "plain")]
        self.assertTrue(plain["self_score_removed"])
        self.assertTrue(by[("P1.dfa.fake.r01", "neutralized")]["self_score_removed"])
        self.assertFalse(by[("P1.baseline.fake.r01", "plain")]["self_score_removed"])
        text = read(self.run.blind_text(plain["blind_id"]))
        expected = blinding.strip_self_score(blinding.strip_provenance(example))[0]
        self.assertEqual(text, expected)
        self.assertEqual(plain["sha256"], hashlib.sha256(text.encode("utf-8")).hexdigest())
        neutral = by[("P1.dfa.fake.r01", "neutralized")]
        self.assertEqual(read(self.run.blind_text(neutral["blind_id"])),
                         blinding.neutralize(expected)[0])
        self.assertEqual(neutral["replacements"], blinding.neutralize(expected)[1])
        self.assertEqual(plain["replacements"], 0)
        for entry in key["entries"]:  # nothing in a blind copy names its condition or id
            body = read(self.run.blind_text(entry["blind_id"]))
            self.assertNotIn(entry["gen_id"], body)
            self.assertNotIn("> **P", body)

    def test_provenance_headers_are_stripped_from_imported_plans_only(self):
        # A generated plan's opening blockquote is the model's own text (summary, assumptions):
        # judges must see it. Only imported plans (the examples/ format) carry a header.
        replace_plan(self.run, "P1.baseline.fake.r01", SUMMARY_PLAN)
        replace_plan(self.run, "P7.dfa.fake.r01", read(EXAMPLES[0]))
        record = records.read_json(self.run.generation_json("P7.dfa.fake.r01"))
        record.update(provider="imported", seed=None)
        records.write_json(self.run.generation_json("P7.dfa.fake.r01"), record)
        key = blinding.run_blind(self.run.root)
        by = {(e["gen_id"], e["variant"]): e for e in key["entries"]}
        kept = read(self.run.blind_text(by[("P1.baseline.fake.r01", "plain")]["blind_id"]))
        self.assertEqual(kept, SUMMARY_PLAN)
        for variant in ("plain", "neutralized"):
            text = read(self.run.blind_text(by[("P7.dfa.fake.r01", variant)]["blind_id"]))
            self.assertFalse(text.startswith("> "), variant)
            self.assertNotIn("Everything below the line is the model's output", text)
        self.assertEqual(blinding.blind_variants(SUMMARY_PLAN)[0][1], SUMMARY_PLAN)
        self.assertEqual(blinding.blind_variants(SUMMARY_PLAN, strip_header=True)[0][1],
                         "## Phase 1\nBuild the ledger first.\n")

    def test_a_self_score_left_in_a_copy_is_flagged_and_warned_about(self):
        left = (V1_PLAN + "## 8. Eval score\n" + V1_SCORE_TABLE
                + "## 9. Notes\nMy score: 19/20.\n")
        replace_plan(self.run, "P1.dfa.fake.r01", left)
        replace_plan(self.run, "P7.dfa.fake.r01", V1_PLAN + "## 8) Eval score\n" + V1_SCORE_TABLE)
        lines = []
        key = blinding.run_blind(self.run.root, log=lines.append)
        by = {(e["gen_id"], e["variant"]): e for e in key["entries"]}
        self.assertFalse(by[("P1.dfa.fake.r01", "plain")]["self_score_removed"])
        self.assertTrue(by[("P7.dfa.fake.r01", "plain")]["self_score_removed"])
        warnings = [line for line in lines if line.startswith("WARNING:")]
        self.assertEqual(len(warnings), 1, lines)
        self.assertIn("2 blind copies still state a score out of 20", warnings[0])
        self.assertIn("P1.dfa.fake.r01 (plain)", warnings[0])

    def test_identical_plans_get_identical_copies_in_every_condition(self):
        text = read(EXAMPLES[1])
        replace_plan(self.run, "P7.baseline.fake.r01", text)
        replace_plan(self.run, "P7.dfa.fake.r01", text)
        key = blinding.run_blind(self.run.root)
        by = {(e["gen_id"], e["variant"]): e for e in key["entries"]}
        for variant in ("plain", "neutralized"):
            base, dfa = by[("P7.baseline.fake.r01", variant)], by[("P7.dfa.fake.r01", variant)]
            self.assertEqual(base["sha256"], dfa["sha256"])
            self.assertEqual(base["replacements"], dfa["replacements"])
            self.assertNotEqual(base["blind_id"], dfa["blind_id"])

    def test_idempotent_deterministic_and_self_cleaning(self):
        first_key = blinding.run_blind(self.run.root)
        first = snapshot(self.run.root / "blind")
        stray = self.run.root / "blind" / "000000000000.md"
        stray.write_bytes(b"left over\n")
        blinding.run_blind(self.run.root)
        self.assertEqual(snapshot(self.run.root / "blind"), first)
        # The same run in another place blinds to the same bytes (ids depend on seed + run id).
        copy = self.tmp / "elsewhere" / self.run.root.name
        shutil.copytree(self.run.root, copy, ignore=shutil.ignore_patterns("blind"))
        self.assertEqual(blinding.run_blind(copy), first_key)
        self.assertEqual(snapshot(copy / "blind"), first)

    def test_failed_generations_are_not_blinded_and_edits_are_refused(self):
        record = records.read_json(self.run.generation_json("P1.baseline.fake.r01"))
        record.update(status="error", error="boom", output_file=None, output_sha256=None,
                      metrics=None)
        records.write_json(self.run.generation_json("P1.baseline.fake.r01"), record)
        self.run.generation_text("P1.baseline.fake.r01").unlink()
        key = blinding.run_blind(self.run.root)
        self.assertNotIn("P1.baseline.fake.r01", {e["gen_id"] for e in key["entries"]})
        md = self.run.generation_text("P1.dfa.fake.r01")
        md.write_bytes(md.read_bytes() + b"An edit the model never wrote.\n")
        with self.assertRaises(generate.HarnessError) as caught:
            blinding.run_blind(self.run.root)
        self.assertIn("refusing to blind an edited plan", str(caught.exception))

    def test_strip_self_score_can_be_turned_off(self):
        cfg = tiny_config(blinding={"strip_self_score": False, "neutralize_terms_for": []})
        run = make_run(self.tmp, cfg, "run2")
        replace_plan(run, "P1.dfa.fake.r01", read(EXAMPLES[0]))
        key = blinding.run_blind(run.root)
        entry = next(e for e in key["entries"] if e["gen_id"] == "P1.dfa.fake.r01"
                     and e["variant"] == "plain")
        self.assertFalse(entry["self_score_removed"])
        self.assertRegex(read(run.blind_text(entry["blind_id"])), SELF_SCORE)
        self.assertEqual(json.loads(json.dumps(key)), key)


if __name__ == "__main__":
    unittest.main()
