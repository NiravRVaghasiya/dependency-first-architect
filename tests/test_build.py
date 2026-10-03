"""Self-test for build.py: adapters, SHA256SUMS, VERSION and CHANGELOG checks.

`--check` must pass on a clean tree and fail on real drift, and `--release-tag` must accept only
a tag that names VERSION with a dated CHANGELOG entry. Each CLI case copies the skill into a temp
dir and runs build.py there, so the real files are never touched. Pure functions are tested on
build.py imported under a private name. Standard library only:
python -m unittest discover -s tests -v
"""

import hashlib
import importlib.util
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODEX = "adapters/AGENTS.md"
CURSOR = "adapters/cursor-dependency-first-architect.mdc"
CODEX_LAZY = "adapters/codex/dependency-first-architect.md"
CODEX_STUB = "adapters/codex/AGENTS-snippet.md"
FILES = [
    "build.py",
    "SKILL.md",
    CODEX,
    CURSOR,
    CODEX_LAZY,
    CODEX_STUB,
    "VERSION",
    "CHANGELOG.md",
    "SHA256SUMS",
    *sorted(f"reference/{p.name}" for p in (ROOT / "reference").glob("*.md")),
]
GENERATED = [CODEX, CURSOR, CODEX_LAZY, CODEX_STUB, "SHA256SUMS"]

# Hard-coded rather than imported: installed Codex blocks are found by these exact lines, so a
# change to them in build.py must fail here.
BEGIN_LINE = "BEGIN dependency-first-architect — GENERATED FILE, DO NOT HAND-EDIT."
END_MARKER = "<!-- END dependency-first-architect -->"
NEW_SENTINELS = ("Validation gates", "Methodology exceptions")
CC_OPEN, CC_CLOSE = "<!-- claude-code-only -->", "<!-- /claude-code-only -->"


def load_build():
    """build.py as a module under a private name, without writing __pycache__ into the repo."""
    spec = importlib.util.spec_from_file_location("dfa_build_under_test", ROOT / "build.py")
    module = importlib.util.module_from_spec(spec)
    saved, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = saved
    return module


build = load_build()


def to_crlf(path):
    path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))


def to_lf(path):
    path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n"))


