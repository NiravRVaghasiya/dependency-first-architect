"""Model providers: one interface over the Claude Code CLI, OpenAI-compatible APIs, any
command-line agent, and a deterministic offline fake.

A provider only makes a call and reports what happened: text, structured output, usage, cost,
the models it says it used, tool calls, and its raw output, which is preserved verbatim. Deciding
what the call means (model mismatches, schema validation, cost caps, retries) is the harness's
job, so every provider is held to the same rules.

  claude-cli         `claude --bare -p` in an empty directory; skills via --plugin-dir and a
                     slash command; judges with tools disabled and --json-schema.
  openai-compatible  Chat Completions over urllib; skill files inlined as the system prompt.
  command            any agent CLI, e.g. Codex or Cursor's agent. options.argv may use the
                     placeholders {prompt_file} {workdir} {output_file} {model}; the prompt is
                     also sent on stdin. The harness gives every call a directory of its own, and
                     a call whose argv names {output_file} but that exits without writing it is
                     an error (never another call's answer). A skill condition's instructions
                     are written to options.instructions_path inside the workdir first; a
                     "{skill}" in that path becomes the condition's skill name, so each arm's
                     file is named after its own skill. For the repository's own skill (a
                     condition whose SKILL.md is the root SKILL.md, the adapters' source),
                     options.adapter_file is copied verbatim, which tests the real adapter; any
                     other skill (the v1.0.0 copy, the active control) gets its own files
                     rendered, under the same Cursor frontmatter as the adapter when the path
                     ends in .mdc. Examples:
                       {"argv": ["codex", "exec", "--skip-git-repo-check", "-m", "{model}",
                                 "--output-last-message", "{output_file}", "-"],
                        "instructions_path": "AGENTS.md", "adapter_file": "adapters/AGENTS.md"}
                       {"argv": ["cursor-agent", "-p", "--output-format", "text"],
                        "instructions_path": ".cursor/rules/{skill}.mdc",
                        "adapter_file": "adapters/cursor-dependency-first-architect.mdc"}
  fake               offline and deterministic, for tests and CI smoke runs (see FakeProvider).

Every CLI gets its prompt on stdin as UTF-8 bytes (exactly the recorded request, on every OS),
and a call that times out has its whole process tree killed (see run_command). API keys are read
from the environment at call time and never written anywhere.
"""

import hashlib
import http.client
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import records, workspace

USAGE_KEYS = ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_input_tokens",
              "cache_creation_input_tokens")
DFA_SKILL = "dependency-first-architect"
JSON_REPLY = ("\n\nReply with a single JSON object that matches this JSON Schema, and nothing "
              "else:\n{schema}")
KILL_WAIT_S = 30  # how long a killed process tree gets to close its pipes
HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)


class ProviderError(RuntimeError):
    """A provider cannot be used as configured (missing CLI or API key, bad options)."""


class CallResult:
    """What one provider call returned. Every field is optional except `raw` (stdout, or '').

    `loaded_paths` (absolute paths) is not recorded as such: it lists instruction files the
    provider delivered or the session read successfully, which the generation stage matches
    against the packaged skill files to fill `instructions_loaded`. `main_models` is not recorded
    either: it lists the provider's reports of the model that ran the main loop (see
    model_mismatch), while `models_used` lists every model the call touched.
    """

    FIELDS = ("text", "structured", "usage", "cost_usd", "latency_s", "turns", "models_used",
              "main_models", "tool_calls", "skills_available", "raw", "error", "loaded_paths")

    def __init__(self, text=None, structured=None, usage=None, cost_usd=None, latency_s=None,
                 turns=None, models_used=None, tool_calls=None, skills_available=None, raw="",
                 error=None, loaded_paths=None, main_models=None):
        self.text = text
        self.structured = structured
        self.usage = usage
        self.cost_usd = cost_usd
        self.latency_s = latency_s
        self.turns = turns
        self.models_used = list(models_used or [])
        self.main_models = list(main_models or [])
        self.tool_calls = list(tool_calls or [])
        self.skills_available = skills_available
        self.raw = raw if isinstance(raw, str) else ""
        self.error = error
        self.loaded_paths = list(loaded_paths or [])

    def __repr__(self):
        shown = ", ".join(f"{k}={getattr(self, k)!r}" for k in ("error", "models_used", "cost_usd"))
        return f"CallResult({shown})"


def model_matches(requested, reported):
    """True if the reported model id names the requested one: the same id, or one containing it
    (a provider prefix or suffix such as us.anthropic.<id>-v1:0, the [1m] context marker)."""
    return isinstance(requested, str) and isinstance(reported, str) and requested in reported


def model_mismatch(requested, models_used, main_models=None):
    """True unless what the provider reported shows that `requested` ran the call.

    `main_models` are the provider's reports of the model that ran the main loop (Claude CLI: the
    stream-json init event's `model`, and the `modelUsage` entry with the most output tokens).
    When there are any, every one must name the requested model. `models_used` alone cannot
    decide: the CLI also lists small helper-model calls there, so "requested is among the models
    used" passes a substituted main model whenever the requested model is also the helper (a
    Haiku arm). Without main-model reports, the requested id must appear among `models_used`.

    The Claude CLI silently substitutes unknown model ids (observed: `--model claude-haiku-4-5`
    ran global.anthropic.claude-opus-5), so the request is never trusted; a call that reports no
    model at all is a mismatch too. With no requested model there is nothing to check.
    """
    if not requested:
        return False
    if main_models:
        return not all(model_matches(requested, m) for m in main_models)
    return not any(model_matches(requested, m) for m in models_used or [])


