"""Providers: parsing what the Claude CLI prints, the model check, and every provider's contract.

No test calls a network or a real model: the Claude CLI is replaced by a stub script, HTTP by a
patched urlopen, agents by small Python scripts, and the fake provider is offline by design.
Standard library only:  python -m unittest discover -s tests -v
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))

from dfa_eval import generate, judge, providers, schema, schemas, workspace  # noqa: E402

DFA = "dependency-first-architect"
V1_DIR = ROOT / "eval" / "conditions" / "dfa-v1.0.0"
CONTROL_DIR = ROOT / "eval" / "conditions" / "generic-architect"
# The real `claude -p --output-format json` result, as captured (abbreviated).
JSON_RESULT = ('{"type":"result","subtype":"success","is_error":false,"duration_ms":1339,'
               '"num_turns":1,"result":"OK","total_cost_usd":0.00022,"usage":{"input_tokens":90,'
               '"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"output_tokens":4,'
               '"output_tokens_details":{"thinking_tokens":0}},"modelUsage":{"claude-sonnet-5-5":'
               '{"inputTokens":90,"outputTokens":4,"costUSD":0.00022}},"structured_output":null}')


def injected(skill_dir):
    """The user turn a `/<skill> ...` slash command adds: the skill's directory, then its body."""
    _, body = workspace.read_skill(skill_dir)
    return f"Base directory for this skill: {skill_dir}\n\n{body}"


def stream_transcript(plugin, workdir, init_model="claude-opus-5-5", model_usage=None,
                      result_text="# BUILD PLAN\n\nThe plan."):
    """A stream-json transcript in the shape run_eval.py parses: init, an injected skill body,
    three Read calls (ok, failed, never answered), a final text block and the result event.
    Lines are what JSON.stringify writes: non-ASCII characters (U+2028 included) unescaped."""
    skill = Path(plugin) / "skills" / DFA
    if model_usage is None:
        model_usage = {"claude-opus-5-5": {"inputTokens": 1200, "outputTokens": 900},
                       "claude-haiku-4-5": {"inputTokens": 10, "outputTokens": 2}}
    events = [
        {"type": "system", "subtype": "init", "cwd": str(workdir), "model": init_model,
         "skills": [{"name": DFA}, "other-skill"], "slash_commands": ["compact", DFA]},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": injected(skill)}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Loading the template."},
            {"type": "tool_use", "id": "t1", "name": "Read",
             "input": {"file_path": str(skill / "reference" / "plan-template.md")}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "# BUILD PLAN — output"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t2", "name": "Read",
             "input": {"file_path": str(Path(workdir) / "package.json")}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t2", "is_error": True,
             "content": "File does not exist."}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t3", "name": "Read",
             "input": {"file_path": str(skill / "reference" / "validation.md")}}]}},
        {"type": "result", "subtype": "success", "is_error": False, "num_turns": 4,
         "result": result_text, "total_cost_usd": 0.4321,
         "usage": {"input_tokens": 1200, "cache_creation_input_tokens": 300,
                   "cache_read_input_tokens": 50, "output_tokens": 900,
                   "output_tokens_details": {"thinking_tokens": 400}},
         "modelUsage": model_usage},
    ]
    return ("progress: starting\n" + "\n".join(json.dumps(e, ensure_ascii=False) for e in events)
            + "\n")


# Stands in for the claude CLI: records what it got (stdin as exact bytes), prints the canned
# output as UTF-8 bytes (as node does), and can hang the way a launcher or .cmd shim does: the
# process holding the pipes is a child of the one the harness started.
STUB_CLAUDE = r'''
import json, os, subprocess, sys, time
from pathlib import Path
args = sys.argv[1:]
prompt = sys.stdin.buffer.read().decode("utf-8")
Path("seen.json").write_text(json.dumps({"args": args, "prompt": prompt, "cwd": os.getcwd()}),
                             encoding="utf-8")

def hang(seconds):
    end = time.monotonic() + float(seconds)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(%s)" % seconds])
    Path("child.pid").write_text(str(child.pid), encoding="utf-8")
    child.wait()
    while time.monotonic() < end:  # hang the full time even if the child dies early (e.g. a
        time.sleep(0.2)            # cleanup that kills a recycled pid): only a kill may end it

if os.environ.get("STUB_SLEEP"):
    time.sleep(float(os.environ["STUB_SLEEP"]))
if os.environ.get("STUB_HANG_BEFORE"):
    hang(os.environ["STUB_HANG_BEFORE"])
if "--json-schema" in args:
    sys.stdout.buffer.write((os.environ["STUB_JSON"] + "\n").encode("utf-8"))
else:
    sys.stdout.buffer.write(Path(os.environ["STUB_STREAM"]).read_bytes())
sys.stdout.flush()
if os.environ.get("STUB_HANG_AFTER"):
    hang(os.environ["STUB_HANG_AFTER"])
sys.exit(int(os.environ.get("STUB_EXIT", "0")))
'''


def process_alive(pid):
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True)
        return f'"{pid}"' in out.stdout
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():  # Linux: a zombie (killed, not yet reaped) is gone
        try:
            return stat.read_text().rsplit(")", 1)[1].split()[0] != "Z"
        except (OSError, IndexError):
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def gone_within(pid, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not process_alive(pid):
            return True
        time.sleep(0.2)
    return not process_alive(pid)


def kill_quietly(pid):
    """Kill a stub's sleeping child that a failing test left behind; only if `pid` still is that
    child (a Python process; on Linux, the sleeper's command line), never whoever reused it."""
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True).stdout
        if f'"{pid}"' in out and "python" in out.split(",")[0].lower():
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        return
    try:
        mine = b"time.sleep" in Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        mine = False
    if mine:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


class ClaudeOutputParsingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.plugin, _ = workspace.build_plugin(ROOT, DFA, self.tmp / "plugins" / "p1")
        self.workdir = self.tmp / "w12345678"
        self.workdir.mkdir()
        self.events = providers.parse_events(stream_transcript(self.plugin, self.workdir))

    def test_parse_events_skips_lines_that_are_not_json_objects(self):
        self.assertEqual(providers.parse_events('noise\n{"type": "a"}\n[1, 2]\n\n{"type": "b"}'),
                         [{"type": "a"}, {"type": "b"}])
        self.assertEqual(len(self.events), 8)
        self.assertEqual(providers.parse_events('{"type": "a"}\r\n{"type": "b"}\r\n'),
                         [{"type": "a"}, {"type": "b"}])

    def test_unicode_line_separators_in_model_text_do_not_split_an_event(self):
        # JSON.stringify leaves U+2028, U+2029 and U+0085 unescaped; str.splitlines() breaks on
        # them, which cut the result line apart and lost a paid, successful generation.
        text = "Phase 1\u2028Phase 2\u0085Phase 3\u2029done"
        transcript = stream_transcript(self.plugin, self.workdir, result_text=text)
        self.assertIn("\u2028", transcript)  # raw, as node writes it
        events = providers.parse_events(transcript)
        self.assertEqual(len(events), 8)
        self.assertEqual(providers.result_event(events)["result"], text)

    def test_result_event_fields(self):
        result = providers.result_event(self.events)
        self.assertEqual(result["result"], "# BUILD PLAN\n\nThe plan.")
        self.assertEqual(providers.usage_from(result["usage"]), {
            "input_tokens": 1200, "output_tokens": 900, "thinking_tokens": 400,
            "cache_read_input_tokens": 50, "cache_creation_input_tokens": 300})
        self.assertEqual(providers.models_from(result["modelUsage"]),
                         ["claude-haiku-4-5", "claude-opus-5-5"])
        self.assertIsNone(providers.result_event(self.events[:-1]))

    def test_real_json_result(self):
        result = providers.parse_json_result(JSON_RESULT + "\n")
        self.assertEqual(result["result"], "OK")
        self.assertEqual(providers.usage_from(result["usage"]), {
            "input_tokens": 90, "output_tokens": 4, "thinking_tokens": 0,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})
        self.assertEqual(providers.models_from(result["modelUsage"]), ["claude-sonnet-5-5"])
        out = providers.CallResult()
        providers.ClaudeCliProvider._fill(out, result)
        self.assertEqual((out.text, out.cost_usd, out.turns, out.structured),
                         ("OK", 0.00022, 1, None))
        self.assertIsNone(providers.call_failure(0, result, "", need_text=False))
        # The same result inside a JSON array, or as JSON lines, is found too.
        self.assertEqual(providers.parse_json_result(f"[{JSON_RESULT}]")["num_turns"], 1)
        self.assertEqual(providers.parse_json_result(f'{{"type":"x"}}\n{JSON_RESULT}')["result"],
                         "OK")
        self.assertIsNone(providers.parse_json_result(""))
        self.assertEqual(providers.usage_from({"input_tokens": True, "output_tokens": 3.0}),
                         {"input_tokens": None, "output_tokens": 3, "thinking_tokens": None,
                          "cache_read_input_tokens": None, "cache_creation_input_tokens": None})

    def test_tool_calls_paths_and_ok_flags(self):
        calls = providers.tool_calls_from(self.events, base=self.tmp)
        self.assertEqual(calls, [
            {"tool": "Read", "path": f"plugins/p1/skills/{DFA}/reference/plan-template.md",
             "ok": True},
            {"tool": "Read", "path": "w12345678/package.json", "ok": False},
            # never answered (cut off): not a successful read
            {"tool": "Read", "path": f"plugins/p1/skills/{DFA}/reference/validation.md",
             "ok": False},
        ])
        home = Path.home() / "some" / "file.md"
        self.assertEqual(providers.display_path(str(home)), "~/some/file.md")
        self.assertEqual(providers.display_path("relative/x.md", self.tmp), "relative/x.md")
        self.assertEqual(providers.display_path(""), "")

    def test_skills_offered_by_the_init_event(self):
        self.assertEqual(providers.skills_from(self.events),
                         ["compact", DFA, "other-skill"])
        self.assertIsNone(providers.skills_from(self.events[1:]))
        self.assertIsNone(providers.skills_from([{"type": "system", "subtype": "init"}]))

    def test_injected_skill_body_counts_as_loaded_only_when_seen(self):
        found = providers.injected_skill_files(self.events, [self.plugin])
        self.assertEqual(found, [self.plugin / "skills" / DFA / "SKILL.md"])
        self.assertEqual(providers.injected_skill_files(self.events[2:], [self.plugin]), [])
        # The title alone is not the skill: v1.0.0 and v2 share "# Dependency-First Architect".
        title_only = [{"type": "user", "message": {"content": [{"type": "text", "text": (
            "Base directory for this skill: x\n\n# Dependency-First Architect\n\nYou turn a "
            "build request")}]}}]
        self.assertEqual(providers.injected_skill_files(title_only, [self.plugin]), [])

    def test_injected_skill_detection_tells_skill_versions_apart(self):
        v1_plugin, _ = workspace.build_plugin(V1_DIR, DFA, self.tmp / "plugins" / "v1")
        v1_skill, v2_skill = v1_plugin / "skills" / DFA, self.plugin / "skills" / DFA
        self.assertEqual(workspace.read_skill(v1_skill)[1].split("\n")[0],
                         workspace.read_skill(v2_skill)[1].split("\n")[0])  # same first line

        def turn(text):
            return [{"type": "user", "message": {"content": [{"type": "text", "text": text}]}}]

        # A v2 body in the transcript does not count as the v1 SKILL.md, and vice versa.
        self.assertEqual(providers.injected_skill_files(self.events, [v1_plugin]), [])
        self.assertEqual(providers.injected_skill_files(turn(injected(v1_skill)), [v1_plugin]),
                         [v1_skill / "SKILL.md"])
        self.assertEqual(providers.injected_skill_files(turn(injected(v1_skill)), [self.plugin]),
                         [])
        # Whitespace and HTML comments the CLI may change are not part of the comparison.
        _, body = workspace.read_skill(v2_skill)
        loose = providers.HTML_COMMENT.sub("", body).replace("\n", "\r\n  ")
        self.assertEqual(providers.injected_skill_files(turn(loose), [self.plugin]),
                         [v2_skill / "SKILL.md"])
        # A body that was cut short is not a loaded skill.
        self.assertEqual(providers.injected_skill_files(turn(body[: len(body) // 2]),
                                                        [self.plugin]), [])

    def test_failures(self):
        ok = {"type": "result", "is_error": False, "result": "text", "subtype": "success"}
        self.assertIsNone(providers.call_failure(0, ok, ""))
        self.assertIn("exited 1", providers.call_failure(1, ok, "boom"))
        self.assertIn("no result", providers.call_failure(0, None, "stderr text"))
        self.assertIn("reported an error",
                      providers.call_failure(0, dict(ok, is_error=True, result="API Error"), ""))
        self.assertIn("empty result", providers.call_failure(0, dict(ok, result="  "), ""))
        self.assertIsNone(providers.call_failure(0, dict(ok, result=""), "", need_text=False))

    def test_a_failed_exit_keeps_the_clis_own_error_text(self):
        # The usual API failure: an is_error result saying why, exit 1, nothing on stderr.
        overloaded = {"type": "result", "is_error": True, "subtype": "success",
                      "result": "API Error: 529 Overloaded"}
        message = providers.call_failure(1, overloaded, "")
        self.assertIn("exited 1", message)
        self.assertIn("API Error: 529 Overloaded", message)
        both = providers.call_failure(1, overloaded, "connection reset")
        self.assertIn("API Error: 529 Overloaded", both)
        self.assertIn("connection reset", both)
        # Without an error flag, stderr says more than the (successful) result text.
        self.assertIn("boom", providers.call_failure(
            2, {"is_error": False, "subtype": "success", "result": "the plan"}, "boom"))

    def test_first_json_object(self):
        self.assertEqual(providers.first_json_object('Sure:\n```json\n{"a": {"b": 1}}\n```'),
                         {"a": {"b": 1}})
        self.assertEqual(providers.first_json_object("{broken {\"x\": 2}"), {"x": 2})
        self.assertIsNone(providers.first_json_object("no json here"))
        self.assertIsNone(providers.first_json_object(None))


class ModelCheckTest(unittest.TestCase):
    HAIKU = "claude-haiku-4-5-20251001"
    HELPER_AND_MAIN = ["global.anthropic.claude-haiku-4-5-20251001-v1:0",
                       "global.anthropic.claude-opus-5"]

    def test_requested_model_must_appear_in_what_ran(self):
        mismatch = providers.model_mismatch
        self.assertFalse(mismatch("claude-opus-5-5", ["claude-opus-5-5"]))
        self.assertFalse(mismatch("claude-opus-5-5", ["us.anthropic.claude-opus-5-5-v1:0"]))
        self.assertFalse(mismatch("claude-opus-5-5", ["claude-haiku-4-5", "claude-opus-5-5"]))
        # Observed: the CLI ran a different model than requested, silently.
        self.assertTrue(mismatch("claude-haiku-4-5", ["global.anthropic.claude-opus-5"]))
        self.assertTrue(mismatch("claude-opus-5-5", []))
        self.assertFalse(mismatch(None, []))

    def test_the_main_loop_model_decides_not_a_helper_entry(self):
        mismatch = providers.model_mismatch
        # The helper entry matches the request, the main model does not: a substitution.
        self.assertTrue(mismatch(self.HAIKU, self.HELPER_AND_MAIN,
                                 ["global.anthropic.claude-opus-5"]))
        self.assertFalse(mismatch(self.HAIKU, self.HELPER_AND_MAIN,
                                  ["global.anthropic.claude-haiku-4-5-20251001-v1:0"]))
        # Every report of the main model must name the requested one (init event and writer).
        self.assertTrue(mismatch("claude-opus-5-5", ["claude-opus-5"],
                                 ["claude-opus-5-5", "claude-opus-5"]))
        # Exact ids and the 1M-context marker are the requested model.
        self.assertFalse(mismatch("claude-opus-5-5", ["claude-opus-5-5"], ["claude-opus-5-5"]))
        self.assertFalse(mismatch("claude-opus-5-5", ["claude-opus-5-5[1m]"],
                                  ["claude-opus-5-5[1m]", "claude-opus-5-5[1m]"]))

    def test_the_model_that_wrote_the_answer(self):
        usage = {"global.anthropic.claude-haiku-4-5-20251001-v1:0": {"outputTokens": 12},
                 "global.anthropic.claude-opus-5": {"outputTokens": 4100}}
        self.assertEqual(providers.top_model(usage), "global.anthropic.claude-opus-5")
        self.assertIsNone(providers.top_model({"a": {"outputTokens": 5}, "b": {"outputTokens": 5}}))
        self.assertIsNone(providers.top_model({"a": {"inputTokens": 5}}))
        self.assertIsNone(providers.top_model(None))
        self.assertEqual(providers.top_model({"a": {"outputTokens": 1}, "b": {}}), "a")

    def test_the_call_check_uses_the_main_model_reports(self):
        substituted = providers.CallResult(text="plan", models_used=self.HELPER_AND_MAIN,
                                           main_models=["global.anthropic.claude-opus-5"])
        self.assertTrue(generate.call_mismatch(self.HAIKU, substituted))
        self.assertIn("main model ['global.anthropic.claude-opus-5']",
                      generate.mismatch_error(self.HAIKU, substituted))
        failed = providers.CallResult(error="timed out after 5 s")
        self.assertFalse(generate.call_mismatch(self.HAIKU, failed))  # nothing to compare


class ClaudeCliProviderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        stub = self.tmp / "claude_stub.py"
        stub.write_text(STUB_CLAUDE, encoding="utf-8")
        self.spec = {"id": "opus", "provider": "claude-cli", "model": "claude-opus-5-5",
                     "effort": "high",
                     "options": {"executable": [sys.executable, str(stub)], "max_budget_usd": 2}}
        self.provider = providers.make_provider(self.spec, ROOT)
        self.plugin, _ = workspace.build_plugin(ROOT, DFA, self.tmp / "plugins" / "p1")
        self.workdir = self.tmp / "w00000001"
        self.workdir.mkdir()
        self.env = {"STUB_STREAM": str(self.transcript()), "STUB_JSON": JSON_RESULT}

    def transcript(self, name="transcript.jsonl", **fields):
        """Write a stream transcript for the stub to print (UTF-8 bytes, LF) and return it."""
        path = self.tmp / name
        path.write_bytes(stream_transcript(self.plugin, self.workdir, **fields).encode("utf-8"))
        return path

    def seen(self):
        return json.loads((self.workdir / "seen.json").read_text(encoding="utf-8"))

    def child_pid(self):
        pid = int((self.workdir / "child.pid").read_text(encoding="utf-8"))
        self.addCleanup(kill_quietly, pid)  # never leave a sleeper behind, whatever happens
        return pid

    def test_claude_exe_windows_shims(self):
        with mock.patch("shutil.which", return_value=r"C:\npm\claude.CMD"):
            self.assertEqual(providers.claude_exe(), r"C:\npm\claude.CMD")
        with mock.patch("shutil.which", return_value=r"C:\bin\claude.EXE"):
            self.assertEqual(providers.claude_exe(), "claude")
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaises(providers.ProviderError):
                providers.claude_exe()

    def test_command_lines(self):
        self.assertEqual(self.provider.base_args(), [
            "--bare", "-p", "--model", "claude-opus-5-5", "--effort", "high",
            "--no-session-persistence", "--max-budget-usd", "2"])
        self.assertEqual(self.provider.generate_args([Path("a"), Path("b")]), [
            "--tools", "Read", "--output-format", "stream-json", "--verbose",
            "--plugin-dir", "a", "--add-dir", "a", "--plugin-dir", "b", "--add-dir", "b"])
        self.assertEqual(providers.ClaudeCliProvider.judge_args({"type": "object"}), [
            "--tools", "", "--output-format", "json", "--json-schema", '{"type":"object"}'])
        self.assertEqual(providers.ClaudeCliProvider.agent_args(["Read", "Write"], "acceptEdits"),
                         ["--tools", "Read,Write", "--permission-mode", "acceptEdits",
                          "--output-format", "stream-json", "--verbose"])
        self.assertFalse(self.provider.supports_seed)

    def test_generate_through_a_stub_cli(self):
        with mock.patch.dict(os.environ, self.env):
            out = self.provider.generate("/dependency-first-architect Plan X.", self.workdir,
                                         [self.plugin], None, 123, 60)
        self.assertIsNone(out.error)
        seen = self.seen()
        self.assertEqual(seen["prompt"], "/dependency-first-architect Plan X.")  # on stdin
        self.assertEqual(Path(seen["cwd"]).resolve(), self.workdir.resolve())
        self.assertEqual(seen["args"][:9], self.provider.base_args())
        self.assertIn(str(self.plugin), seen["args"])
        self.assertEqual(out.text, "# BUILD PLAN\n\nThe plan.")
        self.assertEqual(out.cost_usd, 0.4321)
        self.assertEqual(out.turns, 4)
        self.assertEqual(out.models_used, ["claude-haiku-4-5", "claude-opus-5-5"])
        self.assertEqual(out.skills_available, ["compact", DFA, "other-skill"])
        self.assertEqual([c["path"] for c in out.tool_calls],
                         [f"plugins/p1/skills/{DFA}/reference/plan-template.md",
                          "w00000001/package.json",
                          f"plugins/p1/skills/{DFA}/reference/validation.md"])
        loaded = sorted(Path(p).resolve() for p in out.loaded_paths)
        skill = (self.plugin / "skills" / DFA).resolve()
        self.assertEqual(loaded, sorted([skill / "reference" / "plan-template.md",
                                         skill / "SKILL.md"]))
        self.assertEqual(out.raw, stream_transcript(self.plugin, self.workdir))
        self.assertGreaterEqual(out.latency_s, 0)

    def test_failed_and_timed_out_calls_are_errors(self):
        with mock.patch.dict(os.environ, dict(self.env, STUB_EXIT="2")):
            out = self.provider.generate("Plan X.", self.workdir, [], None, None, 60)
        self.assertIn("exited 2", out.error)
        with mock.patch.dict(os.environ, dict(self.env, STUB_SLEEP="5")):
            out = self.provider.generate("Plan X.", self.workdir, [], None, None, 1)
        self.assertEqual(out.error, "timed out after 1 s")
        broken = providers.make_provider(dict(self.spec, options={
            "executable": [str(self.tmp / "no-such-cli.exe")]}), ROOT)
        out = broken.generate("x", self.workdir, [], None, None, 5)
        self.assertIn("could not start", out.error)

    def test_judge_through_a_stub_cli(self):
        structured = {"dimensions": [], "note": "n"}
        result = json.loads(JSON_RESULT)
        result["structured_output"] = structured
        with mock.patch.dict(os.environ, dict(self.env, STUB_JSON=json.dumps(result))):
            out = self.provider.judge("Score this.", {"type": "object"}, self.workdir, 5, 60)
        self.assertIsNone(out.error)
        self.assertEqual(out.structured, structured)
        self.assertEqual(out.models_used, ["claude-sonnet-5-5"])
        self.assertIn("--json-schema", self.seen()["args"])
        self.assertEqual(self.seen()["args"][self.seen()["args"].index("--tools") + 1], "")
        with mock.patch.dict(os.environ, self.env):  # structured_output null: no error, no object
            out = self.provider.judge("Score this.", {"type": "object"}, self.workdir, 5, 60)
        self.assertIsNone(out.error)
        self.assertIsNone(out.structured)

    def test_the_prompt_reaches_the_cli_as_exact_utf8_bytes(self):
        # A text-mode pipe would turn every \n into \r\n on Windows: the CLI would get other
        # bytes than the recorded request, and other bytes than on Linux.
        prompt = "Plan X — for Müller.\n\nLength limit: your entire answer must be at most 9 words."
        with mock.patch.dict(os.environ, self.env):
            out = self.provider.generate(prompt, self.workdir, [], None, None, 60)
            self.assertEqual(self.seen()["prompt"], prompt)
            self.provider.judge(prompt + "\n", {"type": "object"}, self.workdir, None, 60)
            self.assertEqual(self.seen()["prompt"], prompt + "\n")
        self.assertIsNone(out.error)

    def test_a_line_separator_in_the_plan_does_not_lose_the_result(self):
        text = "# Plan\n\nPhase 1\u2028Phase 2\u2029Phase 3\u0085done."
        env = dict(self.env, STUB_STREAM=str(self.transcript("sep.jsonl", result_text=text)))
        with mock.patch.dict(os.environ, env):
            out = self.provider.generate("Plan X.", self.workdir, [self.plugin], None, None, 60)
        self.assertIsNone(out.error)
        self.assertEqual(out.text, text)
        self.assertEqual(out.cost_usd, 0.4321)

    def test_the_main_loop_model_is_checked_not_a_helper_entry(self):
        # The requested Haiku is also the CLI's helper model, so a substituted main model still
        # leaves a Haiku entry in modelUsage; the init event and the biggest writer show it.
        haiku = "claude-haiku-4-5-20251001"
        usage = {"global.anthropic.claude-haiku-4-5-20251001-v1:0": {"outputTokens": 10},
                 "global.anthropic.claude-opus-5": {"outputTokens": 900}}
        env = dict(self.env, STUB_STREAM=str(self.transcript(
            "sub.jsonl", init_model="global.anthropic.claude-opus-5", model_usage=usage)))
        haiku_cli = providers.make_provider(dict(self.spec, model=haiku), ROOT)
        with mock.patch.dict(os.environ, env):
            out = haiku_cli.generate("Plan X.", self.workdir, [], None, None, 60)
        self.assertIsNone(out.error)
        self.assertEqual(out.main_models, ["global.anthropic.claude-opus-5"] * 2)
        self.assertTrue(generate.call_mismatch(haiku, out))
        # The init event alone can give it away too (the answer's writer is ambiguous here).
        tied = {"global.anthropic.claude-haiku-4-5-20251001-v1:0": {"outputTokens": 10},
                "global.anthropic.claude-opus-5": {"outputTokens": 10}}
        env["STUB_STREAM"] = str(self.transcript("tied.jsonl", init_model="claude-opus-5",
                                                 model_usage=tied))
        with mock.patch.dict(os.environ, env):
            out = haiku_cli.generate("Plan X.", self.workdir, [], None, None, 60)
        self.assertTrue(generate.call_mismatch(haiku, out))
        # A judge (--output-format json) has no init event: the biggest writer decides.
        result = dict(json.loads(JSON_RESULT), modelUsage=usage)
        with mock.patch.dict(os.environ, dict(self.env, STUB_JSON=json.dumps(result))):
            out = haiku_cli.judge("Score this.", {"type": "object"}, self.workdir, None, 60)
        self.assertEqual(out.main_models, ["global.anthropic.claude-opus-5"])
        self.assertTrue(generate.call_mismatch(haiku, out))
        # The requested model, reported exactly or with the [1m] context marker, passes.
        env["STUB_STREAM"] = str(self.transcript(
            "1m.jsonl", init_model="claude-opus-5-5[1m]",
            model_usage={"claude-opus-5-5[1m]": {"outputTokens": 900},
                         "claude-haiku-4-5": {"outputTokens": 3}}))
        with mock.patch.dict(os.environ, env):
            out = self.provider.generate("Plan X.", self.workdir, [], None, None, 60)
        self.assertFalse(generate.call_mismatch("claude-opus-5-5", out))
        with mock.patch.dict(os.environ, self.env):
            out = self.provider.generate("Plan X.", self.workdir, [], None, None, 60)
        self.assertEqual(out.main_models, ["claude-opus-5-5", "claude-opus-5-5"])
        self.assertFalse(generate.call_mismatch("claude-opus-5-5", out))

    def test_a_timeout_kills_the_whole_process_tree(self):
        # The stub hangs in a child process that holds the pipes, as the real CLI does behind a
        # .cmd shim or a launcher. Killing only the direct child would leave it running, and on
        # Windows the call would wait for it (40 s) instead of timing out.
        started = time.monotonic()
        with mock.patch.dict(os.environ, dict(self.env, STUB_HANG_BEFORE="40")):
            out = self.provider.generate("Plan X.", self.workdir, [], None, None, 4)
        elapsed = time.monotonic() - started
        pid = self.child_pid()
        self.assertEqual(out.error, "timed out after 4 s")
        self.assertLess(elapsed, 25)
        self.assertTrue(gone_within(pid), "the CLI's child process survived the timeout")

    def test_a_complete_result_printed_before_a_timeout_is_kept(self):
        # The session finished and printed its result, then hung on the way out: the result is
        # the answer, and it was paid for, so it is not recorded as a timeout.
        with mock.patch.dict(os.environ, dict(self.env, STUB_HANG_AFTER="40")):
            out = self.provider.generate("/dependency-first-architect Plan X.", self.workdir,
                                         [self.plugin], None, None, 4)
        pid = self.child_pid()
        self.assertIsNone(out.error)
        self.assertEqual((out.text, out.cost_usd), ("# BUILD PLAN\n\nThe plan.", 0.4321))
        self.assertTrue(gone_within(pid))
        result = dict(json.loads(JSON_RESULT), structured_output={"dimensions": [], "note": "n"})
        with mock.patch.dict(os.environ, dict(self.env, STUB_JSON=json.dumps(result),
                                              STUB_HANG_AFTER="40")):
            out = self.provider.judge("Score this.", {"type": "object"}, self.workdir, None, 4)
        self.child_pid()
        self.assertIsNone(out.error)
        self.assertEqual(out.structured, {"dimensions": [], "note": "n"})
        # Without a complete result the timeout stands.
        cut = self.tmp / "cut.jsonl"
        cut.write_bytes(stream_transcript(self.plugin, self.workdir).encode("utf-8")[:-200])
        with mock.patch.dict(os.environ, dict(self.env, STUB_STREAM=str(cut),
                                              STUB_HANG_AFTER="40")):
            out = self.provider.generate("Plan X.", self.workdir, [], None, None, 4)
        self.child_pid()
        self.assertEqual(out.error, "timed out after 4 s")

    @unittest.skipIf(os.name == "nt", "the Windows console delivers Ctrl-C to child processes")
    def test_ctrl_c_is_passed_on_to_the_clis_in_flight(self):
        # POSIX CLIs run in sessions of their own (so a timeout can kill the process group), out
        # of reach of the terminal's SIGINT; the harness passes it on, as it reached them before.
        results = []
        call = threading.Thread(target=lambda: results.append(providers.run_command(
            [sys.executable, "-c", "import time; time.sleep(30)"], "", self.workdir, 60)))
        call.start()
        deadline = time.monotonic() + 10
        while not providers._LIVE and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.5)
        providers.interrupt_running()
        call.join(20)
        self.assertFalse(call.is_alive())
        code, _, _, timed_out = results[0]
        self.assertFalse(timed_out)
        self.assertNotEqual(code, 0)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class OpenAICompatibleTest(unittest.TestCase):
    RESPONSE = {"id": "c1", "model": "gpt-9-2026-01-01", "system_fingerprint": "fp_abc",
                "choices": [{"message": {"role": "assistant", "content": "The plan."},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 200,
                          "completion_tokens_details": {"reasoning_tokens": 50},
                          "prompt_tokens_details": {"cached_tokens": 10}}}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.spec = {"id": "gpt", "provider": "openai-compatible", "model": "gpt-9",
                     "effort": None, "options": {"base_url": "https://llm.example.test/v1/",
                                                 "api_key_env": "DFA_TEST_KEY",
                                                 "price_in_per_mtok": 2.0,
                                                 "price_out_per_mtok": 8.0}}
        self.requests = []

    def urlopen(self, body):
        def fake(request, timeout=None):
            self.requests.append((request, timeout))
            return FakeResponse(json.dumps(body).encode("utf-8"))
        return fake

    def test_generate_request_and_response(self):
        provider = providers.make_provider(self.spec, ROOT)
        plugin, _ = workspace.build_plugin(ROOT, DFA, self.tmp / "plugin")
        system = workspace.render_instructions(plugin / "skills" / DFA)
        with mock.patch.dict(os.environ, {"DFA_TEST_KEY": "sk-secret"}), \
                mock.patch("urllib.request.urlopen", self.urlopen(self.RESPONSE)):
            out = provider.generate("Plan X.", self.tmp, [plugin], system, 42, 30)
        request, timeout = self.requests[0]
        self.assertEqual(request.full_url, "https://llm.example.test/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer sk-secret")
        self.assertEqual(timeout, 30)
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["model"], "gpt-9")
        self.assertEqual(body["seed"], 42)
        self.assertEqual(body["messages"], [{"role": "system", "content": system},
                                            {"role": "user", "content": "Plan X."}])
        self.assertIsNone(out.error)
        self.assertEqual(out.text, "The plan.")
        self.assertEqual(out.usage, {"input_tokens": 1000, "output_tokens": 200,
                                     "thinking_tokens": 50, "cache_read_input_tokens": 10,
                                     "cache_creation_input_tokens": None})
        self.assertAlmostEqual(out.cost_usd, (1000 * 2.0 + 200 * 8.0) / 1e6)
        self.assertEqual(out.models_used, ["gpt-9-2026-01-01"])
        self.assertIn('"system_fingerprint": "fp_abc"', out.raw)
        self.assertNotIn("sk-secret", out.raw + repr(out.__dict__))
        self.assertEqual(len(out.loaded_paths), 5)  # SKILL.md + 4 inlined reference files
        self.assertTrue(provider.supports_seed)

    def test_missing_key(self):
        provider = providers.make_provider(self.spec, ROOT)
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(providers.ProviderError) as caught:
                provider.preflight()
            self.assertIn("DFA_TEST_KEY", str(caught.exception))
            out = provider.generate("x", self.tmp, [], None, None, 5)
        self.assertIn("DFA_TEST_KEY is not set", out.error)

    def test_judge_with_and_without_json_schema_mode(self):
        reply = dict(self.RESPONSE, choices=[{"message": {
            "content": 'Here you go:\n```json\n{"p_methodology": 0.25, "cues": []}\n```'}}])
        provider = providers.make_provider(dict(self.spec, options=dict(self.spec["options"],
                                                                        json_schema=True)), ROOT)
        with mock.patch.dict(os.environ, {"DFA_TEST_KEY": "k"}), \
                mock.patch("urllib.request.urlopen", self.urlopen(reply)):
            out = provider.judge("Probe.", schemas.PROBE_RESPONSE, self.tmp, 1, 30)
            plain = providers.make_provider(self.spec, ROOT)
            plain.judge("Probe.", schemas.PROBE_RESPONSE, self.tmp, 1, 30)
        self.assertEqual(out.structured, {"p_methodology": 0.25, "cues": []})
        strict = json.loads(self.requests[0][0].data.decode("utf-8"))
        self.assertEqual(strict["response_format"], {"type": "json_schema", "json_schema": {
            "name": "judgment", "schema": schemas.PROBE_RESPONSE, "strict": False}})
        loose = json.loads(self.requests[1][0].data.decode("utf-8"))
        self.assertNotIn("response_format", loose)
        self.assertIn("JSON Schema", loose["messages"][0]["content"])
        self.assertIn('"p_methodology"', loose["messages"][0]["content"])

    def test_http_errors_are_reported_not_raised(self):
        def fail(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", {},
                                         io.BytesIO(b'{"error": "rate limited"}'))
        provider = providers.make_provider(self.spec, ROOT)
        with mock.patch.dict(os.environ, {"DFA_TEST_KEY": "k"}), \
                mock.patch("urllib.request.urlopen", fail):
            out = provider.generate("x", self.tmp, [], None, None, 5)
        self.assertIn("HTTP 429", out.error)
        self.assertIn("rate limited", out.raw)

    def test_no_agent_sessions(self):
        with self.assertRaises(NotImplementedError):
            providers.make_provider(self.spec, ROOT).agent("x", self.tmp, ["Read"], "plan", 5)


STUB_AGENT = r'''
import sys
from pathlib import Path
prompt = Path(sys.argv[1]).read_text(encoding="utf-8")
rules = Path("AGENTS.md")
seen = rules.read_text(encoding="utf-8")[:40] if rules.exists() else "none"
text = "plan for: " + prompt + "\nrules: " + seen
if len(sys.argv) > 2:
    Path(sys.argv[2]).write_text(text, encoding="utf-8")
else:
    print(text)
'''


class CommandProviderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.stub = self.tmp / "agent.py"
        self.stub.write_text(STUB_AGENT, encoding="utf-8")
        self.workdir = self.tmp / "w0000000a"
        self.workdir.mkdir()

    def provider(self, with_output=True, **options):
        argv = [sys.executable, str(self.stub), "{prompt_file}"]
        if with_output:
            argv.append("{output_file}")
        spec = {"id": "codex", "provider": "command", "model": "gpt-9",
                "options": dict({"argv": argv, "instructions_path": "AGENTS.md"}, **options)}
        return providers.make_provider(spec, ROOT)

    def test_plain_condition(self):
        out = self.provider().generate("Plan X.", self.workdir, [], None, None, 60)
        self.assertIsNone(out.error)
        self.assertEqual(out.text, "plan for: Plan X.\nrules: none")
        self.assertEqual((self.workdir / "prompt.txt").read_text(encoding="utf-8"), "Plan X.")
        self.assertEqual(out.models_used, ["gpt-9"])
        self.assertIsNone(out.usage)
        self.assertIsNone(out.cost_usd)
        self.assertFalse((self.workdir / "AGENTS.md").exists())

    def test_dfa_condition_installs_the_real_adapter_verbatim(self):
        plugin, _ = workspace.build_plugin(ROOT, DFA, self.tmp / "plugin")
        provider = self.provider(adapter_file="adapters/AGENTS.md")
        out = provider.generate("Plan X.", self.workdir, [plugin], "ignored", None, 60)
        self.assertIsNone(out.error)
        self.assertEqual((self.workdir / "AGENTS.md").read_bytes(),
                         (ROOT / "adapters" / "AGENTS.md").read_bytes())
        self.assertEqual(out.loaded_paths, [ROOT / "adapters" / "AGENTS.md"])
        self.assertIn("rules: <!--", out.text)

    def test_other_skills_get_rendered_instructions(self):
        control = ROOT / "eval" / "conditions" / "generic-architect"
        plugin, _ = workspace.build_plugin(control, "architecture-planner", self.tmp / "plugin")
        out = self.provider(adapter_file="adapters/AGENTS.md").generate(
            "Plan X.", self.workdir, [plugin], None, None, 60)
        self.assertIsNone(out.error)
        written = (self.workdir / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(written, workspace.render_instructions(plugin / "skills" /
                                                                "architecture-planner"))
        self.assertEqual(len(out.loaded_paths), 3)

    def test_stdout_mode_errors_and_judging(self):
        out = self.provider(with_output=False).generate("Plan Y.", self.workdir, [], None, None, 60)
        self.assertEqual(out.text.strip(), "plan for: Plan Y.\nrules: none")
        failing = providers.make_provider({"id": "x", "provider": "command", "options": {
            "argv": [sys.executable, "-c", "import sys; sys.stderr.write('nope'); sys.exit(3)"]}},
            ROOT)
        out = failing.generate("x", self.workdir, [], None, None, 60)
        self.assertIn("exit 3", out.error)
        self.assertIn("nope", out.error)
        self.assertEqual(out.models_used, [])
        reply = 'verdict: {"p_methodology": 0.5, "cues": ["a"]}'
        echo = providers.make_provider({"id": "e", "provider": "command", "options": {
            "argv": [sys.executable, "-c", f"print({reply!r})"]}}, ROOT)
        out = echo.judge("Probe.", schemas.PROBE_RESPONSE, self.workdir, None, 60)
        self.assertEqual(out.structured, {"p_methodology": 0.5, "cues": ["a"]})
        self.assertIn("JSON Schema", (self.workdir / "prompt.txt").read_text(encoding="utf-8"))

    def test_preflight(self):
        with self.assertRaises(providers.ProviderError):
            providers.make_provider({"id": "x", "provider": "command", "options": {
                "argv": ["no-such-agent-binary-xyz"]}}, ROOT).preflight()
        self.provider().preflight()
        with self.assertRaises(providers.ProviderError):
            providers.make_provider({"id": "x", "provider": "nope"}, ROOT)

    def test_a_call_never_reads_an_earlier_calls_output_file(self):
        # An agent that exits 0 without writing {output_file}: the answer left in the directory
        # by an earlier call (another plan's judgment) must not become this call's answer.
        stale = '{"p_methodology": 0.9, "cues": ["the plan of an earlier call"]}'
        (self.workdir / "output.txt").write_text(stale, encoding="utf-8")
        silent = providers.make_provider({"id": "s", "provider": "command", "options": {
            "argv": [sys.executable, "-c", "pass", "{prompt_file}", "{output_file}"]}}, ROOT)
        out = silent.judge("Probe plan B.", schemas.PROBE_RESPONSE, self.workdir, None, 60)
        self.assertIn("without writing its output file", out.error)
        self.assertIsNone(out.structured)
        self.assertFalse((self.workdir / "output.txt").exists())
        out = silent.generate("Plan B.", self.workdir, [], None, None, 60)
        self.assertIn("without writing its output file", out.error)

    def test_the_prompt_reaches_the_agent_as_exact_utf8_bytes(self):
        keep = ("import sys; data = sys.stdin.buffer.read(); open('stdin.bin', 'wb').write(data); "
                "sys.stdout.buffer.write(b'ok\\r\\nM\\xc3\\xbcller\\n')")
        agent = providers.make_provider({"id": "k", "provider": "command", "options": {
            "argv": [sys.executable, "-c", keep]}}, ROOT)
        prompt = "Plan X — für Müller.\n\nLength limit: at most 9 words."
        out = agent.generate(prompt, self.workdir, [], None, None, 60)
        self.assertIsNone(out.error)
        self.assertEqual((self.workdir / "stdin.bin").read_bytes(), prompt.encode("utf-8"))
        self.assertEqual(out.text, "ok\nMüller\n")  # decoded as UTF-8, CRLF read as LF

    def test_windows_cmd_shims_are_started_by_their_full_path(self):
        with mock.patch("shutil.which", return_value=r"C:\npm\codex.CMD"):
            self.assertEqual(providers.resolve_executable("codex"), r"C:\npm\codex.CMD")
        with mock.patch("shutil.which", return_value=r"C:\tools\agent.bat"):
            self.assertEqual(providers.resolve_executable("agent"), r"C:\tools\agent.bat")
        with mock.patch("shutil.which", return_value=r"C:\bin\codex.EXE"):
            self.assertEqual(providers.resolve_executable("codex"), "codex")
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(providers.resolve_executable("missing"), "missing")
        if os.name != "nt":
            return
        # Preflight finds fakeagent.CMD through PATHEXT; CreateProcess only appends .exe, so the
        # bare name used to fail every call with [WinError 2].
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "fakeagent.cmd").write_bytes(
            f'@echo off\r\n"{sys.executable}" "{self.stub}" %*\r\n'.encode("utf-8"))
        agent = providers.make_provider({"id": "f", "provider": "command", "options": {
            "argv": ["fakeagent", "{prompt_file}"]}}, ROOT)
        path = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
        with mock.patch.dict(os.environ, {"PATH": path}):
            agent.preflight()
            out = agent.generate("Plan X.", self.workdir, [], None, None, 60)
        self.assertIsNone(out.error)
        self.assertEqual(out.text.strip(), "plan for: Plan X.\nrules: none")

    def test_the_v1_condition_gets_its_own_instructions_not_the_v2_adapter(self):
        # dfa-v1 packages a skill with the same name as the root skill; the adapter is generated
        # from the root SKILL.md (v2), so installing it would turn the v1 arm into a v2 arm.
        v1_plugin, _ = workspace.build_plugin(V1_DIR, DFA, self.tmp / "v1plugin")
        v1_skill = v1_plugin / "skills" / DFA
        rendered = workspace.render_instructions(v1_skill)
        provider = self.provider(adapter_file="adapters/AGENTS.md")
        out = provider.generate("Plan X.", self.workdir, [v1_plugin], rendered, None, 60)
        self.assertIsNone(out.error)
        written = (self.workdir / "AGENTS.md").read_bytes()
        self.assertEqual(written, rendered.encode("utf-8"))
        self.assertNotEqual(written, (ROOT / "adapters" / "AGENTS.md").read_bytes())
        self.assertEqual(sorted(p.relative_to(v1_plugin).as_posix() for p in out.loaded_paths),
                         sorted(f"skills/{DFA}/{rel}" for rel in
                                ("SKILL.md", "reference/ai-systems.md", "reference/evals.md",
                                 "reference/layer-map.md", "reference/plan-template.md")))
        out = provider.generate("Plan X.", self.workdir, [v1_plugin], None, None, 60)
        self.assertEqual((self.workdir / "AGENTS.md").read_text(encoding="utf-8"), rendered)

    def test_cursor_rules_are_named_per_arm_and_load_like_the_adapter(self):
        adapter = ROOT / "adapters" / "cursor-dependency-first-architect.mdc"
        provider = self.provider(instructions_path=".cursor/rules/{skill}.mdc",
                                 adapter_file="adapters/cursor-dependency-first-architect.mdc")
        dfa_plugin, _ = workspace.build_plugin(ROOT, DFA, self.tmp / "dfa")
        control_plugin, _ = workspace.build_plugin(CONTROL_DIR, "architecture-planner",
                                                   self.tmp / "control")
        rules = self.workdir / ".cursor" / "rules"
        out = provider.generate("Plan X.", self.workdir, [dfa_plugin], "ignored", None, 60)
        self.assertIsNone(out.error)
        self.assertEqual(sorted(p.name for p in rules.iterdir()), [f"{DFA}.mdc"])
        self.assertEqual((rules / f"{DFA}.mdc").read_bytes(), adapter.read_bytes())
        shutil.rmtree(self.workdir / ".cursor")
        control = control_plugin / "skills" / "architecture-planner"
        rendered = workspace.render_instructions(control)
        out = provider.generate("Plan X.", self.workdir, [control_plugin], rendered, None, 60)
        self.assertIsNone(out.error)
        # Named after the control's own skill: nothing in its workspace names the treatment.
        self.assertEqual(sorted(p.name for p in rules.iterdir()), ["architecture-planner.mdc"])
        text = (rules / "architecture-planner.mdc").read_text(encoding="utf-8")
        head, _, rest = text.partition("\n---\n\n")
        lines = head.split("\n")
        adapter_lines = adapter.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")
        self.assertEqual(lines[0], "---")
        self.assertEqual([line.split(":")[0] for line in lines[1:]],
                         [line.split(":")[0] for line in adapter_lines[1:4]])
        self.assertEqual(lines[2:], adapter_lines[2:4])  # globs: / alwaysApply: false
        self.assertEqual(lines[1], "description: " + workspace.yaml_value(
            workspace.skill_description(control)))
        self.assertEqual(rest, rendered)
        with self.assertRaises(providers.ProviderError):
            provider._write_instructions(self.workdir, [], "instructions without a skill")


class FakeProviderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.dfa_plugin, _ = workspace.build_plugin(ROOT, DFA, self.tmp / "plugins" / "dfa")
        control = ROOT / "eval" / "conditions" / "generic-architect"
        self.control_plugin, _ = workspace.build_plugin(control, "architecture-planner",
                                                        self.tmp / "plugins" / "control")
        self.workdir = self.tmp / "w0000000f"
        self.workdir.mkdir()

    def fake(self, **options):
        return providers.make_provider({"id": "f", "provider": "fake", "model": "fake-1",
                                        "options": options}, ROOT)

    def test_generate_is_deterministic_and_depends_on_its_inputs(self):
        fake = self.fake()
        run = lambda prompt, plugins, seed: fake.generate(  # noqa: E731
            prompt, self.workdir, plugins, None, seed, 10).text
        self.assertEqual(run("Plan X.", [], 1), run("Plan X.", [], 1))
        self.assertNotEqual(run("Plan X.", [], 1), run("Plan Y.", [], 1))
        seeds = {run("Plan X.", [self.dfa_plugin], s) for s in range(8)}
        self.assertGreater(len(seeds), 1)
        plain, dfa = run("Plan X.", [], 1), run("/dependency-first-architect Plan X.",
                                                [self.dfa_plugin], 1)
        generic = run("/architecture-planner Plan X.", [self.control_plugin], 1)
        self.assertNotIn("## ", plain)
        self.assertEqual(len([l for l in dfa.splitlines() if l.startswith("## ")]), 10)
        self.assertIn("**What:** Plan X.", dfa)  # the slash command is not part of the request
        self.assertIn("## Milestones", generic)
        self.assertNotIn("Length limit", run("Plan X.\n\nLength limit: your entire answer must "
                                             "be at most 1500 words.", [], 1))

    def test_generate_metadata(self):
        out = self.fake(cost_usd=0.25).generate("/dependency-first-architect Plan X.",
                                                self.workdir, [self.dfa_plugin], None, 3, 10)
        self.assertEqual(out.usage["output_tokens"], len(out.text) // 4)
        self.assertEqual(out.cost_usd, 0.25)
        self.assertEqual(out.models_used, ["fake-1"])
        self.assertEqual(out.skills_available, [DFA])
        self.assertTrue(all(c["ok"] and c["path"].startswith("plugins/dfa/skills/")
                            for c in out.tool_calls))
        self.assertEqual(len(out.loaded_paths), 5)
        self.assertEqual(json.loads(out.raw)["text"], out.text)
        plain = self.fake().generate("Plan X.", self.workdir, [], None, 3, 10)
        self.assertEqual((plain.tool_calls, plain.skills_available, plain.cost_usd),
                         ([], None, 0.0))
        self.assertEqual(providers.make_provider({"id": "f", "provider": "fake"}, ROOT).generate(
            "x", self.workdir, [], None, 1, 10).models_used, ["fake"])

    def test_dfa_plan_passes_every_lint_check(self):
        try:
            from dfa_eval import lint
        except ImportError:
            self.skipTest("eval/dfa_eval/lint.py is not present yet")
        fake = self.fake()
        requests = ["Plan a customer-support RAG chatbot over our help-center docs.",
                    "Plan a command-line tool that renames a folder of photos by their EXIF "
                    "capture date.", "Sequence building a CI/CD platform for 50 microservices."]
        for request in requests:
            for seed in range(5):
                text = fake.generate(f"/{DFA} {request}", self.workdir, [self.dfa_plugin], None,
                                     seed, 10).text
                for kind in (None, "AI", "non-AI"):
                    result = lint.lint_plan(text, kind)
                    failed = [(c["id"], c["detail"]) for c in result["checks"] if not c["ok"]]
                    self.assertEqual(failed, [], (request, seed, kind))
                    self.assertEqual(result["total"], 15 if kind is None else 16)
        plain = fake.generate(requests[0], self.workdir, [], None, 1, 10).text
        self.assertLess(lint.lint_plan(plain, "AI")["score"], 0.5)

    def judge_schema(self, rubric_id, prompt_id):
        rubric = judge.load_rubric(ROOT, rubric_id)
        checklist = None
        if rubric.get("uses_checklists"):
            checklist = json.loads((ROOT / "eval" / "rubrics" / "checklists" /
                                    f"{prompt_id}.json").read_text(encoding="utf-8"))
        return rubric, checklist, schemas.judge_response_schema(rubric, checklist)

    def test_judge_answers_are_valid_and_follow_the_prompt_hash(self):
        fake = self.fake()
        for rubric_id in ("methodology-adherence-v1", "engineering-quality-v1"):
            rubric, checklist, response_schema = self.judge_schema(rubric_id, "P3")
            first = fake.judge("prompt A", response_schema, self.workdir, None, 10)
            self.assertIsNone(first.error)
            self.assertEqual(schema.validate(first.structured, response_schema), [])
            self.assertEqual(judge.validate_response(first.structured, rubric, checklist), [])
            again = fake.judge("prompt A", response_schema, self.workdir, None, 10)
            self.assertEqual(first.structured, again.structured)
            others = [fake.judge(f"prompt {n}", response_schema, self.workdir, None, 10).structured
                      for n in range(5)]
            self.assertTrue(any(o != first.structured for o in others))
        probe = fake.judge("probe", schemas.PROBE_RESPONSE, self.workdir, None, 10).structured
        self.assertEqual(schema.validate(probe, schemas.PROBE_RESPONSE), [])

    def test_malformed_answers_fail_validation_readably(self):
        rubric, checklist, response_schema = self.judge_schema("engineering-quality-v1", "P3")
        fake = self.fake(malformed_on=["broken"])
        good = fake.judge("fine prompt", response_schema, self.workdir, None, 10).structured
        self.assertEqual(judge.validate_response(good, rubric, checklist), [])
        bad = fake.judge("a broken prompt", response_schema, self.workdir, None, 10).structured
        errors = judge.validate_response(bad, rubric, checklist)
        joined = "\n".join(errors)
        self.assertIn("$.dimensions[0].score: 5 is greater than maximum 4", errors)
        self.assertIn("$.dimensions: 10 items, fewer than minItems 11", errors)
        self.assertIn("$.dimensions: missing dim 2, 11", errors)
        self.assertIn("$.dimensions: dim 1 given more than once", errors)
        self.assertIn("$.checklist: unexpected id X-00", errors)
        self.assertIn("$.checklist: missing id P3-01", joined)
        probe = fake.judge("broken probe", schemas.PROBE_RESPONSE, self.workdir, None, 10)
        self.assertEqual(judge.validate_probe(probe.structured),
                         ["$.p_methodology: 1.5 is greater than maximum 1"])
        limited = self.fake(malformed_on=["broken"], malformed_times=1)
        first = limited.judge("broken", response_schema, self.workdir, None, 10).structured
        second = limited.judge("broken", response_schema, self.workdir, None, 10).structured
        self.assertTrue(judge.validate_response(first, rubric, checklist))
        self.assertEqual(judge.validate_response(second, rubric, checklist), [])
        self.assertEqual(judge.validate_response(None, rubric, checklist),
                         ["$: no structured JSON object in the response (got nothing)"])

    def test_fail_on_and_reported_models(self):
        fake = self.fake(fail_on=["EXIF"], models_used=["global.anthropic.claude-opus-5"],
                         cost_usd=0.5)
        out = fake.generate("Rename photos by EXIF date.", self.workdir, [], None, 1, 10)
        self.assertIn("'EXIF'", out.error)
        self.assertIsNone(out.text)
        self.assertEqual(out.cost_usd, 0.5)
        ok = fake.generate("Plan X.", self.workdir, [], None, 1, 10)
        self.assertEqual(ok.models_used, ["global.anthropic.claude-opus-5"])
        self.assertTrue(providers.model_mismatch("fake-1", ok.models_used))
        self.assertIn("fake provider failure",
                      fake.judge("EXIF", schemas.PROBE_RESPONSE, self.workdir, None, 10).error)

    def test_agent_copies_the_configured_files(self):
        source = self.tmp / "src"
        (source / "pkg").mkdir(parents=True)
        (source / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
        (source / "__pycache__").mkdir()
        (source / "__pycache__" / "junk.pyc").write_bytes(b"\0")
        second = self.tmp / "src2"
        (second / "pkg").mkdir(parents=True)
        (second / "pkg" / "mod.py").write_text("x = 2\n", encoding="utf-8")
        # Absolute paths work too (repo_root / absolute is the absolute path); a relative path
        # from the checkout to the temp dir does not exist when they are on different drives.
        relative, relative2 = str(source), str(second)
        out = self.fake(agent_copy_from=relative).agent("Build it.", self.workdir, ["Write"],
                                                        "acceptEdits", 10)
        self.assertIsNone(out.error)
        self.assertEqual((self.workdir / "pkg" / "mod.py").read_text(encoding="utf-8"), "x = 1\n")
        self.assertFalse((self.workdir / "__pycache__").exists())
        self.assertEqual(out.tool_calls, [{"tool": "Write", "path": "pkg/mod.py", "ok": True}])
        rounds = self.fake(agent_copy_from=[relative, relative2])
        sandbox = self.tmp / "sandbox"
        sandbox.mkdir()
        rounds.agent("Round 1.", sandbox, ["Write"], "acceptEdits", 10)
        self.assertEqual((sandbox / "pkg" / "mod.py").read_text(encoding="utf-8"), "x = 1\n")
        rounds.agent("Round 2.", sandbox, ["Write"], "acceptEdits", 10)
        self.assertEqual((sandbox / "pkg" / "mod.py").read_text(encoding="utf-8"), "x = 2\n")
        missing = self.fake(agent_copy_from="no/such/dir").agent("x", sandbox, [], "plan", 10)
        self.assertIn("does not exist", missing.error)


if __name__ == "__main__":
    unittest.main()
