"""Self-test for `build.py --check`: it must pass on a clean tree and fail on real drift.

Each case copies the skill into a temp dir and runs build.py there, so the real files are
never touched. Standard library only:  python -m unittest discover -s tests -v
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = [
    "build.py",
    "SKILL.md",
    "adapters/AGENTS.md",
    "adapters/cursor-dependency-first-architect.mdc",
]


class CheckModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        for rel in FILES:
            dest = self.tmp / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / rel, dest)

    def run_build(self, *args):
        return subprocess.run(
            [sys.executable, str(self.tmp / "build.py"), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )

    def edit(self, rel, old, new):
        path = self.tmp / rel
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_bytes(text.replace(old, new).encode("utf-8"))

    def test_clean_tree_passes(self):
        result = self.run_build("--check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_hand_edited_adapter_fails(self):
        self.edit("adapters/AGENTS.md", "## Procedure", "## Procedure\n\nhand edit")
        result = self.run_build("--check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("+++ adapters/AGENTS.md (generated from SKILL.md)", result.stdout)

    def test_skill_change_without_rebuild_fails(self):
        # The diff includes non-cp1252 characters (→), so this also covers console encoding.
        self.edit("SKILL.md", "retrieval → model access", "model access → retrieval")
        result = self.run_build("--check")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("out of sync", result.stderr)

    def test_missing_adapter_fails(self):
        (self.tmp / "adapters" / "cursor-dependency-first-architect.mdc").unlink()
        result = self.run_build("--check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("is missing", result.stdout)

    def test_crlf_checkout_is_not_drift(self):
        for path in (self.tmp / "adapters").iterdir():
            path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        result = self.run_build("--check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_check_writes_nothing(self):
        stale = self.tmp / "adapters" / "AGENTS.md"
        stale.write_bytes(b"stale\n")
        self.run_build("--check")
        self.assertEqual(stale.read_bytes(), b"stale\n")

    def test_build_writes_lf_then_check_passes(self):
        (self.tmp / "adapters" / "AGENTS.md").write_bytes(b"stale\n")
        self.assertEqual(self.run_build().returncode, 0)
        for path in (self.tmp / "adapters").iterdir():
            self.assertNotIn(b"\r\n", path.read_bytes(), path.name)
        self.assertEqual(self.run_build("--check").returncode, 0)

    def test_missing_sentinel_fails_without_writing(self):
        self.edit("SKILL.md", "Tradeoff gates", "Decision log")
        before = (self.tmp / "adapters" / "AGENTS.md").read_bytes()
        self.assertEqual(self.run_build("--check").returncode, 1)
        self.assertEqual(self.run_build().returncode, 1)
        self.assertEqual((self.tmp / "adapters" / "AGENTS.md").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