def _int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _cost(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value >= 0 and value == value and value != float("inf") else None


def _tail(text, limit=600):
    text = (text or "").strip()
    return text if len(text) <= limit else "..." + text[-limit:]


def _text(data):
    """A child's output as text: UTF-8 whatever the locale (undecodable bytes replaced), with
    CRLF and lone CR read as LF, as a text-mode pipe would."""
    if not data:
        return ""
    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
    return text.replace("\r\n", "\n").replace("\r", "\n")


# --------------------------------------------------------------------------------------------
# Running a CLI
# --------------------------------------------------------------------------------------------

_LIVE = set()  # Popen objects of the CLIs running now (see interrupt_running)
_LIVE_LOCK = threading.Lock()


def resolve_executable(name):
    """argv[0] as subprocess needs it: a Windows .cmd/.bat shim (npm installs, e.g. codex.cmd) by
    its full path, because CreateProcess only appends .exe to a bare name; anything else as given
    (a native launcher may dispatch on the name it was invoked as)."""
    found = shutil.which(name)
    return found if found and found.lower().endswith((".cmd", ".bat")) else name


def kill_tree(proc):
    """Kill `proc` and the processes it started; best effort, never raises.

    Windows: `taskkill /T /F` walks the tree (a .cmd shim runs the CLI under cmd.exe, a launcher
    under itself). POSIX: run_command starts the child as the leader of a new session, so killing
    its process group reaches every descendant that did not leave it.
    """
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=KILL_WAIT_S)
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def run_command(cmd, stdin_text, cwd, timeout_s):
    """Run `cmd` in `cwd` with `stdin_text` on stdin: (exit code or None, stdout, stderr,
    timed_out). Raises OSError if the command cannot start.

    Bytes in, bytes out: the child gets exactly the request as UTF-8 (a text-mode pipe turns
    every newline into CRLF on Windows), and its output is decoded as UTF-8 (see _text). On
    timeout the whole process tree is killed, not just the direct child: a CLI started by a .cmd
    shim or a launcher would keep running and keep the pipes open, so the call would wait for it
    however long it took. Whatever the tree printed before it died is returned.
    """
    options = {} if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen([str(part) for part in cmd], cwd=str(cwd), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
    with _LIVE_LOCK:
        _LIVE.add(proc)
    try:
        out, err = proc.communicate((stdin_text or "").encode("utf-8", errors="replace"),
                                    timeout=timeout_s)
        return proc.returncode, _text(out), _text(err), False
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=KILL_WAIT_S)
        except subprocess.TimeoutExpired as exc:  # a process outside the tree holds the pipes
            out, err = exc.stdout, exc.stderr
        return None, _text(out), _text(err), True
    except BaseException:  # never leave a CLI running behind a failed call
        kill_tree(proc)
        raise
    finally:
        with _LIVE_LOCK:
            _LIVE.discard(proc)


def interrupt_running():
    """Pass a Ctrl-C on to every CLI still running, so their calls end and are recorded.

    On POSIX run_command starts each CLI in a session of its own (so a timeout can kill its whole
    process group), which also keeps the terminal's SIGINT from reaching it; this sends it on.
    On Windows the console has already delivered Ctrl-C to them.
    """
    if os.name == "nt":
        return
    with _LIVE_LOCK:
        live = list(_LIVE)
    for proc in live:
        try:
            os.killpg(proc.pid, signal.SIGINT)
        except OSError:
            pass


def first_json_object(text):
    """The first JSON object embedded in `text` (e.g. inside a ```json fence), or None."""
    if not isinstance(text, str):
        return None
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            value, _ = decoder.raw_decode(text, start)
        except ValueError:
            value = None
        if isinstance(value, dict):
            return value
        start = text.find("{", start + 1)
    return None


# --------------------------------------------------------------------------------------------
# Claude Code CLI output parsing (formats as in examples/scoring/run_eval.py)
# --------------------------------------------------------------------------------------------

def claude_exe():
    if not shutil.which("claude"):
        raise ProviderError("the `claude` CLI is not on PATH (install Claude Code, or set "
                            "options.executable)")
    return resolve_executable("claude")


