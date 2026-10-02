"""Self-test for `examples/scoring/tally.py --check`.

The committed scorecard (and README callout) must match the raw judge scores, and an edit on
either side must be caught. Each case runs on a temp copy, so the real files are never touched.
Standard library only:  python -m unittest discover -s tests -v
"""

import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TallyCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        shutil.copytree(ROOT / "examples", self.tmp / "examples",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copyfile(ROOT / "README.md", self.tmp / "README.md")

    def check(self):
        return subprocess.run(
            [sys.executable, str(self.tmp / "examples" / "scoring" / "tally.py"), "--check"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )

    def test_committed_numbers_match_raw_scores(self):
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_changed_raw_score_is_caught(self):
        path = self.tmp / "examples" / "scoring" / "judgments.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        judges = data["plans"][0]["judges"]
        median = int(statistics.median(j["scores"][0] for j in judges))
        for judge in judges:  # move the median itself, so the totals must change
            judge["scores"][0] = (median + 1) % 3
        path.write_bytes(json.dumps(data).encode("utf-8"))
        result = self.check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not match judgments.json", result.stderr)

    def test_edited_plan_is_caught(self):
        path = self.tmp / "examples" / "p1-rag-chatbot" / "with-skill.md"
        path.write_bytes(path.read_bytes() + b"\nOne more line the model never wrote.\n")
        result = self.check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("differs from the model output", result.stderr)

    def test_removed_readme_block_is_caught(self):
        path = self.tmp / "README.md"
        text = path.read_text(encoding="utf-8")
        begin = "<!-- BEGIN GENERATED score callout: examples/scoring/tally.py -->"
        self.assertIn(begin, text)
        path.write_bytes(text.replace(begin, "").encode("utf-8"))
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing its generated-block markers", result.stdout + result.stderr)

    def test_hand_edited_scorecard_is_caught(self):
        path = self.tmp / "examples" / "SCORECARD.md"
        text = path.read_text(encoding="utf-8")
        self.assertIn("| **Mean** |", text)
        path.write_bytes(text.replace("| **Mean** |", "| **Mean (edited)** |", 1).encode("utf-8"))
        result = self.check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("examples/SCORECARD.md does not match", result.stderr)


if __name__ == "__main__":
    unittest.main()
