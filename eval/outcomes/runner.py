#!/usr/bin/env python3
"""Run an outcome task's hidden tests against one implementation and print the results as JSON.

    python runner.py --task-dir eval/outcomes/tasks/webhook-ledger --code-dir <impl> --round 1 \
        --json results.json

The code under test is model-written, so this script is meant to run in a child process started
by `run_tests()` (below): isolated mode (-I), a scrubbed environment with no credentials, and a
timeout. Even so it executes untrusted code: only run it in a container or VM you can throw away.

The task's canonical `ledgerkit` (its environment: the database client and clock) always comes
first on sys.path, from a private copy, so an implementation cannot change the environment the
tests run against by editing its own copy. Results: {"status", "error", "by_category", "tests"},
the shape of `schemas.TEST_RESULTS`. Exit code 0 whether or not tests pass; 2 on runner errors.
Standard library only, and no imports from the harness, so it runs anywhere.
"""

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
import unittest
from pathlib import Path

KEEP_ENV = ("PATH", "SYSTEMROOT", "SystemRoot", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
            "HOME", "USERPROFILE", "LANG", "LC_ALL")


def scrubbed_env(environ=None):
    """A minimal environment for model-written code: no credentials, tokens, or config.

    Keeps only what an interpreter needs to start (PATH, the Windows system directories, temp
    directories, home, locale) and sets PYTHONHASHSEED, PYTHONDONTWRITEBYTECODE and
    PYTHONIOENCODING. Everything else (AWS_*, ANTHROPIC_*, OPENAI_API_KEY, GITHUB_TOKEN, ...) is
    dropped.
    """
    environ = os.environ if environ is None else environ
    env = {key: environ[key] for key in KEEP_ENV if key in environ}
    env.update(PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    return env


# A service that keeps balances needs none of these. Code that uses them is not run: a cheap
# screen against an implementation that would touch the network, spawn processes, or delete files.
# It is a heuristic, not a sandbox (see SECURITY.md).
FORBIDDEN_IMPORTS = ("subprocess", "socket", "ctypes", "multiprocessing", "asyncio.subprocess",
                     "ftplib", "smtplib", "telnetlib", "requests", "http.client", "http.server",
                     "urllib.request")
FORBIDDEN_CALLS = ("os.system", "os.popen", "os.remove", "os.unlink", "os.rmdir",
                   "os.removedirs", "os.kill", "os.fork", "os.execv", "os.execl", "os.spawnv",
                   "os.startfile", "shutil.rmtree", "eval", "exec", "__import__")
DEFAULT_CODE_GLOBS = ("ledger/**/*.py",)


def stage_code(code_dir, globs, dest):
    """Copy only the implementation files (the task's code_globs) from code_dir into dest.

    The hidden tests run against this copy, so files an implementer leaves elsewhere in its
    sandbox (its own tests, a module named like a hidden test) can neither shadow the hidden
    tests nor be imported, and what is tested is exactly what the attempt record snapshots.
    """
    code_dir, dest = Path(code_dir), Path(dest)
    for pattern in globs:
        for path in sorted(code_dir.glob(pattern)):
            if path.is_file() and "__pycache__" not in path.parts:
                target = dest / path.relative_to(code_dir)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
    return dest


def screen_code(code_dir):
    """Reasons not to execute the Python files under `code_dir` (empty list: none found).

    ledgerkit/ is skipped: the canonical copy replaces it anyway.
    """
    import ast
    problems = []
    for path in sorted(Path(code_dir).rglob("*.py")):
        rel = path.relative_to(code_dir).as_posix()
        if rel.startswith("ledgerkit/"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=rel)
        except SyntaxError:
            continue  # it will fail to import, and the tests will report that
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if any(name == f or name.startswith(f + ".") for f in FORBIDDEN_IMPORTS):
                    problems.append(f"{rel}:{node.lineno}: imports {name}")
            if isinstance(node, ast.Call):
                func = node.func
                dotted = None
                if isinstance(func, ast.Name):
                    dotted = func.id
                elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                    dotted = f"{func.value.id}.{func.attr}"
                if dotted in FORBIDDEN_CALLS:
                    problems.append(f"{rel}:{node.lineno}: calls {dotted}")
    return problems


def load_task(task_dir):
    return json.loads((Path(task_dir) / "task.json").read_text(encoding="utf-8"))


class _Collector(unittest.TestResult):
    """One row per test: pass, fail, error or skip. A test whose subtests fail is one failed row
    (its first failing subtest's message), so every test counts once."""

    def __init__(self, categories):
        super().__init__()
        self.categories = categories
        self.module = None  # the hidden-test module being run
        self.rows = []
        self._failed_subtests = set()

    def _row(self, test, outcome, message=None):
        self.rows.append({"id": test.id(), "category": self.categories.get(self.module, "unknown"),
                          "outcome": outcome, "message": message})

    def addSuccess(self, test):
        if test.id() not in self._failed_subtests:
            self._row(test, "pass")

    def addFailure(self, test, err):
        if test.id() not in self._failed_subtests:  # already recorded once, as failed
            self._row(test, "fail", _short(err))

    def addError(self, test, err):
        if test.id() not in self._failed_subtests:
            self._row(test, "error", _short(err))

    def addSkip(self, test, reason):
        self._row(test, "skip", str(reason)[:300])

    def addSubTest(self, test, subtest, err):
        if err is not None and test.id() not in self._failed_subtests:
            self._failed_subtests.add(test.id())
            outcome = "fail" if issubclass(err[0], test.failureException) else "error"
            self._row(test, outcome, f"{subtest._subDescription()}: {_short(err)}")


def _short(err):
    kind, value, _ = err
    return f"{kind.__name__}: {value}"[:500]


def collect(task_dir, code_dir, round_number):
    """Run the round's hidden test modules in this process and return the results dict."""
    task_dir, code_dir = Path(task_dir).resolve(), Path(code_dir).resolve()
    task = load_task(task_dir)
    modules = task[f"round{round_number}_modules"]
    categories = task["categories"]
    env_dir = Path(tempfile.mkdtemp(prefix="ledgerkit-"))
    try:
        shutil.copytree(task_dir / "starter" / "ledgerkit", env_dir / "ledgerkit")
        sys.path[:0] = [str(env_dir), str(code_dir), str(task_dir / "hidden_tests")]
        result = _Collector(categories)
        loader = unittest.TestLoader()
        for module in modules:
            result.module = module
            try:
                suite = loader.loadTestsFromName(module)
            except Exception:  # the implementation failed to import: the module errors
                result.rows.append({"id": f"{module}.<import>", "category":
                                    categories.get(module, "unknown"), "outcome": "error",
                                    "message": traceback.format_exc(limit=2)[-500:]})
                continue
            suite.run(result)
        by_category = {}
        for row in result.rows:
            counts = by_category.setdefault(row["category"], {"passed": 0, "total": 0})
            counts["total"] += 1
            counts["passed"] += row["outcome"] == "pass"
        return {"status": "ok", "error": None, "by_category": dict(sorted(by_category.items())),
                "tests": sorted(result.rows, key=lambda row: row["id"])}
    finally:
        shutil.rmtree(env_dir, ignore_errors=True)


def run_tests(task_dir, code_dir, round_number, timeout_s=120, python=None):
    """Run this script in a child process and return its results dict.

    Only the implementation files (the task's code_globs) are copied into a fresh directory and
    tested. The child gets `scrubbed_env()` (no credentials; PYTHONHASHSEED=0), no user site
    directory (-s) and no bytecode writes (-B). Code that fails the static screen is not run
    (status "refused"); a child that runs past `timeout_s` is killed and reported as status
    "timeout"; one that crashes, as status "error".
    """
    task = load_task(task_dir)
    out_dir = Path(tempfile.mkdtemp(prefix="outcome-run-"))
    staged = stage_code(code_dir, task.get("code_globs", DEFAULT_CODE_GLOBS), out_dir / "code")
    refused = screen_code(staged)
    if refused:
        shutil.rmtree(out_dir, ignore_errors=True)
        return {"status": "refused", "error": "not run: the code uses something a ledger service "
                "does not need: " + "; ".join(refused[:5]), "by_category": {}, "tests": []}
    out = out_dir / "results.json"
    cmd = [python or sys.executable, "-s", "-B", str(Path(__file__).resolve()), "--task-dir",
           str(Path(task_dir).resolve()), "--code-dir", str(staged.resolve()), "--round",
           str(round_number), "--json", str(out)]
    try:
        proc = subprocess.run(cmd, cwd=str(out_dir), env=scrubbed_env(), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout_s)
        if out.is_file():
            return json.loads(out.read_text(encoding="utf-8"))
        return {"status": "error", "error": f"runner exited {proc.returncode}: "
                f"{(proc.stderr or proc.stdout)[-500:]}", "by_category": {}, "tests": []}
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "error": f"tests did not finish within {timeout_s} s",
                "by_category": {}, "tests": []}
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--task-dir", required=True)
    parser.add_argument("--code-dir", required=True)
    parser.add_argument("--round", type=int, choices=(1, 2), required=True)
    parser.add_argument("--json", required=True, help="where to write the results")
    args = parser.parse_args(argv)
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    try:
        results = collect(args.task_dir, args.code_dir, args.round)
        code = 0
    except Exception:
        results = {"status": "error", "error": traceback.format_exc(limit=3)[-800:],
                   "by_category": {}, "tests": []}
        code = 2
    Path(args.json).write_bytes((json.dumps(results, indent=2) + "\n").encode("utf-8"))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