def claude_version(executable=None):
    """`claude --version` as a bare version string, or "unknown"."""
    try:
        argv = list(executable) if executable else [claude_exe()]
        out = subprocess.run([*argv, "--version"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=60)
    except (OSError, ProviderError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.split("(")[0].replace("claude", "").strip() or "unknown"


def parse_events(stdout):
    """The JSON objects of a stream-json transcript, one per line; other lines are skipped.

    Lines end at "\\n" only: JSON.stringify leaves U+2028, U+2029 and U+0085 unescaped, and
    str.splitlines() would cut a line holding one of them (in model text) into fragments.
    """
    events = []
    for line in (stdout or "").split("\n"):
        try:
            event = json.loads(line.rstrip("\r"))
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def result_event(events):
    """The last `result` event, or None."""
    return next((e for e in reversed(events) if e.get("type") == "result"), None)


UNVERIFIABLE = object()  # structured_from_raw: this provider's raw output does not hold the answer


def structured_from_raw(provider_name, raw):
    """The structured answer a judge or probe call returned, re-parsed from its saved raw output
    the way the provider parsed it: a dict, None if the raw output holds none, or UNVERIFIABLE for
    a provider whose answer is not in the raw file (command: the agent's output file)."""
    if provider_name == "claude-cli":
        result = parse_json_result(raw)
        structured = (result or {}).get("structured_output")
        return structured if isinstance(structured, dict) else None
    if provider_name == "fake":
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        structured = data.get("structured") if isinstance(data, dict) else None
        return structured if isinstance(structured, dict) else None
    if provider_name == "openai-compatible":
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        choices = data.get("choices") if isinstance(data, dict) else None
        message = (choices[0] if isinstance(choices, list) and choices
                   and isinstance(choices[0], dict) else {}).get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        return first_json_object(content) if isinstance(content, str) else None
    return UNVERIFIABLE


def parse_json_result(stdout):
    """The result object of `--output-format json` (one object; tolerate a list or JSON lines)."""
    try:
        data = json.loads(stdout)
    except ValueError:
        return result_event(parse_events(stdout))
    if isinstance(data, list):
        return result_event([e for e in data if isinstance(e, dict)])
    return data if isinstance(data, dict) else None


def usage_from(usage):
    """A CLI `usage` object mapped onto schemas.USAGE (thinking: output_tokens_details)."""
    if not isinstance(usage, dict):
        return None
    details = usage.get("output_tokens_details")
    details = details if isinstance(details, dict) else {}
    return {
        "input_tokens": _int(usage.get("input_tokens")),
        "output_tokens": _int(usage.get("output_tokens")),
        "thinking_tokens": _int(details.get("thinking_tokens")),
        "cache_read_input_tokens": _int(usage.get("cache_read_input_tokens")),
        "cache_creation_input_tokens": _int(usage.get("cache_creation_input_tokens")),
    }


def models_from(model_usage):
    """The models a call actually ran on: the keys of the result's `modelUsage`, sorted."""
    if not isinstance(model_usage, dict):
        return []
    return sorted(k for k in model_usage if isinstance(k, str) and k)


def top_model(model_usage):
    """The `modelUsage` key with the most output tokens (the model that wrote the answer, not a
    helper), or None if no entry reports output tokens or two entries tie for the most."""
    if not isinstance(model_usage, dict):
        return None
    counts = [(key, _int(value.get("outputTokens"))) for key, value in model_usage.items()
              if isinstance(key, str) and key and isinstance(value, dict)]
    counts = [(key, n) for key, n in counts if n is not None]
    if not counts:
        return None
    most = max(n for _, n in counts)
    tops = [key for key, n in counts if n == most]
    return tops[0] if len(tops) == 1 else None


def init_model(events):
    """The model the session's system/init event names (its main-loop model), or None."""
    for event in events:
        if event.get("type") == "system" and event.get("subtype") == "init":
            model = event.get("model")
            return model if isinstance(model, str) and model else None
    return None


def skills_from(events):
    """Skill and slash-command names offered by the session's `system`/`init` event, or None."""
    for event in events:
        if event.get("type") != "system" or event.get("subtype") != "init":
            continue
        names, found = set(), False
        for key in ("skills", "slash_commands"):
            values = event.get(key)
            if not isinstance(values, list):
                continue
            found = True
            for item in values:
                name = item.get("name") if isinstance(item, dict) else item
                if isinstance(name, str) and name:
                    names.add(name)
        return sorted(names) if found else None
    return None


def _blocks(event):
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def tool_uses(events):
    """[(tool name, raw path or "", ok)] for every tool call. ok needs a non-error tool_result:
    a call the session never got an answer for (cut off, timed out) did not succeed."""
    results = {}
    for event in events:
        if event.get("type") == "user":
            for block in _blocks(event):
                if block.get("type") == "tool_result":
                    results[block.get("tool_use_id")] = not block.get("is_error")
    calls = []
    for event in events:
        if event.get("type") != "assistant":
            continue
        for block in _blocks(event):
            if block.get("type") != "tool_use":
                continue
            given = block.get("input") if isinstance(block.get("input"), dict) else {}
            raw = next((given[k] for k in ("file_path", "path", "notebook_path")
                        if isinstance(given.get(k), str)), "")
            calls.append((str(block.get("name") or ""), raw, results.get(block.get("id"), False)))
    return calls


def display_path(raw, base=None):
    """How a tool-call path is recorded: relative to `base` when inside it, `~/...` when under
    the home directory (no user names in committed records), else as given (POSIX)."""
    if not raw:
        return ""
    path = Path(raw)
    if not path.is_absolute():
        return path.as_posix()
    for root, prefix in ((base, ""), (Path.home(), "~/")):
        if root is None:
            continue
        try:
            return prefix + path.resolve().relative_to(Path(root).resolve()).as_posix()
        except (ValueError, OSError):
            continue
    return path.as_posix()


def _inside(raw, roots):
    """True if absolute path `raw` lies inside one of `roots`."""
    path = Path(raw)
    if not path.is_absolute():
        return False
    for root in roots or []:
        try:
            path.resolve().relative_to(Path(root).resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def tool_calls_from(events, base=None):
    """Every tool call a session made: tool, path (see display_path) and whether it succeeded."""
    return [{"tool": tool, "path": display_path(raw, base), "ok": ok}
            for tool, raw, ok in tool_uses(events)]


def _user_texts(events):
    texts = []
    for event in events:
        if event.get("type") != "user":
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            texts.append(content)
        for block in _blocks(event):
            for value in (block.get("text"), block.get("content")):
                if isinstance(value, str):
                    texts.append(value)
                elif isinstance(value, list):
                    texts += [b["text"] for b in value
                              if isinstance(b, dict) and isinstance(b.get("text"), str)]
    return texts


def _squashed(text):
    """`text` with HTML comments dropped and every run of whitespace as one space."""
    return " ".join(HTML_COMMENT.sub(" ", text).split())


def injected_skill_files(events, plugin_dirs):
    """SKILL.md files whose whole body shows up in the transcript's user turns.

    A slash command injects SKILL.md without a tool call, so it only counts as loaded when the
    transcript shows its text, never by assumption. The whole body must appear (compared without
    HTML comments and whitespace differences): a first line or a title is shared by versions of
    one skill (v1.0.0 and v2 both open with "# Dependency-First Architect"), so it cannot tell
    which version the session was given.
    """
    seen = _squashed(" ".join(_user_texts(events)))
    found = []
    for _, skill_dir in workspace.plugin_skills(plugin_dirs):
        _, body = workspace.read_skill(skill_dir)
        needle = _squashed(body)
        if len(needle) >= 8 and needle in seen:
            found.append(skill_dir / "SKILL.md")
    return found


def call_failure(code, result, stderr, need_text=True):
    """Why a CLI call failed, or None if it succeeded."""
    if result is None:
        return f"no result in the CLI output (exit {code}): {_tail(stderr)}"
    detail = f"subtype={result.get('subtype')}, api_error={result.get('api_error_status')}"
    text = result.get("result") if isinstance(result.get("result"), str) else ""
    stderr = stderr or ""
    if code:
        # An API failure (overloaded, rate limit, auth) prints an is_error result whose text says
        # why and exits 1, often with nothing on stderr: keep that text in the error.
        parts = []
        if text.strip() and (result.get("is_error") or not stderr.strip()):
            parts.append(_tail(text, 400))
        if stderr.strip():
            parts.append("stderr: " + _tail(stderr, 400))
        return f"the CLI exited {code} ({detail}): {'; '.join(parts)}"
    if result.get("is_error"):
        return f"the CLI reported an error ({detail}): {_tail(text if text.strip() else stderr)}"
    if need_text and not text.strip():
        return f"empty result text ({detail})"
    return None


def run_failure(error, code, result, stderr, need_text=True):
    """The error of one CLI call: `error` from running it (a timeout, a failed start), else what
    call_failure finds in its output. A session that timed out after printing a complete,
    successful result (it hung on its way out) keeps that result: it is the answer, paid for."""
    if error is None:
        return call_failure(code, result, stderr, need_text)
    if result is not None and call_failure(0, result, stderr, need_text) is None:
        return None
    return error


# --------------------------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------------------------

class Provider:
    """Base class. `spec` is a config generator/judge entry; paths in options are repo-relative."""

    name = "base"
    supports_seed = False
    slash_commands = False  # skill conditions are invoked as `/<skill> <request>`

    def __init__(self, spec, repo_root):
        self.spec = spec
        self.id = spec.get("id")
        self.model = spec.get("model")
        self.effort = spec.get("effort")
        self.options = dict(spec.get("options") or {})
        self.repo_root = Path(repo_root)

    def preflight(self):
        """Raise ProviderError if no call can possibly succeed (checked before any call)."""

    def version(self):
        """Tool version for the run manifest, or None."""
        return None

    def generate(self, prompt, workdir, plugin_dirs, system_append, seed, timeout_s):
        raise NotImplementedError

    def judge(self, prompt, schema, workdir, seed, timeout_s):
        raise NotImplementedError

    def agent(self, prompt, workdir, tools, permission_mode, timeout_s):
        raise NotImplementedError(f"provider {self.name} cannot run agent sessions")


class ClaudeCliProvider(Provider):
    name = "claude-cli"
    slash_commands = True

    def executable(self):
        exe = self.options.get("executable")
        if exe:
            argv = [exe] if isinstance(exe, str) else [str(part) for part in exe]
            return [resolve_executable(argv[0]), *argv[1:]] if argv else argv
        return [claude_exe()]

    def preflight(self):
        self.executable()

    def version(self):
        try:
            return claude_version(self.executable())
        except ProviderError:
            return "unknown"

    def base_args(self):
        args = ["--bare", "-p"]
        if self.model:
            args += ["--model", self.model]
        if self.effort:
            args += ["--effort", self.effort]
        args.append("--no-session-persistence")
        if self.options.get("max_budget_usd") is not None:
            args += ["--max-budget-usd", str(self.options["max_budget_usd"])]
        return args

    def generate_args(self, plugin_dirs, system_append=None):
        args = ["--tools", "Read", "--output-format", "stream-json", "--verbose"]
        for plugin in plugin_dirs or []:
            # --add-dir lets Read reach the skill's files whatever the session's permission mode
            # (Haiku 4.5 runs in `default`, which refused them without it).
            args += ["--plugin-dir", str(plugin), "--add-dir", str(plugin)]
        if system_append:
            args += ["--append-system-prompt", system_append]
        return args

    @staticmethod
    def judge_args(schema):
        return ["--tools", "", "--output-format", "json", "--json-schema",
                json.dumps(schema, separators=(",", ":"))]

    @staticmethod
    def agent_args(tools, permission_mode):
        return ["--tools", ",".join(tools), "--permission-mode", permission_mode,
                "--output-format", "stream-json", "--verbose"]

    def command(self, args):
        return [*self.executable(), *self.base_args(), *args]

    def _run(self, args, prompt, cwd, timeout_s):
        """(exit code or None, stdout, stderr, seconds, error or None); never raises."""
        Path(cwd).mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        try:
            code, stdout, stderr, timed_out = run_command(self.command(args), prompt, cwd,
                                                          timeout_s)
        except (OSError, ProviderError) as exc:
            return None, "", "", time.monotonic() - started, f"could not start the CLI: {exc}"
        error = f"timed out after {timeout_s} s" if timed_out else None
        return code, stdout, stderr, time.monotonic() - started, error

    def _stream(self, run, plugin_dirs, base, need_text):
        code, stdout, stderr, seconds, error = run
        events = parse_events(stdout)
        result = result_event(events)
        out = CallResult(raw=stdout, latency_s=round(seconds, 3))
        out.tool_calls = tool_calls_from(events, base)
        out.skills_available = skills_from(events)
        out.loaded_paths = [Path(raw) for tool, raw, ok in tool_uses(events)
                            if ok and raw and _inside(raw, plugin_dirs)]
        out.loaded_paths += injected_skill_files(events, plugin_dirs)
        if result:
            self._fill(out, result)
        started_on = init_model(events)
        if started_on:  # the main-loop model as the session reported it when it started
            out.main_models.insert(0, started_on)
        out.error = run_failure(error, code, result, stderr, need_text)
        return out

    @staticmethod
    def _fill(out, result):
        out.text = result.get("result") if isinstance(result.get("result"), str) else None
        out.usage = usage_from(result.get("usage"))
        out.cost_usd = _cost(result.get("total_cost_usd"))
        out.turns = _int(result.get("num_turns"))
        out.models_used = models_from(result.get("modelUsage"))
        wrote = top_model(result.get("modelUsage"))
        out.main_models = [wrote] if wrote else []
        structured = result.get("structured_output")
        out.structured = structured if isinstance(structured, dict) else None

    def generate(self, prompt, workdir, plugin_dirs, system_append, seed, timeout_s):
        run = self._run(self.generate_args(plugin_dirs, system_append), prompt, workdir, timeout_s)
        return self._stream(run, plugin_dirs, Path(workdir).parent, need_text=True)

    def judge(self, prompt, schema, workdir, seed, timeout_s):
        code, stdout, stderr, seconds, error = self._run(self.judge_args(schema), prompt, workdir,
                                                         timeout_s)
        result = parse_json_result(stdout)
        out = CallResult(raw=stdout, latency_s=round(seconds, 3))
        if result:
            self._fill(out, result)
        out.error = run_failure(error, code, result, stderr, need_text=False)
        return out

    def agent(self, prompt, workdir, tools, permission_mode, timeout_s):
        run = self._run(self.agent_args(tools, permission_mode), prompt, workdir, timeout_s)
        return self._stream(run, [], workdir, need_text=False)


class OpenAICompatibleProvider(Provider):
    name = "openai-compatible"
    supports_seed = True

    def _key(self):
        env = self.options.get("api_key_env", "OPENAI_API_KEY")
        key = os.environ.get(env)
        if not key:
            raise ProviderError(f"environment variable {env} is not set (options.api_key_env "
                                f"names the variable that holds the API key)")
        return key

    def preflight(self):
        self._key()

    def url(self):
        base = self.options.get("base_url", "https://api.openai.com/v1")
        return base.rstrip("/") + "/chat/completions"

    def body(self, messages, seed=None, extra=None):
        body = {"model": self.model, "messages": messages}
        if seed is not None:
            body["seed"] = seed
        if self.effort:
            body["reasoning_effort"] = self.effort
        body.update(extra or {})
        body.update(self.options.get("params") or {})
        return body

    def _post(self, body, timeout_s):
        """(response text, seconds, error or None); never raises, never echoes the key."""
        started = time.monotonic()
        try:
            request = urllib.request.Request(
                self.url(), data=json.dumps(body).encode("utf-8"), method="POST",
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {self._key()}"})
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                raw = response.read().decode("utf-8", errors="replace")
            return raw, time.monotonic() - started, None
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8", errors="replace")
            except OSError:
                raw = ""
            return raw, time.monotonic() - started, f"HTTP {exc.code}: {_tail(raw, 400)}"
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError,
                ProviderError) as exc:
            return "", time.monotonic() - started, f"request failed: {exc}"

    def complete(self, messages, timeout_s, seed=None, extra=None):
        raw, seconds, error = self._post(self.body(messages, seed, extra), timeout_s)
        out = CallResult(raw=raw, latency_s=round(seconds, 3))
        if error:
            out.error = error
            return out
        try:
            data = json.loads(raw)
        except ValueError:
            out.error = f"the response is not JSON: {_tail(raw, 300)}"
            return out
        if not isinstance(data, dict):
            out.error = "the response is not a JSON object"
            return out
        choices = data.get("choices") if isinstance(data.get("choices"), list) else []
        choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        out.text = message.get("content") if isinstance(message.get("content"), str) else None
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        details_out = usage.get("completion_tokens_details")
        details_in = usage.get("prompt_tokens_details")
        out.usage = {
            "input_tokens": _int(usage.get("prompt_tokens")),
            "output_tokens": _int(usage.get("completion_tokens")),
            "thinking_tokens": _int(details_out.get("reasoning_tokens"))
            if isinstance(details_out, dict) else None,
            "cache_read_input_tokens": _int(details_in.get("cached_tokens"))
            if isinstance(details_in, dict) else None,
            "cache_creation_input_tokens": None,
        }
        price_in = self.options.get("price_in_per_mtok")
        price_out = self.options.get("price_out_per_mtok")
        tokens_in, tokens_out = out.usage["input_tokens"], out.usage["output_tokens"]
        if None not in (price_in, price_out, tokens_in, tokens_out):
            out.cost_usd = round((tokens_in * price_in + tokens_out * price_out) / 1e6, 6)
        out.models_used = [data["model"]] if isinstance(data.get("model"), str) else []
        out.turns = 1
        if not (out.text and out.text.strip()):
            out.error = f"empty reply (finish_reason={choice.get('finish_reason')})"
        return out

    def generate(self, prompt, workdir, plugin_dirs, system_append, seed, timeout_s):
        messages = [{"role": "system", "content": system_append}] if system_append else []
        messages.append({"role": "user", "content": prompt})
        out = self.complete(messages, timeout_s, seed)
        if system_append:  # the inlined skill files reached the model by construction
            out.loaded_paths = _skill_files(plugin_dirs)
        return out

    def judge(self, prompt, schema, workdir, seed, timeout_s):
        if self.options.get("json_schema"):
            extra = {"response_format": {"type": "json_schema", "json_schema": {
                "name": "judgment", "schema": schema, "strict": False}}}
            out = self.complete([{"role": "user", "content": prompt}], timeout_s, seed, extra)
        else:
            text = prompt + JSON_REPLY.format(schema=json.dumps(schema))
            out = self.complete([{"role": "user", "content": text}], timeout_s, seed)
        out.structured = first_json_object(out.text)
        return out

    def agent(self, prompt, workdir, tools, permission_mode, timeout_s):
        raise NotImplementedError("the openai-compatible provider has no tool loop, so it cannot "
                                  "run agent sessions; use claude-cli or a command provider")


def _skill_files(plugin_dirs):
    """Every file packaged in the skills of `plugin_dirs` (SKILL.md and reference files)."""
    files = []
    for _, skill_dir in workspace.plugin_skills(plugin_dirs):
        files += sorted(p for p in skill_dir.rglob("*.md") if p.is_file())
    return files


class CommandProvider(Provider):
    name = "command"

    def argv(self):
        argv = self.options.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise ProviderError(f"command provider {self.id}: options.argv must be a non-empty "
                                "list of strings")
        return argv

    def preflight(self):
        first = self.argv()[0]
        if not (shutil.which(first) or Path(first).is_file()):
            raise ProviderError(f"command provider {self.id}: {first!r} is not on PATH")

    def models(self):
        declared = self.model or self.options.get("model")
        return [declared] if declared else []

    def _is_root_skill(self, skill_dir):
        """True if `skill_dir` packages the repository's own skill: its SKILL.md is the root
        SKILL.md, the source the adapters are generated from. Another version of the same skill
        (the v1.0.0 copy) has the same name but other text, so the adapter is not its own."""
        try:
            return (workspace.read_lf(Path(skill_dir) / "SKILL.md")
                    == workspace.read_lf(self.repo_root / "SKILL.md"))
        except OSError:
            return False

    def _write_instructions(self, workdir, plugin_dirs, system_append):
        """Put a skill condition's instructions where the agent reads them; return their files."""
        skills = workspace.plugin_skills(plugin_dirs)
        if not skills and not system_append:
            return []
        rel = self.options.get("instructions_path")
        if not rel:
            raise ProviderError(f"command provider {self.id}: a skill condition needs "
                                "options.instructions_path (e.g. AGENTS.md)")
        if "{skill}" in rel:  # one file name per arm, e.g. .cursor/rules/{skill}.mdc
            if not skills:
                raise ProviderError(f"options.instructions_path {rel!r} names {{skill}}, but the "
                                    "condition packages no skill")
            rel = rel.replace("{skill}", skills[0][0])
        target = (Path(workdir) / rel).resolve()
        if Path(workdir).resolve() not in target.parents:
            raise ProviderError(f"options.instructions_path {rel!r} must stay inside the workdir")
        target.parent.mkdir(parents=True, exist_ok=True)
        adapter = self.options.get("adapter_file")
        if adapter and len(skills) == 1 and self._is_root_skill(skills[0][1]):
            source = self.repo_root / adapter
            target.write_bytes(source.read_bytes())  # verbatim: this tests the real adapter
            return [source]
        if system_append:
            text = system_append
        else:
            text = "\n".join(workspace.render_instructions(d) for _, d in skills)
        if target.suffix.lower() == ".mdc" and skills:  # loaded the way the adapter rule is
            text = workspace.cursor_rule(text, workspace.skill_description(skills[0][1]))
        target.write_bytes(text.encode("utf-8"))
        return _skill_files(plugin_dirs)

    def _run(self, prompt, workdir, timeout_s):
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        prompt_file, output_file = workdir / "prompt.txt", workdir / "output.txt"
        prompt_file.write_bytes(prompt.encode("utf-8"))
        out = CallResult(models_used=self.models(), turns=None)
        try:
            if output_file.exists():  # never read an earlier call's answer as this one's
                output_file.unlink()
        except OSError as exc:
            out.error = f"could not remove the stale output file {output_file.name}: {exc}"
            return out
        values = {"{prompt_file}": str(prompt_file), "{workdir}": str(workdir),
                  "{output_file}": str(output_file), "{model}": self.model or ""}
        argv = self.argv()
        uses_output = any("{output_file}" in arg for arg in argv)
        cmd = []
        for arg in argv:
            for placeholder, value in values.items():
                arg = arg.replace(placeholder, value)
            cmd.append(arg)
        cmd[0] = resolve_executable(cmd[0])
        started = time.monotonic()
        try:
            code, stdout, stderr, timed_out = run_command(cmd, prompt, workdir, timeout_s)
        except OSError as exc:
            out.error = f"could not start {cmd[0]!r}: {exc}"
        else:
            out.raw = stdout
            if timed_out:
                out.error = f"timed out after {timeout_s} s"
            elif uses_output:  # newlines normalized as stdout's are
                out.text = (_text(output_file.read_bytes()) if output_file.is_file() else None)
            else:
                out.text = stdout
            if out.error is None and code:
                out.error = f"exit {code}: {_tail(stderr)}"
            elif out.error is None and uses_output and out.text is None:
                out.error = (f"the command exited 0 without writing its output file "
                             f"({output_file.name}, the argv's {{output_file}})")
        out.latency_s = round(time.monotonic() - started, 3)
        return out

    def generate(self, prompt, workdir, plugin_dirs, system_append, seed, timeout_s):
        try:
            loaded = self._write_instructions(workdir, plugin_dirs, system_append)
        except (OSError, ProviderError) as exc:
            return CallResult(error=f"could not write the instructions file: {exc}",
                              models_used=self.models())
        out = self._run(prompt, workdir, timeout_s)
        out.loaded_paths = loaded
        if not out.error and not (out.text and out.text.strip()):
            out.error = "the command produced no output"
        return out

    def judge(self, prompt, schema, workdir, seed, timeout_s):
        out = self._run(prompt + JSON_REPLY.format(schema=json.dumps(schema)), workdir, timeout_s)
        out.structured = first_json_object(out.text)
        return out

    def agent(self, prompt, workdir, tools, permission_mode, timeout_s):
        return self._run(prompt, workdir, timeout_s)


class FakeProvider(Provider):
    """Deterministic and offline: the same inputs always give the same outputs, at no cost.

    generate: the text depends on the prompt, the packaged skill names and the seed (which it
      quotes, so two runs never return the same text). No skill: a short prose plan.
      dependency-first-architect: a plan in plan-template v2 shape that passes every lint check.
      Any other skill: a generic sectioned plan. It "reads" the packaged reference files
      (successful Read tool calls).
    judge: an object valid for the given schema, with scores taken from sha256(prompt).
    agent: copies options.agent_copy_from (repo-relative or absolute; a list means the k-th call
      on the same workdir copies the k-th entry, so round 1 and round 2 can differ) into the
      workdir.
    Test hooks in options: fail_on (substrings: the call fails), malformed_on (substrings: the
    judge returns a schema-violating object; malformed_times limits it to the first N such calls),
    cost_usd (cost reported per call, default 0.0), models_used (the models it claims to have
    used, to exercise the model check).
    """

    name = "fake"
    supports_seed = True
    slash_commands = True

    def __init__(self, spec, repo_root):
        super().__init__(spec, repo_root)
        self._lock = threading.Lock()
        self._malformed = {}
        self._agent_calls = {}

    def _models(self):
        return list(self.options.get("models_used") or [self.model or "fake"])

    def _cost(self):
        return float(self.options.get("cost_usd", 0.0))

    def _failure(self, prompt):
        for needle in self.options.get("fail_on") or []:
            if needle in prompt:
                return f"fake provider failure: the prompt contains {needle!r}"
        return None

    def _usage(self, prompt, text):
        return {"input_tokens": len(prompt) // 4, "output_tokens": len(text) // 4,
                "thinking_tokens": None, "cache_read_input_tokens": None,
                "cache_creation_input_tokens": None}

    def _result(self, prompt, text, **fields):
        error = self._failure(prompt)
        raw = records.dumps({"provider": "fake", "model": self._models(), "error": error,
                             "text": None if error else text,
                             "structured": None if error else fields.get("structured")})
        if error:
            return CallResult(error=error, raw=raw, models_used=self._models(),
                              cost_usd=self._cost(), latency_s=0.0)
        return CallResult(text=text, usage=self._usage(prompt, text), cost_usd=self._cost(),
                          latency_s=0.0, models_used=self._models(), raw=raw, **fields)

    def generate(self, prompt, workdir, plugin_dirs, system_append, seed, timeout_s):
        skills = workspace.plugin_skills(plugin_dirs)
        names = [name for name, _ in skills]
        request = prompt
        if prompt.startswith("/") and " " in prompt:
            request = prompt.split(" ", 1)[1]
        text = fake_plan(request, names, seed)
        reads = [p for p in _skill_files(plugin_dirs) if p.name != "SKILL.md"]
        base = Path(workdir).parent
        calls = [{"tool": "Read", "path": display_path(str(p), base), "ok": True} for p in reads]
        return self._result(prompt, text, turns=1 + len(calls), tool_calls=calls,
                            skills_available=names if plugin_dirs else None,
                            loaded_paths=[d / "SKILL.md" for _, d in skills] + reads)

    def judge(self, prompt, schema, workdir, seed, timeout_s):
        stream = _byte_stream(prompt)
        structured = fake_structured(schema, stream)
        if self._is_malformed(prompt):
            structured = malformed(structured, schema)
        return self._result(prompt, json.dumps(structured), structured=structured, turns=1)

    def _is_malformed(self, prompt):
        needles = self.options.get("malformed_on") or []
        if not any(needle in prompt for needle in needles):
            return False
        limit = self.options.get("malformed_times")
        if limit is None:
            return True
        key = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        with self._lock:
            count = self._malformed.get(key, 0)
            self._malformed[key] = count + 1
        return count < limit

    def agent(self, prompt, workdir, tools, permission_mode, timeout_s):
        workdir = Path(workdir)
        source = self.options.get("agent_copy_from")
        if isinstance(source, list):
            with self._lock:
                key = str(workdir.resolve())
                call = self._agent_calls.get(key, 0)
                self._agent_calls[key] = call + 1
            source = source[min(call, len(source) - 1)] if source else None
        copied = []
        if source:
            origin = self.repo_root / source
            if origin.is_dir():
                for path in sorted(origin.rglob("*")):
                    rel = path.relative_to(origin)
                    if path.is_file() and "__pycache__" not in rel.parts:
                        (workdir / rel).parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(path, workdir / rel)
                        copied.append(rel.as_posix())
            elif origin.is_file():
                workdir.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(origin, workdir / origin.name)
                copied.append(origin.name)
            else:
                return CallResult(error=f"options.agent_copy_from {source!r} does not exist",
                                  models_used=self._models(), raw="")
        text = f"Copied {len(copied)} file(s) from {source}." if source else "No changes."
        calls = [{"tool": "Write", "path": rel, "ok": True} for rel in copied]
        return self._result(prompt, text, turns=1 + len(calls), tool_calls=calls)


def _byte_stream(text):
    """An endless, deterministic stream of bytes derived from sha256(text)."""
    counter = 0
    while True:
        for byte in hashlib.sha256(f"{counter}:{text}".encode("utf-8")).digest():
            yield byte
        counter += 1


def fake_structured(schema, stream):
    """A response valid for a judge or probe schema (or a minimal object for any other)."""
    props = schema.get("properties", {})
    if "dimensions" in props:
        dims = props["dimensions"]
        score = dims["items"]["properties"]["score"]
        lo, hi = score["minimum"], score["maximum"]
        out = {"dimensions": [
            {"dim": i, "score": lo + next(stream) % (hi - lo + 1), "evidence": "none",
             "rationale": "Synthetic score from the offline fake provider."}
            for i in range(1, dims["minItems"] + 1)], "note": "Offline fake judgment."}
        if "checklist" in props:
            ids = props["checklist"]["items"]["properties"]["id"]["enum"]
            out["checklist"] = [{"id": i, "status": next(stream) % 3, "evidence": "none"}
                                for i in ids]
        if "traps" in props:
            ids = props["traps"]["items"]["properties"]["id"]["enum"]
            out["traps"] = [{"id": i, "present": next(stream) % 2 == 1, "evidence": "none"}
                            for i in ids]
        return out
    if "p_methodology" in props:
        return {"p_methodology": round(next(stream) / 255, 4),
                "cues": [f"fake cue {k}" for k in range(1, next(stream) % 4 + 1)]}
    filler = {"string": "", "integer": 0, "number": 0, "boolean": False, "array": [],
              "object": {}, "null": None}
    out = {}
    for name in schema.get("required", []):
        kind = props.get(name, {}).get("type", "string")
        out[name] = filler.get(kind if isinstance(kind, str) else kind[0])
    return out


def malformed(structured, schema):
    """A schema-violating copy: score above max, a duplicated and a missing dimension, a wrong
    checklist id; for a probe, a probability above 1."""
    out = json.loads(json.dumps(structured))
    if "dimensions" in out and out["dimensions"]:
        hi = schema["properties"]["dimensions"]["items"]["properties"]["score"]["maximum"]
        dims = out["dimensions"]
        dims[0]["score"] = hi + 1
        if len(dims) > 2:
            dims[1]["dim"] = dims[0]["dim"]
        dims.pop()
        if out.get("checklist"):
            out["checklist"][0]["id"] = "X-00"
        return out
    if "p_methodology" in out:
        out["p_methodology"] = 1.5
        return out
    return {}


# --------------------------------------------------------------------------------------------
# Fake plans
# --------------------------------------------------------------------------------------------

def _pick(seed, options, salt):
    digest = hashlib.sha256(f"{seed}:{salt}".encode("utf-8")).digest()
    return options[digest[0] % len(options)]


def fake_plan(request, skill_names, seed):
    request = request.split("\n\nLength limit:")[0].strip()
    if DFA_SKILL in skill_names:
        return _fake_v2_plan(request, seed)
    if skill_names:
        return _fake_generic_plan(request, seed)
    return _fake_prose_plan(request, seed)


def _fake_prose_plan(request, seed):
    first = _pick(seed, ["Start with the data model and the main write path.",
                         "Start by agreeing on the API contract with the first client.",
                         "Start with a small prototype of the riskiest component."], "p1")
    then = _pick(seed, ["Then add authentication and the main user flows.",
                        "Then build the core features behind a feature flag.",
                        "Then harden the integrations and add monitoring."], "p2")
    return (f"Here is a plan for this request (draft {seed}): {request}\n\n"
            f"{first} {then} Keep the first release small, test it with a few internal users, "
            "and only widen access once errors and latency look healthy. Write down the "
            "assumptions about scale and cost, and revisit them after the first month of real "
            "traffic.\n\nFinally, add the remaining features in order of user value, with "
            "automated tests and a rollback plan for each release.\n")


def _fake_generic_plan(request, seed):
    store = _pick(seed, ["PostgreSQL", "a managed relational database", "SQLite"], "g1")
    return (f"# Architecture and build plan\n\n## Context and goals\n{request} (Draft {seed}.) "
            "The goal is a working first release with as few expensive surprises as possible."
            "\n\n"
            "## Proposed architecture\nOne service with a clear API, backed by "
            f"{store}. Components talk over plain HTTPS.\n\n"
            "## Key decisions\n- Storage: relational, because the data is structured. Revisit "
            "if access patterns turn out to be document-shaped.\n- Deployment: a single "
            "region to start.\n\n## Milestones\n1. A thin end-to-end path in a test "
            "environment.\n2. The core features, behind a flag.\n3. A wider rollout with "
            "monitoring and alerts.\n\n## Risks and open questions\n- Expected load is "
            "unknown; measure it during milestone 2.\n")


def _fake_v2_plan(request, seed):
    latency = _pick(seed, [200, 250, 300, 400], "v1")
    rate = _pick(seed, [20, 50, 100], "v2")
    canary = _pick(seed, ["one internal team", "five percent of traffic", "one pilot customer"],
                   "v3")
    return f"""# BUILD PLAN

## 1. Classification and constraints
- **What:** {request} (draft {seed})
- **Type:** software; greenfield; not a small build.
- **Dominant constraint:** correctness.
- **Worst failure:** a wrong result reaches a real user without anyone noticing.

**Budgets:**

| Budget | Target | Label | Checked by |
|---|---|---|---|
| Latency | p95 under {latency} ms at the expected load | ASSUMPTION (a typical interactive budget) | V1 |
| Throughput | needs the expected peak request rate from the product owner before load testing | UNKNOWN | Phase 2 |
| Availability / SLO | 99.9% monthly | ASSUMPTION (a single-region default) | Phase 2 |
| Operational complexity | one service and one database, run by one team | REQUIREMENT (team size stated by the owner) | Phase 1 |

**Missing inputs:** the expected peak load; the phase order does not depend on it.

## 2. Dependency map

| What waits | Depends on | Kind | Blocked from being |
|---|---|---|---|
| Phase 1 data model | V1 load check | validation | specified |
| Exposure to real users | access control in place and checked (V2) | risk-security | exposed |
| Production deploys | cloud account approval | organizational | deployed |

No economic dependencies.

## 3. Tradeoff gates (resolved up front)

| Decision | Reversibility | Default (chosen now) | Assumption | Validated by | Flip condition |
|---|---|---|---|---|---|
| Data model and identifiers | R3: real data depends on it | relational schema with stable ids | relational fits the main access patterns | V1 | V1 fails, or the reads turn out to be document-shaped |
| Sync vs async processing | R2: a queue can be added behind the API | synchronous requests | requests finish within the latency budget | a load test before Phase 2 | p95 over budget at the expected load |

**R1 defaults:** language → Python; CI → the existing pipeline.
**N/A:** monolith vs services — one small team, so a single service.

## 4. Walking skeleton (Phase 0)
- One real request through the API and the real response.
- Tiers: client → API → database → back.
- Deployed through the pipeline, logged with one trace id, monitored with an alert, rolled back once.
- Who can reach it: internal traffic only, behind a flag.

Exit check (V0): a real request succeeds with one trace across every tier; a deploy and a rollback succeed through the pipeline; an injected failure fires an alert; the first budget values are recorded as baselines.

## 5. Phases
- **Phase 1 — Core data model**
  - Unlocks: every feature that stores data.
  - Depends on: Phase 0 and V0.
  - Tasks: schema and ids first, then the write path, then the read path.
  - Rollback: forward-only migrations with a tested down script.
  - Exit check: V1 passes.
- **Phase 2 — First users**
  - Unlocks: real usage by {canary}.
  - Depends on: Phase 1 and V1.
  - Tasks: access control, then a canary to {canary}, then a wider rollout.
  - Rollback: turn the flag off.
  - Exit check: V2 passes.

## 6. Validation gates

| ID | Hypothesis | Method | Acceptance threshold | Evidence | Unlocks (and if it fails) | Phase |
|---|---|---|---|---|---|---|
| V0 | A change can be deployed, observed, and rolled back through every tier in production | deploy, roll back, and inject a failure | the V0 exit check in section 4 | pipeline log and alert record in the runbook | Phase 1 | 0 |
| V1 | The relational model serves the main reads within budget | load test with production-shaped data | p95 under {latency} ms at {rate} requests per second (ASSUMPTION) | load-test report in the repository | Phase 2; if it fails: revisit the data model | 1 |
| V2 | Access control blocks cross-user reads | automated authorization tests and a security review | zero cross-user reads in the suite, plus the security reviewer's sign-off | test report and review notes | the wider rollout; if it fails: stay internal | 2 |

## 7. Cross-cutting concerns

| Phase | Security | Observability | Reproducibility | Resilience |
|---|---|---|---|---|
| 0 | secrets in a vault, internal-only access | one trace per request and an alert | pinned dependencies and infrastructure as code | a rehearsed rollback |
| 1 | a least-privilege database role | query latency metrics | versioned migrations | backups with a restore test |
| 2 | authorization tests | per-user error rates | seeded test data | a canary with automatic rollback |

## 8. AI layer
N/A — no AI component in the requested system. If one is added later, its prompt-injection defense, cost budget, and human approval come before any capability.

## 9. Methodology exceptions
None.

## 10. Deliberately deferred
- Multi-region failover: pull forward when the availability target needs more than one region.
- A caching layer: pull forward when V1 shows reads over budget.
"""


PROVIDERS = {
    "claude-cli": ClaudeCliProvider,
    "openai-compatible": OpenAICompatibleProvider,
    "command": CommandProvider,
    "fake": FakeProvider,
}


def make_provider(spec, repo_root):
    """The provider for a config generator/judge entry."""
    cls = PROVIDERS.get(spec.get("provider"))
    if cls is None:
        raise ProviderError(f"unknown provider {spec.get('provider')!r}; known: "
                            f"{', '.join(sorted(PROVIDERS))}")
    return cls(spec, repo_root)
