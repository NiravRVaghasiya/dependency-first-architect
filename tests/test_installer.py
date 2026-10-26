"""Self-tests for the installer: installer/install.py, as package_skill.py renders it into a release.

Offline: every case installs into a temporary project and a temporary home folder, so neither the
repository nor your own ~/.claude is touched. The rendered installer is imported (its main() takes
the folders, the question and whether stdin is a terminal as arguments) and, for the end-to-end
cases, run as a script.
"""

import contextlib
import hashlib
import importlib.util
import io
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import build  # noqa: E402
import package_skill as pkg  # noqa: E402

NAME = "dependency-first-architect"
OUTPUTS, PROBLEMS = pkg.plan(ROOT)
VERSION = build.read_version()
INSTALLER_NAME = pkg.installer_name(VERSION)
INSTALLER = OUTPUTS.get(f"release/{INSTALLER_NAME}", b"")
_TMP = tempfile.TemporaryDirectory()
INSTALLER_PATH = Path(_TMP.name) / INSTALLER_NAME
INSTALLER_PATH.write_bytes(INSTALLER)


def load(path, name="dfa_installer"):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


inst = load(INSTALLER_PATH)
ASSET = {name: OUTPUTS[f"release/{name}"] for name in (
    "cursor-dependency-first-architect.mdc", "codex-lazy-dependency-first-architect.md",
    "codex-lazy-AGENTS-snippet.md", "codex-AGENTS.md")}
PACKAGE = {rel: OUTPUTS[f"{NAME}/{rel}"] for rel in pkg.PACKAGE_FILES}
STUB = ASSET["codex-lazy-AGENTS-snippet.md"]
FULL_BLOCK = ASSET["codex-AGENTS.md"]  # the standard Codex block older instructions appended


def tearDownModule():
    _TMP.cleanup()


def knowing(*packages):
    """Patch the installer to also know these {path: bytes} as files of an earlier release."""
    extra = "".join(f"{hashlib.sha256(data.replace(b'\r\n', b'\n')).hexdigest()}  {NAME}/{rel}\n"
                    for package in packages for rel, data in package.items())
    return mock.patch.object(inst, "RELEASED", inst.RELEASED + extra)


def make_read_only(case, path):
    """Make `path` read-only for one test (and writable again afterwards, so it can be deleted)."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        case.skipTest("root can write read-only files")
    os.chmod(path, stat.S_IREAD)
    case.addCleanup(lambda: path.exists() and os.chmod(path, stat.S_IREAD | stat.S_IWRITE))


def snapshot(folder, skip=(".git",)):
    """{POSIX path: bytes} of every file under `folder`, and the folders, as path + '/'."""
    found = {}
    for parent, dirs, names in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in skip]
        for d in dirs:
            found[(Path(parent) / d).relative_to(folder).as_posix() + "/"] = None
        for n in names:
            path = Path(parent) / n
            found[path.relative_to(folder).as_posix()] = path.read_bytes()
    return found


class InstallerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.project = self.home / "work" / "app"
        (self.project / ".git").mkdir(parents=True)

    def run_installer(self, *argv, answers=None, interactive=None, cwd=None, module=inst):
        """(exit code, output) of the installer's main() in the test project."""
        answers = list(answers or [])
        asked = []

        def ask(prompt):
            asked.append(prompt)
            if not answers:
                raise EOFError
            return answers.pop(0)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                code = module.main(list(argv), cwd=cwd or self.project, home=self.home, ask=ask,
                                   interactive=bool(answers) if interactive is None
                                   else interactive)
            except SystemExit as exc:
                code = exc.code
        self.asked = asked
        return code, out.getvalue()

    def assertRuns(self, *argv, **kwargs):
        code, out = self.run_installer(*argv, **kwargs)
        self.assertEqual(code, 0, out)
        return out

    def assertStops(self, *argv, expect, code=1, **kwargs):
        before = snapshot(self.home)
        got, out = self.run_installer(*argv, **kwargs)
        self.assertEqual(got, code, out)
        self.assertIn(expect, out)
        self.assertEqual(snapshot(self.home), before, "a refused run changed files")
        return out

    def write(self, rel, data, base=None):
        path = (base or self.project) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return path

    def read(self, rel, base=None):
        return ((base or self.project) / rel).read_bytes()

    def skill_files(self, base=None):
        folder = (base or self.project) / ".claude" / "skills" / NAME
        return {k: v for k, v in snapshot(folder).items() if not k.endswith("/")}