class TempTree(unittest.TestCase):
    """A temp copy of everything build.py reads and writes; build.py runs inside it."""

    def setUp(self):
        self.tmp = self.copy_tree()

    def copy_tree(self, files=FILES):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        for rel in files:
            dest = tmp / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / rel, dest)
        return tmp

    def run_build(self, *args, root=None):
        return subprocess.run(
            [sys.executable, str((root or self.tmp) / "build.py"), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )

    def edit(self, rel, old, new):
        path = self.tmp / rel
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_bytes(text.replace(old, new).encode("utf-8"))

    def read(self, rel):
        return (self.tmp / rel).read_bytes()

    def write(self, rel, text):
        (self.tmp / rel).write_bytes(text.encode("utf-8"))

    def generated(self, root=None):
        return {rel: ((root or self.tmp) / rel).read_bytes() for rel in GENERATED}

    def assertPasses(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def assertFails(self, result, *needles):
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1, output)
        for needle in needles:
            self.assertIn(needle, output)


class CheckModeTest(TempTree):
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
        for path in (self.tmp / "adapters").rglob("*"):
            if path.is_file():
                to_crlf(path)
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
        for path in (self.tmp / "adapters").rglob("*"):
            if path.is_file():
                self.assertNotIn(b"\r\n", path.read_bytes(), path.name)
        self.assertEqual(self.run_build("--check").returncode, 0)

    def test_missing_sentinel_fails_without_writing(self):
        self.edit("SKILL.md", "Tradeoff gates", "Decision log")
        before = (self.tmp / "adapters" / "AGENTS.md").read_bytes()
        self.assertEqual(self.run_build("--check").returncode, 1)
        self.assertEqual(self.run_build().returncode, 1)
        self.assertEqual((self.tmp / "adapters" / "AGENTS.md").read_bytes(), before)


class VersionRulesTest(unittest.TestCase):
    def test_semantic_versions_are_accepted(self):
        for text, version in [
            ("2.0.0\n", "2.0.0"), ("2.0.0", "2.0.0"), ("2.0.0\r\n", "2.0.0"),
            ("0.0.0\n", "0.0.0"), ("10.20.30\n", "10.20.30"), ("1.0.0-rc.1\n", "1.0.0-rc.1"),
            ("1.0.0-alpha-1.0a\n", "1.0.0-alpha-1.0a"), ("1.0.0+build.5\n", "1.0.0+build.5"),
            ("1.0.0-0.3.7+exp.sha.5114f85\n", "1.0.0-0.3.7+exp.sha.5114f85"),
        ]:
            with self.subTest(text=text):
                self.assertEqual(build.parse_version(text), version)

    def test_bad_versions_are_rejected(self):
        for text in [
            "", "\n", "2\n", "2.0\n", "v2.0.0\n", "2.0.0.0\n", "02.0.0\n", "2.00.0\n", "2.0.0-\n",
            "2.0.0+\n", "2.0.0-01\n", "2.0.0-rc..1\n", "2.0.0-rc_1\n", " 2.0.0\n", "2.0.0 \n",
            "2.0.0\n\n", "2.0.0\n2.0.1\n", "2.0.0 # current\n",
            "\ufeff2.0.0\n",  # a byte-order mark
            "\uff12.0.0\n", "\u0662.0.0\n",  # non-ASCII digits, which `\d` would accept
        ]:
            with self.subTest(text=text):
                with self.assertRaises(ValueError) as caught:
                    build.parse_version(text)
                self.assertIn("semantic version", str(caught.exception))


class VersionFileTest(TempTree):
    def test_bad_version_fails_both_modes_and_writes_nothing(self):
        before = self.generated()
        self.write("VERSION", "2.0\n")
        for args in ((), ("--check",)):
            with self.subTest(args=args):
                self.assertFails(self.run_build(*args), "VERSION must be one line holding a "
                                 "semantic version such as 2.0.0, not '2.0'")
        self.assertEqual(self.generated(), before)

    def test_missing_version_fails(self):
        (self.tmp / "VERSION").unlink()
        self.assertFails(self.run_build(), "VERSION is missing")
        self.assertFails(self.run_build("--check"), "VERSION is missing")


class BannerTest(TempTree):
    def test_banner_names_version_and_skill_hash(self):
        self.write("VERSION", "3.1.4-rc.1\n")
        self.assertPasses(self.run_build())
        digest = hashlib.sha256(self.read("SKILL.md")).hexdigest()
        version_line = f"Version 3.1.4-rc.1 · built from SKILL.md sha256 {digest[:12]}"
        reference_line = ("Reference files (reference/*.md) are not bundled in this adapter; "
                          "see the repository.")
        for rel, indent in ((CODEX, ""), (CURSOR, " ")):
            with self.subTest(adapter=rel):
                lines = self.read(rel).decode("utf-8").split("\n")
                begin = lines.index(indent + BEGIN_LINE)
                self.assertEqual(lines[begin + 1], indent + version_line)
                self.assertEqual(lines[begin + 2], indent + reference_line)
        # The banner prefix is the start of SKILL.md's line in SHA256SUMS.
        self.assertIn(f"{digest}  SKILL.md\n", self.read("SHA256SUMS").decode("utf-8"))

    def test_begin_and_end_markers_unchanged(self):
        self.assertPasses(self.run_build())
        codex = self.read(CODEX).decode("utf-8")
        cursor = self.read(CURSOR).decode("utf-8")
        self.assertTrue(codex.startswith(f"<!--\n{BEGIN_LINE}\n"), codex[:200])
        self.assertTrue(cursor.startswith("---\ndescription: "), cursor[:200])
        self.assertIn(f"\nalwaysApply: false\n---\n\n<!--\n {BEGIN_LINE}\n", cursor)
        for name, text in (("codex", codex), ("cursor", cursor)):
            with self.subTest(adapter=name):
                self.assertTrue(text.endswith(f"\n\n{END_MARKER}\n"), text[-200:])
                self.assertEqual(text.count("BEGIN dependency-first-architect"), 1)
                self.assertEqual(text.count("END dependency-first-architect"), 1)


class ChecksumsTest(TempTree):
    def test_format_is_sha256sum_compatible(self):
        for rel in FILES:  # what a fresh checkout has (.gitattributes: eol=lf)
            to_lf(self.tmp / rel)
        self.assertPasses(self.run_build())
        data = self.read("SHA256SUMS")
        self.assertNotIn(b"\r", data)
        self.assertTrue(data.endswith(b"\n"))
        paths = []
        for line in data.decode("utf-8").split("\n")[:-1]:
            match = re.fullmatch(r"([0-9a-f]{64})  ([^\s\\]+)", line)
            self.assertIsNotNone(match, f"not `<sha256>  <posix path>`: {line!r}")
            digest, rel = match.groups()
            self.assertFalse(rel.startswith(("/", "./")) or ":" in rel, rel)
            # What `sha256sum -c` does: hash the bytes on disk.
            self.assertEqual(hashlib.sha256((self.tmp / rel).read_bytes()).hexdigest(), digest, rel)
            paths.append(rel)
        self.assertEqual(paths, sorted(paths))
        expected = {"VERSION", "SKILL.md", CODEX, CURSOR, CODEX_LAZY, CODEX_STUB}
        expected |= {f"reference/{p.name}" for p in (self.tmp / "reference").iterdir()
                     if p.suffix == ".md"}
        self.assertEqual(len(paths), len(expected))
        self.assertEqual(set(paths), expected)

    @unittest.skipUnless(shutil.which("sha256sum"), "sha256sum is not installed")
    def test_sha256sum_accepts_it(self):
        for rel in FILES:
            to_lf(self.tmp / rel)
        self.assertPasses(self.run_build())
        result = subprocess.run([shutil.which("sha256sum"), "-c", "SHA256SUMS"], cwd=self.tmp,
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = self.read("SHA256SUMS").decode("utf-8").splitlines()
        self.assertEqual(result.stdout.count(": OK"), len(lines), result.stdout)

    def test_tampered_entry_fails(self):
        text = self.read("SHA256SUMS").decode("utf-8")
        line = next(line for line in text.splitlines() if line.endswith("  SKILL.md"))
        self.write("SHA256SUMS", text.replace(line, ("1" if line[0] == "0" else "0") + line[1:]))
        result = self.run_build("--check")
        self.assertFails(result, "STALE: SHA256SUMS entry for SKILL.md is out of date",
                         "ERROR: SHA256SUMS is out of sync")
        self.assertNotIn("adapter(s) out of sync", result.stderr)

    def test_missing_file_fails_and_build_restores_it(self):
        (self.tmp / "SHA256SUMS").unlink()
        self.assertFails(self.run_build("--check"), "STALE: SHA256SUMS is missing")
        self.assertFalse((self.tmp / "SHA256SUMS").exists())  # --check wrote nothing
        self.assertPasses(self.run_build())
        self.assertPasses(self.run_build("--check"))

    def test_edited_bundled_reference_drifts_the_lazy_codex_adapter(self):
        self.edit("reference/plan-template.md", "# BUILD PLAN", "# BUILD PLAN (edited)")
        result = self.run_build("--check")
        self.assertFails(result, "1 adapter(s) out of sync", f"+++ {CODEX_LAZY}")
        self.assertPasses(self.run_build())
        self.assertIn(b"# BUILD PLAN (edited)", self.read(CODEX_LAZY))

    def test_edited_reference_file_fails(self):
        path = self.tmp / "reference" / "layer-map.md"  # bundled in no adapter
        path.write_bytes(path.read_bytes() + b"\nAn edit nobody rebuilt.\n")
        result = self.run_build("--check")
        self.assertFails(result, f"STALE: SHA256SUMS entry for reference/{path.name} is out of date")
        self.assertNotIn("adapter(s) out of sync", result.stderr)  # not part of the adapters
        self.assertPasses(self.run_build())
        self.assertPasses(self.run_build("--check"))

    def test_unlisted_and_removed_reference_files_fail(self):
        (self.tmp / "reference" / "zz-new.md").write_bytes(b"# New\n")
        self.assertFails(self.run_build("--check"),
                         "STALE: SHA256SUMS has no entry for reference/zz-new.md")
        (self.tmp / "reference" / "zz-new.md").unlink()
        removed = self.tmp / "reference" / "layer-map.md"  # one the adapters do not bundle
        removed.unlink()
        self.assertFails(self.run_build("--check"),
                         f"STALE: SHA256SUMS lists reference/{removed.name}, which is not a file")

    def test_malformed_and_reordered_lines_fail(self):
        original = self.read("SHA256SUMS").decode("utf-8")
        self.write("SHA256SUMS", original.replace("  VERSION\n", " VERSION\n"))
        self.assertFails(self.run_build("--check"), "is not `<sha256>  <path>`",
                         "STALE: SHA256SUMS has no entry for VERSION")
        self.write("SHA256SUMS", "".join(reversed(original.splitlines(keepends=True))))
        self.assertFails(self.run_build("--check"), "not the generated layout")

    def test_check_does_not_rewrite_it(self):
        self.write("SHA256SUMS", "stale\n")
        self.assertFails(self.run_build("--check"))
        self.assertEqual(self.read("SHA256SUMS"), b"stale\n")


class ChangelogRulesTest(unittest.TestCase):
    def test_entry_for_version_is_found(self):
        for text in ("## [2.0.0] — Unreleased\n", "# Changelog\n\n## [2.0.0] - 2026-10-02\n",
                     "## [2.0.0]\n", "## [2.0.0] — Unreleased\r\n\r\n## [1.0.0] — 2026-10-02\r\n"):
            with self.subTest(text=text):
                self.assertEqual(build.changelog_problems(text, "2.0.0"), [])

    def test_missing_entry_is_reported(self):
        for text in ("", "# Changelog\n", "### [2.0.0] — Unreleased\n", "## 2.0.0 — Unreleased\n",
                     "## [2.0.0-rc.1] — Unreleased\n", "## [2.0.1] — 2026-10-02\n",
                     "## [v2.0.0] — 2026-10-02\n", "See ## [2.0.0] below.\n"):
            with self.subTest(text=text):
                problems = build.changelog_problems(text, "2.0.0")
                self.assertEqual(len(problems), 1)
                self.assertIn("no `## [2.0.0]` heading", problems[0])
        self.assertIn("CHANGELOG.md is missing", build.changelog_problems(None, "2.0.0")[0])

    def test_duplicate_entry_is_reported(self):
        problems = build.changelog_problems("## [2.0.0] — Unreleased\n## [2.0.0] — 2026-10-02\n",
                                            "2.0.0")
        self.assertEqual(problems, ["CHANGELOG.md has 2 `## [2.0.0]` headings; keep one"])

    def test_release_needs_matching_tag_and_date(self):
        for separator in ("—", "–", "-"):
            with self.subTest(separator=separator):
                text = f"## [2.0.0] {separator} 2026-10-02\n"
                self.assertEqual(build.changelog_problems(text, "2.0.0", "v2.0.0"), [])
        dated = "## [2.0.0] — 2026-10-02\n"
        for tag in ("2.0.0", "v2.0.1", "V2.0.0", "v2.0.0-rc.1", "refs/tags/v2.0.0", ""):
            with self.subTest(tag=tag):
                problems = build.changelog_problems(dated, "2.0.0", tag)
                self.assertEqual(len(problems), 1)
                self.assertIn("does not match VERSION 2.0.0", problems[0])
        for label in ("Unreleased", "", "2026-13-01", "2026-02-30", "Oct 2, 2026",
                      "2026-10-02 [YANKED]"):
            with self.subTest(label=label):
                problems = build.changelog_problems(f"## [2.0.0] — {label}\n", "2.0.0", "v2.0.0")
                self.assertEqual(len(problems), 1)
                self.assertIn("has no release date", problems[0])
        self.assertEqual(len(build.changelog_problems(None, "2.0.0", "v2.0.1")), 2)


class ChangelogFileTest(TempTree):
    def test_check_requires_an_entry_for_version(self):
        version = self.read("VERSION").decode("utf-8").strip()
        self.write("CHANGELOG.md", "# Changelog\n\n## [0.0.1] — 2020-01-01\n\n- Old.\n")
        self.assertFails(self.run_build("--check"), f"no `## [{version}]` heading")
        (self.tmp / "CHANGELOG.md").unlink()
        self.assertFails(self.run_build("--check"), "CHANGELOG.md is missing")

    def test_version_bump_needs_an_entry_before_check_passes(self):
        self.write("VERSION", "9.9.9\n")
        result = self.run_build()  # generating still works, with a warning
        self.assertPasses(result)
        self.assertIn("WARNING: CHANGELOG.md has no `## [9.9.9]` heading", result.stderr)
        self.assertIn("Version 9.9.9 · built from SKILL.md", self.read(CODEX).decode("utf-8"))
        self.assertFails(self.run_build("--check"), "no `## [9.9.9]` heading")
        changelog = self.read("CHANGELOG.md").decode("utf-8")
        self.write("CHANGELOG.md", changelog + "\n## [9.9.9] — Unreleased\n\n- A change.\n")
        self.assertPasses(self.run_build("--check"))


class ReleaseTagTest(TempTree):
    def setUp(self):
        super().setUp()
        self.write("VERSION", "3.1.4\n")
        self.write_changelog("2026-11-05")
        self.assertPasses(self.run_build())

    def write_changelog(self, label):
        self.write("CHANGELOG.md", f"# Changelog\n\n## [3.1.4] — {label}\n\n### Changed\n\n- A.\n\n"
                                   "## [1.0.0] — 2026-10-02\n\nRetroactively designated.\n")

    def test_matching_tag_with_dated_entry_passes(self):
        result = self.run_build("--release-tag", "v3.1.4")
        self.assertPasses(result)
        self.assertIn("Release tag v3.1.4 matches VERSION and a dated CHANGELOG entry: OK",
                      result.stdout)

    def test_mismatched_tag_fails(self):
        for tag in ("v3.1.5", "3.1.4", "v3.1.4-rc.1", "refs/tags/v3.1.4", ""):
            with self.subTest(tag=tag):
                self.assertFails(self.run_build("--release-tag", tag),
                                 f"release tag {tag!r} does not match VERSION 3.1.4")

    def test_unreleased_entry_fails(self):
        self.write_changelog("Unreleased")
        self.assertPasses(self.run_build("--check"))  # fine while the version is in progress
        self.assertFails(self.run_build("--release-tag", "v3.1.4"),
                         "the CHANGELOG.md entry for 3.1.4 has no release date (found 'Unreleased')")

    def test_release_tag_also_runs_check_and_writes_nothing(self):
        self.edit(CODEX, "## Procedure", "## Procedure\n\nhand edit")
        before = self.generated()
        self.assertFails(self.run_build("--release-tag", "v3.1.4"),
                         "1 adapter(s) out of sync with SKILL.md")
        self.assertEqual(self.generated(), before)


class DeterminismTest(TempTree):
    def test_two_builds_are_byte_identical(self):
        self.assertPasses(self.run_build())
        first = self.generated()
        self.assertPasses(self.run_build())
        self.assertEqual(self.generated(), first)
        # From scratch at another path: nothing depends on the location or the clock.
        other = self.copy_tree([rel for rel in FILES if rel not in GENERATED])
        self.assertFalse((other / "adapters").exists())
        self.assertPasses(self.run_build(root=other))
        self.assertEqual(self.generated(other), first)

    def test_crlf_sources_build_the_same_bytes(self):
        self.assertPasses(self.run_build())
        lf = self.generated()
        for rel in FILES:
            if rel != "build.py" and rel not in GENERATED:
                to_crlf(self.tmp / rel)
        self.assertPasses(self.run_build("--check"))  # a CRLF working copy is not drift
        self.assertPasses(self.run_build())
        self.assertEqual(self.generated(), lf)

    def test_full_crlf_checkout_passes_check(self):
        self.assertPasses(self.run_build())
        for rel in FILES:
            if rel != "build.py":
                to_crlf(self.tmp / rel)
        self.assertPasses(self.run_build("--check"))


class SentinelTest(TempTree):
    def test_sentinel_names(self):
        self.assertEqual(build.SENTINELS, ("Tradeoff gates", *NEW_SENTINELS))
        self.assertEqual(build.SENTINEL, build.SENTINELS[0])

    def test_each_new_sentinel_removal_fails_without_writing(self):
        original = self.read("SKILL.md")
        before = self.generated()
        for sentinel in NEW_SENTINELS:
            with self.subTest(sentinel=sentinel):
                (self.tmp / "SKILL.md").write_bytes(original)
                self.edit("SKILL.md", sentinel, "Renamed section")
                for args in (("--check",), ()):
                    self.assertFails(self.run_build(*args),
                                     f"ERROR: sentinel '{sentinel}' missing from AGENTS.md",
                                     f"ERROR: sentinel '{sentinel}' missing from "
                                     "cursor-dependency-first-architect.mdc")
                self.assertEqual(self.generated(), before)


class ClaudeCodeOnlyRulesTest(unittest.TestCase):
    def strip(self, text):
        return build.strip_claude_code_only(text)

    def test_text_without_markers_is_unchanged(self):
        text = "# Title\n\nPara.\n\n\n## Kept as is\n"
        self.assertEqual(self.strip(text), text)

    def test_span_inside_a_line_is_removed_with_the_space_before_it(self):
        self.assertEqual(self.strip(f"A sentence.{CC_OPEN} Load `reference/x.md`.{CC_CLOSE}\nNext.\n"),
                         "A sentence.\nNext.\n")
        self.assertEqual(self.strip(f"A sentence. {CC_OPEN}Load it.{CC_CLOSE} More.\n"),
                         "A sentence. More.\n")
        self.assertEqual(self.strip(f"Text{CC_OPEN} one\ntwo{CC_CLOSE} tail\n"), "Text tail\n")

    def test_block_span_leaves_no_doubled_or_leading_blank_line(self):
        for text, expected in (
            (f"Para A.\n\n{CC_OPEN}\n## Ref\ntext\n{CC_CLOSE}\n\nPara B.\n", "Para A.\n\nPara B.\n"),
            (f"Para A.\n{CC_OPEN}\n\n## Ref\n{CC_CLOSE}\n", "Para A.\n"),
            (f"{CC_OPEN}\nhidden\n{CC_CLOSE}\n\nPara B.\n", "Para B.\n"),
            (f"A.\n{CC_OPEN}\nx\n{CC_CLOSE}\nB.{CC_OPEN} y{CC_CLOSE}\n", "A.\nB.\n"),
        ):
            with self.subTest(text=text):
                self.assertEqual(self.strip(text), expected)

    def test_unbalanced_or_nested_markers_are_rejected(self):
        for text in (f"{CC_OPEN} x", f"x {CC_CLOSE}", f"{CC_OPEN}{CC_OPEN}x{CC_CLOSE}{CC_CLOSE}",
                     f"{CC_CLOSE} x {CC_OPEN}"):
            with self.subTest(text=text):
                with self.assertRaisesRegex(ValueError, "unbalanced or nested"):
                    self.strip(text)


class ClaudeCodeOnlyBuildTest(TempTree):
    SKILL = (
        "---\nname: synthetic\ndescription: >-\n  Use when testing the build.\n---\n\n"
        "# Synthetic\n\n"
        f"## Tradeoff gates\nDecide early.{CC_OPEN} Load `reference/only-claude.md` now.{CC_CLOSE}\n\n"
        "## Validation gates\nFive fields.\n\n"
        f"## Methodology exceptions\nDeclare them.\n{CC_OPEN}\n\n## Reference files\n"
        f"- `reference/only-claude.md`\n{CC_CLOSE}\n"
    )

    def test_adapters_omit_claude_code_only_spans(self):
        self.write("SKILL.md", self.SKILL)
        self.assertPasses(self.run_build())
        body = ("# Synthetic\n\n## Tradeoff gates\nDecide early.\n\n## Validation gates\n"
                "Five fields.\n\n## Methodology exceptions\nDeclare them.\n\n" + END_MARKER + "\n")
        codex = self.read(CODEX).decode("utf-8")
        self.assertTrue(codex.endswith("**When to use:** Use when testing the build.\n\n" + body),
                        codex)
        self.assertTrue(self.read(CURSOR).decode("utf-8").endswith("-->\n\n" + body))
        self.assertPasses(self.run_build("--check"))

    def test_real_adapters_carry_no_markers(self):
        self.assertPasses(self.run_build())
        for rel in (CODEX, CURSOR):
            text = self.read(rel).decode("utf-8")
            self.assertNotIn(CC_OPEN, text, rel)
            self.assertNotIn(CC_CLOSE, text, rel)

    def test_unbalanced_markers_fail_without_writing(self):
        before = self.generated()
        skill = self.read("SKILL.md").decode("utf-8")
        self.write("SKILL.md", skill + f"\n{CC_OPEN}\nNever closed.\n")
        for args in ((), ("--check",)):
            with self.subTest(args=args):
                self.assertFails(self.run_build(*args), "unbalanced or nested")
        self.assertEqual(self.generated(), before)


class FrontmatterTest(unittest.TestCase):
    def parse(self, front):
        return build.parse_skill("---\n" + front + "---\n\n# Body\n\nText.\n")

    def test_crlf_skill_parses_like_lf(self):
        lf = (ROOT / "SKILL.md").read_bytes().decode("utf-8").replace("\r\n", "\n")
        crlf = build.parse_skill(lf.replace("\n", "\r\n"))
        self.assertEqual(crlf, build.parse_skill(lf))
        self.assertNotIn("\r", "".join(crlf))
        text = "---\r\nname: n\r\ndescription: >-\r\n  folded\r\n  text\r\n---\r\n\r\n# Body\r\n"
        self.assertEqual(build.parse_skill(text), ("n", "folded text", "# Body\n"))

    def test_missing_name(self):
        for front in ("description: Plans builds.\n", "name:\ndescription: d\n",
                      "description: d\n  name: indented, so part of the description\n"):
            with self.subTest(front=front):
                with self.assertRaisesRegex(ValueError, "frontmatter missing `name`"):
                    self.parse(front)

    def test_missing_description(self):
        for front in ("name: n\n", "name: n\ndescription:\n", "name: n\ndescription: >-\n",
                      "name: n\ndescription: >-\n\nlicense: MIT\n", "name: n\n# description: d\n"):
            with self.subTest(front=front):
                with self.assertRaisesRegex(ValueError, "frontmatter missing `description`"):
                    self.parse(front)

    def test_folded_description_is_joined_into_one_line(self):
        name, description, body = self.parse(
            "name: my-skill\n"
            "description: >-\n"
            "  Use when a user asks\n"
            "  to plan a build.\n"
            "\n"
            "    Leads with a skeleton.\n"
            "license: MIT\n"
        )
        self.assertEqual(name, "my-skill")
        self.assertEqual(description, "Use when a user asks to plan a build. Leads with a skeleton.")
        self.assertEqual(body, "# Body\n\nText.\n")

    def test_other_multiline_forms_are_joined(self):
        for front in ("description: |\n  one\n  two\n", "description: >+\n  one\n  two\n",
                      "description: one\n  two\n", "description:\n  one\n  two\n"):
            with self.subTest(front=front):
                self.assertEqual(self.parse("name: n\n" + front)[1], "one two")

    def test_dashes_inside_a_value_do_not_close_the_frontmatter(self):
        self.assertEqual(self.parse("name: n\ndescription: before --- after\n")[1],
                         "before --- after")

    def test_not_frontmatter(self):
        for text, message in (("# No frontmatter\n", "must start with YAML frontmatter"),
                              ("", "must start with YAML frontmatter"),
                              ("---\nname: n\ndescription: d\n", "not closed by a --- line")):
            with self.subTest(text=text):
                with self.assertRaisesRegex(ValueError, message):
                    build.parse_skill(text)

    def test_cursor_description_is_one_yaml_line(self):
        lines = [BEGIN_LINE]
        plain = build.build_cursor("n", "Plan builds — any size.", "# Body\n", lines)
        self.assertEqual(plain.split("\n")[1], "description: Plan builds — any size.")
        # ": " and " #" would change what YAML reads, so such a description is quoted.
        quoted = build.build_cursor("n", 'Use when: planning #1 "big" builds', "# Body\n", lines)
        self.assertEqual(quoted.split("\n")[1],
                         'description: "Use when: planning #1 \\"big\\" builds"')


class LazyCodexTest(unittest.TestCase):
    """The lazy Codex install: a small always-loaded block plus the whole skill in one file."""

    @classmethod
    def setUpClass(cls):
        outputs = build.render(build.read_version())
        cls.stub = outputs[build.CODEX_STUB]
        cls.lazy = outputs[build.CODEX_LAZY]
        cls.full = outputs[build.CODEX]

    def test_stub_is_small_and_points_at_the_file(self):
        self.assertLess(len(self.stub.encode("utf-8")), len(self.full.encode("utf-8")) / 8)
        self.assertIn(f"read `{build.CODEX_LAZY_INSTALLED}` in full", self.stub)
        self.assertIn(BEGIN_LINE, self.stub)
        self.assertTrue(self.stub.endswith(END_MARKER + "\n"))
        self.assertNotIn("Tradeoff gates", self.stub)  # the method lives in the lazy file

    def test_lazy_file_carries_the_method_and_its_references(self):
        for sentinel in build.SENTINELS:
            self.assertIn(sentinel, self.lazy)
        for letter, rel in zip("ABC", build.CODEX_LAZY_REFERENCES):
            self.assertIn(f"## Appendix {letter}: `{rel}`", self.lazy)
            first = next(line for line in (ROOT / rel).read_text(encoding="utf-8").splitlines()
                         if line.startswith("# "))
            self.assertIn("##" + first, self.lazy)  # demoted under its appendix heading
        self.assertNotIn(CC_OPEN, self.lazy)
        self.assertNotIn("reference/evals.md", self.lazy)
        self.assertTrue(self.lazy.endswith(END_MARKER + "\n"))

    def test_heading_demotion_skips_code_fences(self):
        text = "# Title\n\n```\n# not a heading\n```\n\n## Section"
        self.assertEqual(build.demote_headings(text),
                         "### Title\n\n```\n# not a heading\n```\n\n#### Section")


if __name__ == "__main__":
    unittest.main()