class GeneratedInstallerTest(unittest.TestCase):
    """What package_skill.py publishes, and the static checks it runs on it."""

    def test_built_without_problems_as_a_release_asset(self):
        self.assertEqual(PROBLEMS, [])
        self.assertTrue(INSTALLER.startswith(b"#!/usr/bin/env python3\n"))
        entries, malformed = build.parse_sums(OUTPUTS["release/SHA256SUMS"].decode("utf-8"))
        self.assertEqual(malformed, [])
        self.assertEqual(entries[INSTALLER_NAME], hashlib.sha256(INSTALLER).hexdigest())

    def test_carries_the_release_sums_and_exactly_the_files_it_installs(self):
        sums = OUTPUTS["release/SHA256SUMS"].decode("utf-8")
        own = f"{hashlib.sha256(INSTALLER).hexdigest()}  {INSTALLER_NAME}\n"
        self.assertEqual(inst.MANIFEST, sums.replace(own, ""))
        self.assertEqual(inst.VERSION, VERSION)
        self.assertEqual(sorted(inst.PAYLOAD), sorted(pkg.INSTALLER_PAYLOAD))
        for name, text in inst.PAYLOAD.items():
            source = OUTPUTS.get(f"release/{name}", OUTPUTS.get(name))
            self.assertEqual(text.encode("utf-8"), source, name)
        # Nothing an agent does not read: no zip, no standard Codex block, no SHA256SUMS.
        self.assertNotIn("codex-AGENTS.md", inst.PAYLOAD)

    def test_constants_match_build_and_package_skill(self):
        self.assertEqual(inst.BEGIN_LINE, build.BEGIN_LINE)
        self.assertEqual(inst.END_MARKER, build.END_MARKER)
        self.assertEqual(inst.PACKAGE_FILES, pkg.PACKAGE_FILES)
        self.assertEqual(inst.OS_METADATA, pkg.OS_METADATA)
        self.assertEqual(inst.PAYLOAD_NAMES, pkg.INSTALLER_PAYLOAD)
        self.assertEqual("/".join(inst.CODEX_FILE), build.CODEX_LAZY_INSTALLED)
        self.assertEqual(inst.CURSOR_ASSET, pkg.ADAPTER_ASSETS[build.CURSOR])
        self.assertEqual(inst.CODEX_FILE_ASSET, pkg.ADAPTER_ASSETS[build.CODEX_LAZY])
        self.assertEqual(inst.CODEX_BLOCK_ASSET, pkg.ADAPTER_ASSETS[build.CODEX_STUB])
        # Every adapter the installer manages opens with the line its block grammar expects.
        for name in (inst.CODEX_BLOCK_ASSET, inst.CODEX_FILE_ASSET):
            self.assertTrue(ASSET[name].startswith(f"<!--\n{build.BEGIN_LINE}\n".encode()), name)

    def test_rebuild_is_byte_identical(self):
        again, problems = pkg.plan(ROOT)
        self.assertEqual(problems, [])
        self.assertEqual(again[f"release/{INSTALLER_NAME}"], INSTALLER)

    def test_static_checks_pass_and_catch_what_they_are_for(self):
        text = INSTALLER.decode("ascii")
        payload = {n: OUTPUTS.get(f"release/{n}", OUTPUTS.get(n)) for n in pkg.INSTALLER_PAYLOAD}
        check = lambda t: pkg.installer_problems(  # noqa: E731
            t, VERSION, inst.MANIFEST, payload, inst.RELEASED)
        self.assertEqual(check(text), [])
        planted = {
            "import socket\n": "imports socket",
            "import urllib.request\n": "imports urllib.request",
            "from subprocess import run\n": "imports subprocess",
            "os.system('x')\n": "uses system",
            "from os import system as s\n": "uses system",
            "from os import environ\n": "uses environ",
            "os.posix_spawn('x', [], {})\n": "uses posix_spawn",
            "eval('1')\n": "uses eval",
            "x = vars()\n": "uses vars",
            "x = os.environ\n": "uses environ",
            "PAYLOAD['x'] = 'y'\n": "changes PAYLOAD",
            "PAYLOAD.update({})\n": "calls a method of PAYLOAD",
            "def f():\n    global MANIFEST\n    MANIFEST = ''\n": "rebinds",
            "del PAYLOAD['x']\n": "deletes from the release data",
            "_p = PAYLOAD\n": "reads PAYLOAD other than as an argument of Release()",
            "dict.__setitem__(PAYLOAD, 'a', 'b')\n": "reads PAYLOAD other than",
            "for PAYLOAD in []:\n    pass\n": "rebinds PAYLOAD",
            "(RELEASED := '')\n": "rebinds RELEASED",
            "import os as VERSION\n": "rebinds VERSION",
            "try:\n    pass\nexcept Exception as PAYLOAD:\n    pass\n": "rebinds PAYLOAD",
        }
        for line, expected in planted.items():
            with self.subTest(line=line):
                self.assertTrue(any(expected in p for p in check(text + line)), check(text + line))
        tampered = text.replace("\"name: dependency-first-architect\\n\"", "\"name: other\\n\"", 1)
        self.assertNotEqual(tampered, text)
        self.assertTrue(any("PAYLOAD must be assigned once" in p for p in check(tampered)))
        self.assertIn("must be ASCII", check(text + "# \u00e9\n")[0])
        self.assertIn("must be ASCII", check(text.replace("\n", "\r\n"))[0])

    def test_template_needs_its_placeholder_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "installer").mkdir()
            source = (ROOT / pkg.INSTALLER_SOURCE).read_text("ascii")
            for text, expected in ((source.replace(pkg.INSTALLER_PLACEHOLDER, ""), "exactly once"),
                                   (source + pkg.INSTALLER_PLACEHOLDER, "exactly once"),
                                   (source + "\u00e9", "must be ASCII")):
                (root / pkg.INSTALLER_SOURCE).write_bytes(text.encode("utf-8"))
                _, problems = pkg.make_installer(root, VERSION, inst.MANIFEST, {})
                self.assertIn(expected, problems[0])

    def test_the_template_itself_refuses_to_run(self):
        result = subprocess.run([sys.executable, "-I", str(ROOT / pkg.INSTALLER_SOURCE),
                                 "--agent", "cursor", "--project", _TMP.name],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("template, not a release", result.stdout)

    def test_a_damaged_installer_stops_before_writing(self):
        text = INSTALLER.decode("ascii").replace("\"# Dependency-First Architect\\n\"",
                                                 "\"# Dependency-First Architekt\\n\"", 1)
        self.assertNotEqual(text.encode("ascii"), INSTALLER)
        with tempfile.TemporaryDirectory() as tmp:
            damaged = Path(tmp) / "damaged.py"
            damaged.write_bytes(text.encode("ascii"))
            project = Path(tmp) / "p"
            (project / ".git").mkdir(parents=True)
            result = subprocess.run([sys.executable, "-I", str(damaged), "--agent",
                                     "claude-code", "--project", str(project)],
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("installer is damaged", result.stdout)
            self.assertEqual(sorted(p.name for p in project.iterdir()), [".git"])


class EndToEndTest(InstallerCase):
    """The release asset, run as a script the way the README runs it."""

    def script(self, *argv, cwd=None):
        env = {k: v for k, v in os.environ.items() if k not in ("HOME", "USERPROFILE")}
        env["HOME"] = env["USERPROFILE"] = str(self.home)
        return subprocess.run([sys.executable, "-I", str(INSTALLER_PATH), *argv],
                              cwd=cwd or self.project, env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=120)

    def test_install_rerun_verify_uninstall(self):
        (self.project / ".cursor").mkdir()
        first = self.script()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn("Cursor: installed .cursor/rules/dependency-first-architect.mdc", first.stdout)
        self.assertIn("Done. To try it:", first.stdout)
        self.assertEqual(self.read(".cursor/rules/dependency-first-architect.mdc"),
                         ASSET["cursor-dependency-first-architect.mdc"])
        again = self.script()
        self.assertEqual(again.returncode, 0)
        self.assertIn("nothing to change", again.stdout)
        self.assertEqual(self.script("--verify").returncode, 0)
        gone = self.script("--uninstall")
        self.assertEqual(gone.returncode, 0, gone.stdout)
        self.assertEqual(sorted(p.name for p in self.project.iterdir()), [".git"])

    def test_without_a_terminal_an_unclear_project_asks_for_agent(self):
        # stdin is the null device: Windows reports it as a terminal, so the installer must
        # notice that nobody can answer.
        result = self.script()
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("Could not tell which coding agent this project uses", result.stdout)
        self.assertIn("--agent claude-code, --agent cursor or --agent codex", result.stdout)
        self.assertEqual(sorted(p.name for p in self.project.iterdir()), [".git"])


class FreshInstallTest(InstallerCase):
    def test_claude_code_gets_exactly_the_skill_package(self):
        out = self.assertRuns("--agent", "claude-code")
        self.assertEqual(self.skill_files(), PACKAGE)
        self.assertIn("Claude Code: installed .claude/skills/dependency-first-architect/", out)
        self.assertIn(f"/{NAME}", out)  # how to call it
        self.assertEqual(set(snapshot(self.project)) - {".claude/", ".claude/skills/"},
                         {f".claude/skills/{NAME}/", f".claude/skills/{NAME}/reference/",
                          *(f".claude/skills/{NAME}/{rel}" for rel in PACKAGE)})

    def test_cursor_gets_one_complete_rule(self):
        self.assertRuns("--agent", "cursor")
        rule = self.read(".cursor/rules/dependency-first-architect.mdc")
        self.assertEqual(rule, ASSET["cursor-dependency-first-architect.mdc"])
        for heading in ("## Appendix A: `reference/plan-template.md`",
                        "## Appendix B: `reference/validation.md`",
                        "## Appendix C: `reference/ai-systems.md`"):
            self.assertIn(heading.encode(), rule)  # nothing of the skill left out
        self.assertEqual(set(snapshot(self.project)),
                         {".cursor/", ".cursor/rules/", ".cursor/rules/dependency-first-architect.mdc"})

    def test_codex_gets_the_block_and_the_file_it_points_at(self):
        self.assertRuns("--agent", "codex")
        self.assertEqual(self.read("AGENTS.md"), STUB)
        self.assertEqual(self.read(".codex/dependency-first-architect.md"),
                         ASSET["codex-lazy-dependency-first-architect.md"])
        self.assertIn(b"read `.codex/dependency-first-architect.md` in full", STUB)

    def test_all_three_at_once(self):
        out = self.assertRuns("--agent", "claude-code", "--agent", "cursor", "--agent", "codex")
        for agent in ("Claude Code", "Cursor", "Codex"):
            self.assertIn(f"{agent}: installed", out)

    def test_global_installs_for_claude_code_in_the_home_folder_only(self):
        out = self.assertRuns("--global")
        self.assertEqual(self.skill_files(self.home), PACKAGE)
        self.assertEqual(sorted(p.name for p in self.project.iterdir()), [".git"])
        self.assertIn("your home folder", out)
        self.assertNotIn("commit them", out)

    def test_dry_run_changes_nothing(self):
        (self.project / ".claude").mkdir()
        before = snapshot(self.home)
        out = self.assertRuns("--dry-run")
        self.assertIn("Claude Code: would install", out)
        self.assertIn("Dry run: nothing was changed.", out)
        self.assertEqual(snapshot(self.home), before)


class DetectionTest(InstallerCase):
    def test_one_clear_signal_is_used(self):
        for marker, agent in ((".claude/", "Claude Code"), ("CLAUDE.md", "Claude Code"),
                              (".cursor/", "Cursor"), ("AGENTS.md", "Codex")):
            with self.subTest(marker=marker):
                shutil.rmtree(self.project)
                (self.project / ".git").mkdir(parents=True)
                if marker.endswith("/"):
                    (self.project / marker).mkdir()
                else:
                    self.write(marker, "# notes\n")
                out = self.assertRuns()
                self.assertIn(f"{agent}: installed", out)
                self.assertEqual(out.count(": installed"), 1, out)
                self.assertEqual(self.asked, [])

    def test_no_signal_and_no_terminal_exits_2_and_writes_nothing(self):
        out = self.assertStops(expect="--agent claude-code, --agent cursor or --agent codex",
                               code=2, interactive=False)
        self.assertIn("Could not tell", out)

    def test_several_signals_ask_one_question(self):
        (self.project / ".claude").mkdir()
        self.write("AGENTS.md", "# notes\n")
        out = self.assertRuns(answers=["1 3"])
        self.assertEqual(len(self.asked), 1)
        self.assertIn("(found .claude/)", out)
        self.assertIn("(found AGENTS.md)", out)
        self.assertIn("Claude Code: installed", out)
        self.assertIn("Codex: installed", out)
        self.assertNotIn("Cursor: installed", out)

    def test_answers_by_name_and_repeated_invalid_answers(self):
        self.assertIn("Cursor: installed", self.assertRuns(answers=["cursor"]))
        shutil.rmtree(self.project / ".cursor")
        self.assertStops(answers=["9", "yes", "0"], expect="No agent chosen", code=2)
        # End of input at the first prompt: nobody to answer.
        self.assertStops(interactive=True, expect="Could not tell which coding agent", code=2)

    def test_options_must_be_spelled_out(self):
        for argv in (["--uninst"], ["--dry"], ["--glob"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit), \
                    contextlib.redirect_stderr(io.StringIO()):
                inst.parse_args(argv)

    def test_the_home_folder_through_a_link_is_still_the_home_folder(self):
        link = self.tmp / "linked-home"
        try:
            if sys.platform == "win32":
                import _winapi
                _winapi.CreateJunction(str(self.home), str(link))
            else:
                os.symlink(self.home, link, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"cannot link here: {exc}")
        self.write("AGENTS.md", "# Home notes\n", self.home)
        self.assertStops(cwd=link, expect="is your home folder, not a project", code=2)

    def test_a_subfolder_installs_at_the_repository_root(self):
        sub = self.project / "src" / "pkg"
        sub.mkdir(parents=True)
        (self.project / ".cursor").mkdir()
        self.assertRuns(cwd=sub)
        self.assertTrue((self.project / ".cursor/rules/dependency-first-architect.mdc").is_file())
        self.assertFalse((sub / ".cursor").exists())

    def test_the_home_folder_is_not_a_project(self):
        (self.home / ".claude").mkdir()
        self.assertStops(cwd=self.home, expect="is your home folder, not a project", code=2)
        self.assertStops("--project", str(self.home), "--agent", "cursor",
                         expect="not a project", code=2)

    def test_global_is_for_claude_code_only(self):
        self.assertStops("--global", "--agent", "cursor", expect="--global is for Claude Code",
                         code=2)

    def test_a_rerun_updates_what_is_installed_and_nothing_else(self):
        self.assertRuns("--agent", "cursor")
        (self.project / ".claude").mkdir()  # a new signal does not widen the install
        out = self.assertRuns()
        self.assertIn("Cursor: already", out)
        self.assertNotIn("Claude Code", out.split("Done.")[0])
        self.assertEqual(self.asked, [])


class IdempotenceTest(InstallerCase):
    def test_a_second_run_changes_no_byte_and_no_timestamp(self):
        self.write("AGENTS.md", "# Team rules\n\nUse tabs.\n")
        args = ("--agent", "claude-code", "--agent", "cursor", "--agent", "codex")
        self.assertRuns(*args)
        before = snapshot(self.project)
        stamps = {p: p.stat().st_mtime_ns for p in self.project.rglob("*") if p.is_file()}
        out = self.assertRuns(*args)
        self.assertEqual(out.count("nothing to change"), 3, out)
        self.assertEqual(snapshot(self.project), before)
        self.assertEqual({p: p.stat().st_mtime_ns for p in self.project.rglob("*")
                          if p.is_file()}, stamps)
        self.assertEqual(self.read("AGENTS.md").count(build.BEGIN_LINE.encode()), 1)


class AgentsMdTest(InstallerCase):
    """The rest of AGENTS.md is the user's: it survives install, update and uninstall."""

    def install_then_uninstall(self, original):
        self.write("AGENTS.md", original)
        self.assertRuns("--agent", "codex")
        merged = self.read("AGENTS.md")
        self.assertEqual(merged.count(build.BEGIN_LINE.encode()), 1)
        self.assertRuns("--uninstall")
        return merged, self.read("AGENTS.md") if (self.project / "AGENTS.md").exists() else None

    def test_user_text_is_kept_and_restored(self):
        original = b"# Team rules\n\n- Use tabs.\n- Run the tests.\n"
        merged, after = self.install_then_uninstall(original)
        self.assertEqual(merged, original + b"\n" + STUB)
        self.assertEqual(after, original)

    def test_crlf_bom_and_a_missing_final_newline(self):
        crlf = b"# Team rules\r\n\r\nUse tabs.\r\n"
        merged, after = self.install_then_uninstall(crlf)
        self.assertEqual(merged, crlf + b"\r\n" + STUB.replace(b"\n", b"\r\n"))
        self.assertEqual(after, crlf)
        bom = "\ufeff# Regeln\n\nTabs, nicht Leerzeichen.\n".encode("utf-8")
        merged, after = self.install_then_uninstall(bom)
        self.assertTrue(merged.startswith(bom))
        self.assertEqual(after, bom)
        merged, after = self.install_then_uninstall(b"# Team rules")
        self.assertEqual(after, b"# Team rules\n")  # the one byte a round trip adds

    def test_a_file_holding_only_the_block_is_removed_on_uninstall(self):
        merged, after = self.install_then_uninstall(b"")
        self.assertEqual(merged, STUB)
        self.assertIsNone(after)

    def test_duplicate_blocks_from_older_appends_collapse_into_one(self):
        # What `{ printf '\n'; cat codex-AGENTS.md; } >> AGENTS.md` left after two runs.
        mine = b"# Team rules\n\nUse tabs.\n"
        self.write("AGENTS.md", mine + b"\n" + FULL_BLOCK + b"\n" + FULL_BLOCK + b"\n## Mine too\n")
        out = self.assertRuns("--agent", "codex")
        self.assertIn("Codex: updated", out)
        self.assertEqual(self.read("AGENTS.md"), mine + b"\n" + STUB + b"\n## Mine too\n")

    def test_an_older_block_is_replaced_in_place(self):
        old = STUB.replace(f"Version {VERSION} ".encode(), b"Version 2.0.1 ")
        self.write("AGENTS.md", b"# Top\n\n" + old + b"\n# Bottom\n")
        self.write(".codex/dependency-first-architect.md", ASSET[
            "codex-lazy-dependency-first-architect.md"].replace(f"Version {VERSION} ".encode(),
                                                                b"Version 2.0.1 "))
        self.assertIn("update from 2.0.1", self.assertRuns("--dry-run"))
        self.assertIn("Codex: updated", self.assertRuns())
        self.assertEqual(self.read("AGENTS.md"), b"# Top\n\n" + STUB + b"\n# Bottom\n")

    def test_damaged_blocks_and_other_encodings_stop_the_run(self):
        begin = f"<!--\n{build.BEGIN_LINE}\n".encode()
        cases = {
            b"# Mine\n\n" + begin + b"no end\n": "has no END line",
            b"# Mine\n" + build.END_MARKER.encode() + b"\n": "END line without a BEGIN",
            b"# Mine\n" + build.BEGIN_LINE.encode() + b"\n" + build.END_MARKER.encode(): "no `<!--`",
            begin + begin + build.END_MARKER.encode() + b"\n": "starts inside another",
            "# Mine\n".encode("utf-16"): "not UTF-8 text",
        }
        for data, expected in cases.items():
            with self.subTest(expected=expected):
                self.write("AGENTS.md", data)
                self.assertStops("--agent", "codex", expect=expected)
                self.assertStops("--uninstall", "--agent", "codex", expect=expected)

    def test_an_older_unmarked_copy_is_reported(self):
        self.write("AGENTS.md", "# Mine\n\n# dependency-first-architect\n\nOld v1 text.\n")
        out = self.assertRuns("--agent", "codex")
        self.assertIn("AGENTS.md line 3 starts an older copy", out)

    def test_large_agents_md_gets_a_size_warning_only_when_the_block_is_past_the_limit(self):
        self.write("AGENTS.md", "# Mine\n\n" + "x" * 40000 + "\n")
        self.assertIn("Codex reads only the first 32 KiB", self.assertRuns("--agent", "codex"))
        self.write("AGENTS.md", STUB.decode() + "\n# Mine\n\n" + "x" * 40000 + "\n")
        self.assertNotIn("32 KiB", self.assertRuns("--agent", "codex"))

    def test_merge_function_directly(self):
        block = STUB.decode("utf-8")
        self.assertEqual(inst.merge_agents_md(None, block), block)
        self.assertEqual(inst.merge_agents_md("a\n", block), "a\n\n" + block)
        self.assertEqual(inst.merge_agents_md("a\n\n" + block, None), "a\n")
        self.assertEqual(inst.merge_agents_md(block + "\nb\n", None), "\nb\n")
        self.assertEqual(inst.merge_agents_md("a\n\n" + block, block), "a\n\n" + block)


class PreservationTest(InstallerCase):
    """Paths the installer manages, holding something it did not write, are left alone."""

    def test_a_foreign_cursor_rule_is_not_overwritten(self):
        self.write(".cursor/rules/dependency-first-architect.mdc", "---\nmy own rule\n")
        self.assertStops("--agent", "cursor", expect="was not written by this installer")
        self.assertStops("--uninstall", "--agent", "cursor", expect="was not written by this")

    def test_edits_and_added_files_in_the_skill_folder_are_not_deleted(self):
        self.assertRuns("--agent", "claude-code")
        folder = self.project / ".claude" / "skills" / NAME
        added = self.write("reference/team-conventions.md", "# Ours\n", folder)
        for args in (("--agent", "claude-code"), ("--uninstall", "--agent", "claude-code")):
            with self.subTest(args=args):
                self.assertStops(*args, expect="holds files this installer did not put there")
        added.unlink()
        skill = folder / "SKILL.md"
        skill.write_bytes(skill.read_bytes() + b"\nOur own rule.\n")
        for args in (("--agent", "claude-code"), ("--uninstall", "--agent", "claude-code")):
            with self.subTest(args=args):
                self.assertStops(*args, expect="differs from every released version")
        self.assertTrue(skill.read_bytes().endswith(b"Our own rule.\n"))

    def test_read_only_files_do_not_block_runs_that_write_nothing(self):
        self.write("AGENTS.md", "# Mine\n")
        self.assertRuns("--agent", "claude-code", "--agent", "codex")
        for path in (self.project / "AGENTS.md",
                     self.project / ".claude" / "skills" / NAME / "SKILL.md"):
            make_read_only(self, path)
        self.assertIn("exactly as released", self.assertRuns("--verify"))
        self.assertEqual(self.assertRuns().count("nothing to change"), 2)
        self.assertRuns("--dry-run", "--uninstall")
        # A read-only file in the skill folder does not stop an uninstall either.
        self.assertIn("Claude Code: removed", self.assertRuns("--uninstall", "--agent",
                                                              "claude-code"))

    def test_bare_uninstall_and_verify_report_what_they_leave_alone(self):
        self.assertRuns("--agent", "claude-code", "--agent", "cursor")
        folder = self.project / ".claude" / "skills" / NAME
        self.write("reference/team.md", "# Ours\n", folder)
        code, out = self.run_installer("--verify")
        self.assertEqual(code, 1)
        self.assertIn("Claude Code: cannot check:", out)
        code, out = self.run_installer("--uninstall")
        self.assertEqual(code, 1, out)
        self.assertIn("Claude Code: left alone:", out)
        self.assertIn("Cursor: removed", out)
        self.assertIn("Done, except for what was left alone above.", out)
        self.assertTrue((folder / "reference" / "team.md").is_file())

    def test_read_only_files_stop_the_run_before_anything_is_written(self):
        self.write("AGENTS.md", "# Mine\n")
        make_read_only(self, self.project / "AGENTS.md")
        self.assertStops("--agent", "codex", expect="is read-only")
        rule = self.write(".cursor/rules/dependency-first-architect.mdc",
                          ASSET["cursor-dependency-first-architect.mdc"].replace(b"## Core", b"## Old"))
        make_read_only(self, rule)
        self.assertStops("--agent", "cursor", expect="is read-only")

    def test_a_cloned_repository_in_the_skill_folder_is_not_deleted(self):
        clone = self.project / ".claude" / "skills" / NAME
        self.write("SKILL.md", PACKAGE["SKILL.md"], clone)
        self.write(".git/HEAD", "ref: refs/heads/main\n", clone)
        self.write("eval/run.py", "print()\n", clone)
        out = self.assertStops("--agent", "claude-code", expect="holds files this installer did")
        self.assertIn("copy of the whole repository", out)
        self.assertStops("--uninstall", "--agent", "claude-code",
                         expect="holds files this installer did")
        # Not an install of ours: a plain uninstall says what it leaves alone, and why.
        out = self.assertStops("--uninstall", expect="Claude Code: left alone:")
        self.assertIn("copy of the whole repository", out)

    def test_another_skill_with_the_folder_name_is_not_replaced(self):
        self.write(".claude/skills/dependency-first-architect/SKILL.md",
                   "---\nname: something-else\n---\n")
        self.assertStops("--agent", "claude-code", expect="differs from every released version")

    def test_other_skills_rules_and_files_are_untouched(self):
        other = {".claude/skills/mine/SKILL.md": b"---\nname: mine\n---\n",
                 ".cursor/rules/style.mdc": b"---\nalwaysApply: true\n---\nTabs.\n",
                 ".codex/config.toml": b"model = 'x'\n", "CLAUDE.md": b"# Notes\n"}
        for rel, data in other.items():
            self.write(rel, data)
        args = ("--agent", "claude-code", "--agent", "cursor", "--agent", "codex")
        self.assertRuns(*args)
        self.assertRuns("--uninstall")
        after = {k: v for k, v in snapshot(self.project).items() if not k.endswith("/")}
        self.assertEqual(after, other)

    def test_links_are_not_written_through(self):
        target = self.tmp / "elsewhere"
        target.mkdir()
        (target / "AGENTS.md").write_bytes(b"# Shared\n")
        try:
            os.symlink(target / "AGENTS.md", self.project / "AGENTS.md")
            os.symlink(target, self.project / ".cursor", target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"cannot make symbolic links here: {exc}")
        self.assertStops("--agent", "codex", expect="is a symbolic link or junction")
        self.assertStops("--agent", "cursor", expect="is a symbolic link or junction")
        self.assertEqual(sorted(p.name for p in target.iterdir()), ["AGENTS.md"])

    @unittest.skipUnless(sys.platform == "win32", "junctions are Windows-only")
    def test_junctions_are_links(self):
        import _winapi
        target = self.tmp / "elsewhere"
        target.mkdir()
        (self.project / ".claude").mkdir()
        _winapi.CreateJunction(str(target), str(self.project / ".claude" / "skills"))
        self.assertStops("--agent", "claude-code", expect="is a symbolic link or junction")
        self.assertEqual(list(target.iterdir()), [])

    def test_global_works_with_a_linked_skills_folder(self):
        real = self.tmp / "dotfiles" / "skills"
        real.mkdir(parents=True)
        (self.home / ".claude").mkdir()
        try:
            if sys.platform == "win32":
                import _winapi
                _winapi.CreateJunction(str(real), str(self.home / ".claude" / "skills"))
            else:
                os.symlink(real, self.home / ".claude" / "skills", target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"cannot link here: {exc}")
        self.assertRuns("--global")
        self.assertEqual(self.skill_files(self.home), PACKAGE)
        self.assertEqual(sorted(p.name for p in real.parent.iterdir()), ["skills"])  # tidied
        self.assertRuns("--global", "--uninstall")
        self.assertEqual(list(real.iterdir()), [])

    def test_two_claude_code_copies_are_reported(self):
        self.assertRuns("--global")
        out = self.assertRuns("--agent", "claude-code")
        self.assertIn("Claude Code may load both", out)
        self.assertIn("--global --uninstall", out)


class UpdateTest(InstallerCase):
    def test_the_released_v2_0_1_package_is_recognized(self):
        released = (ROOT / pkg.RELEASED_SUMS).read_text("ascii")
        self.assertIn("# v2.0.1\n", released)
        self.assertIn(released, inst.RELEASED)
        # Every dated release from 2.0.1 on, before this one, has its package lines there.
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        for version, label in build.CHANGELOG_HEADING.findall(changelog):
            core = tuple(int(n) for n in version.split("-")[0].split("+")[0].split(".")
                         if n.isdigit())
            if (len(core) == 3 and (2, 0, 1) <= core and version != VERSION
                    and build.is_date(label.strip().lstrip("\u2014\u2013-").strip())):
                self.assertIn(f"# v{version}\n", released, version)

    def test_an_older_skill_folder_is_replaced_whole(self):
        folder = self.project / ".claude" / "skills" / NAME
        older = {"SKILL.md": PACKAGE["SKILL.md"].replace(b"## Procedure", b"## Old procedure"),
                 "reference/retired.md": b"# Gone in this version\n"}
        for rel, data in older.items():
            self.write(rel, data, folder)
        self.write(".DS_Store", b"\x00\x01", folder)
        with knowing(older):
            out = self.assertRuns()
        self.assertIn("Claude Code: updated", out)
        self.assertEqual(self.skill_files(), PACKAGE)
        self.assertEqual([p.name for p in (self.project / ".claude").iterdir()], ["skills"])

    def test_an_edited_rule_is_replaced_and_verify_reports_it_first(self):
        self.assertRuns("--agent", "cursor")
        rule = self.project / ".cursor/rules/dependency-first-architect.mdc"
        rule.write_bytes(rule.read_bytes().replace(b"## Core principles", b"## My principles"))
        code, out = self.run_installer("--verify")
        self.assertEqual(code, 1)
        self.assertIn(f"Cursor: version {VERSION}, or edited", out)
        self.assertIn("Cursor: updated", self.assertRuns())
        self.assertEqual(self.run_installer("--verify")[0], 0)

    def test_a_crlf_checkout_of_the_same_files_is_current(self):
        self.assertRuns("--agent", "cursor")
        rule = self.project / ".cursor/rules/dependency-first-architect.mdc"
        rule.write_bytes(rule.read_bytes().replace(b"\n", b"\r\n"))  # git autocrlf on Windows
        self.assertIn("nothing to change", self.assertRuns())

    def test_the_older_cursor_rule_name_is_cleaned_up(self):
        old = self.write(".cursor/rules/cursor-dependency-first-architect.mdc",
                         "---\nalwaysApply: false\n---\n\n<!--\n GENERATED FILE \u2014 DO NOT "
                         "HAND-EDIT.\n-->\n\n# Dependency-First Architect\n")
        out = self.assertRuns("--agent", "cursor")
        self.assertIn("deleted an older copy, cursor-dependency-first-architect.mdc", out)
        self.assertFalse(old.exists())


class InterruptionTest(InstallerCase):
    """A run that fails part-way keeps the previous install; the next run recovers."""

    def test_a_failed_rename_keeps_the_previous_rule(self):
        old = ASSET["cursor-dependency-first-architect.mdc"].replace(b"Version ", b"Version 0")
        rule = self.write(".cursor/rules/dependency-first-architect.mdc", old)
        with mock.patch.object(inst.os, "replace", side_effect=OSError("disk full")):
            code, out = self.run_installer("--agent", "cursor")
        self.assertEqual(code, 1)
        self.assertIn("FAILED to write", out)
        self.assertEqual(rule.read_bytes(), old)
        self.assertEqual(sorted(p.name for p in rule.parent.iterdir()), [rule.name])

    def test_a_failed_swap_restores_the_previous_skill_folder(self):
        folder = self.project / ".claude" / "skills" / NAME
        old_skill = PACKAGE["SKILL.md"].replace(b"## Procedure", b"## Old procedure")
        self.write("SKILL.md", old_skill, folder)
        self.enterContext(knowing({"SKILL.md": old_skill}))
        real_rename, calls = os.rename, []

        def flaky(src, dst):
            calls.append((src, dst))
            if len(calls) == 2:  # the staged folder into place
                raise OSError("in use")
            return real_rename(src, dst)

        with mock.patch.object(inst.os, "rename", side_effect=flaky):
            code, out = self.run_installer("--agent", "claude-code")
        self.assertEqual(code, 1, out)
        self.assertIn("in use", out)  # the real reason, not a cleanup error
        self.assertEqual(self.skill_files(), {"SKILL.md": old_skill})
        self.assertEqual(sorted(p.name for p in (self.project / ".claude").iterdir()), ["skills"])

    def test_leftovers_of_an_interrupted_run_are_recovered(self):
        claude = self.project / ".claude"
        # Killed after moving the old folder aside, before moving the new one in.
        for rel, data in PACKAGE.items():
            self.write(rel, data, claude / ".dfa-old-0badc0de")
        self.write("SKILL.md", PACKAGE["SKILL.md"][:100], claude / ".dfa-staging-12345678")
        self.write("LICENSE", PACKAGE["LICENSE"], claude / ".dfa-staging-9abcdef0")  # no SKILL.md
        tmp_rule = self.write(".cursor/rules/.dependency-first-architect.mdc.dfa-tmp-abcd", "half")
        tmp_agents = self.write(".AGENTS.md.dfa-tmp-abcd", "half")
        (claude / "skills").mkdir()
        out = self.assertRuns("--agent", "claude-code", "--agent", "cursor", "--agent", "codex")
        self.assertIn("restored .claude/skills/dependency-first-architect/", out)
        self.assertEqual(self.skill_files(), PACKAGE)
        self.assertEqual(sorted(p.name for p in claude.iterdir()), ["skills"])
        self.assertFalse(tmp_rule.exists() or tmp_agents.exists())

    def test_leftovers_next_to_a_current_install_are_tidied_up(self):
        self.assertRuns("--agent", "claude-code", "--agent", "cursor")
        claude = self.project / ".claude"
        shutil.copytree(claude / "skills" / NAME, claude / ".dfa-old-0badc0de")
        tmp_rule = self.write(".cursor/rules/.dependency-first-architect.mdc.dfa-tmp-abcd", "half")
        make_read_only(self, tmp_rule)  # what a failed write leaves on Windows
        out = self.assertRuns()
        self.assertEqual(out.count("nothing to change"), 2, out)
        self.assertEqual(sorted(p.name for p in claude.iterdir()), ["skills"])
        self.assertFalse(tmp_rule.exists())

    def test_an_interrupted_uninstall_is_finished(self):
        self.assertRuns("--agent", "claude-code")
        claude = self.project / ".claude"
        os.rename(claude / "skills" / NAME, claude / ".dfa-old-deadbeef")  # killed mid-uninstall
        out = self.assertRuns("--uninstall")
        self.assertIn("Claude Code: removed", out)
        self.assertEqual(sorted(p.name for p in self.project.iterdir()), [".git"])

    def test_a_codex_failure_puts_the_codex_file_back(self):
        self.write("AGENTS.md", "# Mine\n")
        real = inst.write_atomic

        def fail_on_agents_md(path, data):
            if path.name == "AGENTS.md":
                raise OSError("locked by an editor")
            return real(path, data)

        with mock.patch.object(inst, "write_atomic", side_effect=fail_on_agents_md):
            code, out = self.run_installer("--agent", "codex")
        self.assertEqual(code, 1, out)
        self.assertIn("left as it was before this run", out)
        self.assertEqual(self.read("AGENTS.md"), b"# Mine\n")
        self.assertFalse((self.project / ".codex" / f"{NAME}.md").exists())

    def test_a_stop_after_tidying_up_says_so(self):
        self.assertRuns("--agent", "claude-code")
        claude = self.project / ".claude"
        os.rename(claude / "skills" / NAME, claude / ".dfa-old-0e")  # killed mid-update
        self.write(".cursor/rules/dependency-first-architect.mdc", "my own rule\n")
        code, out = self.run_installer("--agent", "cursor")
        self.assertEqual(code, 1, out)
        self.assertIn("restored .claude/skills/dependency-first-architect/", out)
        self.assertIn("Nothing else was changed (only what an earlier, interrupted run left", out)
        self.assertEqual(self.skill_files(), PACKAGE)

    def test_an_older_cursor_copy_that_cannot_be_deleted_puts_the_rule_back(self):
        old = self.write(".cursor/rules/cursor-dependency-first-architect.mdc",
                         ASSET["cursor-dependency-first-architect.mdc"])
        real = inst.unlink_or_fail

        def locked(path):
            if path.name == old.name:
                raise PermissionError("in use")
            return real(path)

        with mock.patch.object(inst, "unlink_or_fail", side_effect=locked):
            code, out = self.run_installer("--agent", "cursor")
        self.assertEqual(code, 1, out)
        self.assertIn("left as it was before this run", out)
        self.assertFalse((self.project / ".cursor/rules/dependency-first-architect.mdc").exists())
        self.assertTrue(old.exists())

    def test_a_codex_uninstall_that_cannot_delete_its_file_keeps_the_block(self):
        self.write("AGENTS.md", "# Mine\n")
        self.assertRuns("--agent", "codex")
        before = self.read("AGENTS.md")
        with mock.patch.object(inst, "unlink_or_fail", side_effect=PermissionError("in use")):
            code, out = self.run_installer("--uninstall")
        self.assertEqual(code, 1, out)
        self.assertIn("Codex: left installed", out)
        self.assertEqual(self.read("AGENTS.md"), before)
        self.assertEqual(self.run_installer("--verify")[0], 0)

    def test_a_failure_after_another_agent_says_what_was_done(self):
        self.write(".cursor/rules", "a file where a folder should be")
        code, out = self.run_installer("--agent", "claude-code", "--agent", "cursor")
        self.assertEqual(code, 1, out)
        self.assertIn("Already installed or updated in this run, and left so: Claude Code.", out)

    def test_an_unwritable_destination_fails_without_partial_files(self):
        self.write(".cursor/rules", "a file where a folder should be")
        code, out = self.run_installer("--agent", "cursor")
        self.assertEqual(code, 1, out)
        self.assertEqual(sorted(snapshot(self.project)), [".cursor/", ".cursor/rules"])


class UninstallTest(InstallerCase):
    def test_removes_only_what_it_installed(self):
        (self.project / ".claude").mkdir()
        self.write(".claude/settings.json", "{}\n")
        self.write("AGENTS.md", "# Mine\n")
        self.assertRuns("--agent", "claude-code", "--agent", "cursor", "--agent", "codex")
        self.assertIn("would remove", self.assertRuns("--uninstall", "--dry-run"))
        out = self.assertRuns("--uninstall")
        self.assertEqual(out.count(": removed"), 3, out)
        self.assertEqual({k: v for k, v in snapshot(self.project).items()},
                         {".claude/": None, ".claude/settings.json": b"{}\n",
                          "AGENTS.md": b"# Mine\n"})

    def test_nothing_installed(self):
        self.assertIn("Nothing from Dependency-First Architect is installed here",
                      self.assertRuns("--uninstall"))
        code, _ = self.run_installer("--verify")
        self.assertEqual(code, 1)

    def test_global_uninstall_leaves_the_rest_of_the_home_folder(self):
        self.write(".claude/skills/mine/SKILL.md", "---\nname: mine\n---\n", self.home)
        self.assertRuns("--global")
        self.assertRuns("--global", "--uninstall")
        self.assertFalse((self.home / ".claude/skills" / NAME).exists())
        self.assertTrue((self.home / ".claude/skills/mine/SKILL.md").is_file())


README = (ROOT / "README.md").read_text(encoding="utf-8")
RELEASE_URL = ("https://github.com/NiravRVaghasiya/dependency-first-architect/releases/download/"
               f"v{VERSION}/")


def readme_block(marker):
    """The README's fenced code block that contains `marker`."""
    blocks = [b for b in README.split("```")[1::2] if marker in b]
    if len(blocks) != 1:
        raise AssertionError(f"expected one README block with {marker!r}, found {len(blocks)}")
    return blocks[0].split("\n", 1)[1]


def find_bash():
    for name in ("bash", "sh"):
        path = shutil.which(name)
        if path and "system32" not in path.lower():  # WSL's bash.exe runs in another system
            return path
    return None


class ReadmeBlocksTest(InstallerCase):
    """The README's install blocks, run as written, with the release URL pointed at a local
    folder and the installer told which agent and project to use."""

    def setUp(self):
        super().setUp()
        if INSTALLER_NAME not in README:
            self.skipTest("the README names another release")  # test_matrix checks which
        self.served = self.tmp / "served"
        self.served.mkdir()
        self.temp = self.tmp / "temp"
        self.temp.mkdir()
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("HOME", "USERPROFILE", "TMP", "TEMP", "TMPDIR")}
        self.env.update(HOME=str(self.home), USERPROFILE=str(self.home), TMP=str(self.temp),
                        TEMP=str(self.temp), TMPDIR=str(self.temp))
        self.exe = Path(sys.executable).as_posix()

    def serve(self, data):
        if data is not None:
            (self.served / INSTALLER_NAME).write_bytes(data)

    def check_outcomes(self, run):
        rule = self.project / ".cursor/rules/dependency-first-architect.mdc"
        self.serve(INSTALLER)
        out = run()
        self.assertIn("Cursor: installed", out)
        self.assertEqual(rule.read_bytes(), ASSET["cursor-dependency-first-architect.mdc"])
        rule.unlink()
        self.serve(INSTALLER.replace(b"Dependency-First", b"Dependency-Last", 1))  # tampered
        out = run()
        self.assertIn("Not run: the installer is missing or does not match", out)
        self.assertFalse(rule.exists())
        (self.served / INSTALLER_NAME).unlink()  # the download fails
        out = run()
        self.assertIn("Not run: the installer is missing or does not match", out)
        self.assertFalse(rule.exists())

    def test_bash_block(self):
        bash = find_bash()
        if not bash or not shutil.which("curl"):
            self.skipTest("bash and curl are needed")
        script = readme_block(f"f={INSTALLER_NAME}")
        self.assertEqual(script.count(RELEASE_URL), 1)
        script = script.replace(RELEASE_URL, self.served.as_uri() + "/")
        runs = 'python3 -I "$t/$f";'
        self.assertEqual(script.count(runs), 1)
        script = script.replace(runs, f'"{self.exe}" -I "$t/$f" --agent cursor '
                                      f'--project "{self.project.as_posix()}";')

        def run():
            result = subprocess.run([bash, "-c", script], cwd=self.project, env=self.env,
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                    timeout=120)
            return result.stdout + result.stderr

        self.check_outcomes(run)

    def test_powershell_block(self):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            self.skipTest("PowerShell is needed")
        script = readme_block(f'$f = "{INSTALLER_NAME}"')
        self.assertEqual(script.count(RELEASE_URL), 1)
        script = script.replace(RELEASE_URL, self.served.as_uri() + "/")
        if sys.platform != "win32":
            script = script.replace("curl.exe ", "curl ")  # curl.exe is Windows' name for it
        runs = "{ py -3 -I $t }"
        self.assertEqual(script.count(runs), 1)
        script = script.replace(runs, f'{{ & "{self.exe}" -I $t --agent cursor --project '
                                      f'"{self.project}" }}')

        def run():
            result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", script],
                                    cwd=self.project, env=self.env, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=180)
            return result.stdout + result.stderr

        self.check_outcomes(run)

    def test_every_option_the_readme_names_exists(self):
        section = README.split("### Options\n", 1)[1].split("\n### ", 1)[0]
        options = set(re.findall(r"`(--[a-z][a-z-]*)", section))
        self.assertTrue({"--agent", "--global", "--dry-run", "--verify", "--uninstall",
                         "--project"} <= options, options)
        for option in options:
            argv = [option, "x"] if option in ("--project",) else [option]
            if option == "--agent":
                argv = [option, "cursor"]
            with self.subTest(option=option):
                inst.parse_args(argv)  # argparse exits on an unknown option

    def test_the_readme_table_names_what_the_installer_writes(self):
        section = README.split("## Install\n", 1)[1].split("\n### ", 1)[0]
        for path in ("/".join(inst.SKILL_DIR) + "/", "/".join(inst.CURSOR_RULE),
                     "/".join(inst.CODEX_FILE), "AGENTS.md"):
            self.assertIn(f"`{path}", section, path)


if __name__ == "__main__":
    unittest.main()
