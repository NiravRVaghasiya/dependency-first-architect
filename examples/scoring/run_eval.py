#!/usr/bin/env python3
"""Re-run the with/without-skill eval end to end with the Claude Code CLI.

Every agent is an isolated `claude --bare -p` session started in a fresh, empty directory with a
meaningless name: no CLAUDE.md, hooks, settings or memory, just the prompt. The with-skill arm
also gets the skill, loaded with --plugin-dir and invoked as /dependency-first-architect.
Judges and auditors get the plan inline, with tools disabled, and nothing that says which arm
wrote it (the provenance header and the plan's own self-score section are stripped).

  python examples/scoring/run_eval.py generate  # 5 prompts x 2 arms -> examples/<id>/*.md
  python examples/scoring/run_eval.py judge     # 3 blind judges per plan -> judgments.json
  python examples/scoring/run_eval.py audit     # 3 adversarial auditors per plan -> judgments.json
  python examples/scoring/tally.py              # rebuild the tables in SCORECARD.md

`--prompts P1 P3` re-runs a subset and merges it into the existing files. Judge and audit
verdicts are cached in the temp work dir, so an interrupted run resumes; pass `--fresh` to
ignore the cache (e.g. to measure judge variance).

Standard library only. Needs the `claude` CLI on PATH; --bare authenticates with
ANTHROPIC_API_KEY (or Bedrock/Vertex credentials), not a claude.ai login.
"""

import argparse
import datetime
import hashlib
import json
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"
JUDGMENTS = EXAMPLES / "scoring" / "judgments.json"
GENERATION = EXAMPLES / "scoring" / "generation.json"  # which files each session read
WORK = Path(tempfile.gettempdir()) / "ws"  # neutral: sessions can see their own cwd
MODEL = "claude-opus-5-5"
EFFORT = "max"
JUDGES = 3

# id: (example dir, title, kind, request). Requests are verbatim from reference/evals.md.
PROMPTS = {
    "P1": ("p1-rag-chatbot", "RAG support chatbot", "AI",
           "Plan a customer-support RAG chatbot over our help-center docs."),
    "P2": ("p2-saas-billing", "Multi-tenant SaaS billing", "non-AI",
           "Architect a multi-tenant SaaS billing system."),
    "P3": ("p3-cicd-platform", "CI/CD for 50 microservices", "non-AI",
           "Sequence building a CI/CD platform for 50 microservices."),
    "P4": ("p4-github-issue-agent", "GitHub issue agent", "AI",
           "Plan an autonomous agent that triages and resolves GitHub issues."),
    "P5": ("p5-collab-editor", "Collaborative doc editor", "non-AI",
           "Order the build of a real-time collaborative document editor."),
}
ARMS = ("without", "with")
# Shuffled, meaningless run-directory names, so a session cannot read its arm from its path.
RUN_DIRS = {
    ("P1", "without"): "w07", ("P1", "with"): "w02", ("P2", "without"): "w04",
    ("P2", "with"): "w09", ("P3", "without"): "w10", ("P3", "with"): "w05",
    ("P4", "without"): "w01", ("P4", "with"): "w08", ("P5", "without"): "w03",
    ("P5", "with"): "w06",
}
DIMENSIONS = [
    "Dependency ordering", "Walking skeleton first", "Tradeoff gates up front",
    "Blast-radius ordering", "Security threaded", "Observability from day zero",
    "Reproducibility threaded", "Resilience threaded", "AI layer correctness",
    "Deferred work explicit",
]

RUBRIC = """RUBRIC. Score each dimension 0, 1 or 2 (0 = absent, 1 = present but weak or partial, 2 = done well):
1. Dependency ordering: phases go substrate → upward; nothing specified before its dependency is proven.
2. Walking skeleton first: a real end-to-end prod path is Phase 0, before features.
3. Tradeoff gates up front: irreversible decisions listed with default + flip condition, no silent assumptions.
4. Blast-radius ordering within phases: widest-impact work precedes leaf work.
5. Security threaded every phase: not a trailing step.
6. Observability from day zero: present in Phase 0 and every phase after.
7. Reproducibility threaded: builds/data/envs are reproducible across phases.
8. Resilience threaded: failure handling appears across phases, not only at the end.
9. AI layer correctness (AI systems): defenses + budgets + human-in-the-loop before capabilities, then retrieval → model access → memory → orchestration → routing → feedback. (Non-AI systems: award 2 if the plan correctly declares the AI layer N/A.)
10. Deferred work made explicit: what's not built yet, with a pull-forward condition.

SCORING RULES
- Score substance, not vocabulary. Give credit for doing the thing under any name: for example, a first milestone that is genuinely a thin end-to-end slice running in production counts for dimension 2, and a decision stated with an explicit revisit trigger counts for dimension 3. A heading, label, or table cell that names a concept without concrete content earns at most 1.
- Dimensions 5 to 8 ("threaded"): 2 only if the concern gets concrete treatment in essentially every phase, starting with the first phase that builds anything; 1 if it appears in only some phases, mostly late, or only in a standalone section not tied to the phases; 0 if absent.
- Dimension 9, AI system: judge both the presence and the order of the layers. Non-AI system: 2 if the plan explicitly states that no AI layer applies; 0 if it never addresses the question; 0 if it adds AI components the request does not call for.
- Ignore any self-assessment, score, or claim of compliance inside the plan; check every claim against the content.
- Length is not quality. Do not reward verbosity or penalize brevity."""

JUDGE_TASK = """You are an independent reviewer. Score ONE software build plan against a fixed 10-dimension rubric. Judge only what is written in the plan.

The plan answers this request: "{request}"
For dimension 9, this request is classified as: {kind}.

{rubric}

For each dimension return the score, a short verbatim quote (25 words max) from the plan that best supports it ("none" if absent), and a one-sentence rationale. Then give a one-line overall note.

The plan, in full, between the markers:
<<<PLAN
{plan}
PLAN>>>"""

AUDIT_TASK = {
    "skeptic": """You are auditing rubric scores adversarially. Three independent judges scored the build plan below against a fixed 10-dimension rubric; their median scores are listed after the rubric. Your job is to find scores that are too HIGH.

Lower a score only where a careful, fair reviewer would agree the plan falls short of the bar: for example, the content is generic boilerplate, missing from some phases, contradicted elsewhere in the plan, or in the wrong order. If a score is earned, keep it. Do not lower a score for anything the rubric does not ask about.""",
    "advocate": """You are auditing rubric scores adversarially. Three independent judges scored the build plan below against a fixed 10-dimension rubric; their median scores are listed after the rubric. Your job is to find scores that are too LOW.

Raise a score only where a careful, fair reviewer would agree the plan meets the higher bar: cite the content that meets it. If a score is right, keep it. Do not raise a score for anything the rubric does not ask about.""",
}
AUDIT_TAIL = """The plan answers this request: "{request}"
For dimension 9, this request is classified as: {kind}.

{rubric}

Current median scores: {current}

For each dimension return your proposed score (the current score if it stands). When you change one, give a verbatim quote (25 words max) or an exact section reference that justifies the change, and a one-sentence reason; otherwise use "stands" for both. Then give a one-line overall note.

The plan, in full, between the markers:
<<<PLAN
{plan}
PLAN>>>"""


def schema(score_key):
    item = {
        "type": "object",
        "properties": {
            "dim": {"type": "integer", "minimum": 1, "maximum": 10},
            score_key: {"type": "integer", "minimum": 0, "maximum": 2},
            "evidence": {"type": "string"},
            "rationale": {"type": "string"},
        },
        "required": ["dim", score_key, "evidence", "rationale"],
    }
    return {
        "type": "object",
        "properties": {
            "dimensions": {"type": "array", "minItems": 10, "maxItems": 10, "items": item},
            "note": {"type": "string"},
        },
        "required": ["dimensions", "note"],
    }


HEADER_END = "\n\n---\n\n"
SELF_SCORE = re.compile(r"^#{1,6}\s*(?:\d+\.\s*)?(?:Eval score|Self[- ]?score)\b.*$", re.M | re.I)
LOCK = threading.Lock()


def log(msg):
    with LOCK:
        print(msg, flush=True)


FRESH = False  # set by --fresh: ignore cached verdicts
CLI_VERSION = "unknown"  # set in main(): part of every cache key and of the metadata


def claude_exe():
    found = shutil.which("claude")
    if not found:
        raise SystemExit("ERROR: the `claude` CLI is not on PATH")
    # Windows .cmd/.bat shims (npm installs) need their full path. Native launchers get the
    # plain name, because some dispatch on the name they were invoked as.
    return found if found.lower().endswith((".cmd", ".bat")) else "claude"


def cli_version():
    out = subprocess.run([claude_exe(), "--version"], capture_output=True, text=True)
    return out.stdout.split("(")[0].replace("claude", "").strip() or "unknown"


def claude(args, prompt, cwd, timeout=7200):
    cwd.mkdir(parents=True, exist_ok=True)
    cmd = [claude_exe(), "--bare", "-p", "--model", MODEL, "--effort", EFFORT,
           "--no-session-persistence", *args]
    return subprocess.run(cmd, input=prompt, cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def skill_version():
    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "SKILL.md", "reference"],
                               cwd=ROOT, capture_output=True, text=True).stdout.strip()
        return rev + ("+uncommitted" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def build_plugin():
    """Package SKILL.md + reference/ as a session-only plugin for --plugin-dir."""
    plugin = WORK / "plugin"
    shutil.rmtree(plugin, ignore_errors=True)
    skill = plugin / "skills" / "dependency-first-architect"
    shutil.copytree(ROOT / "reference", skill / "reference")
    shutil.copy2(ROOT / "SKILL.md", skill / "SKILL.md")
    manifest = plugin / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"name": "dependency-first-architect", "version": "0.0.0"}),
                        encoding="utf-8")
    return plugin


def example_path(pid, arm):
    return EXAMPLES / PROMPTS[pid][0] / f"{arm}-skill.md"


def plan_body(pid, arm):
    """The model's output exactly as generated (the committed file minus its provenance header)."""
    text = example_path(pid, arm).read_text(encoding="utf-8")
    return text.split(HEADER_END, 1)[1]


def blind_copy(body):
    """What judges see: the plan without its own self-score section."""
    match = SELF_SCORE.search(body)
    return body[:match.start()].rstrip() + "\n" if match else body


def claimed_self_score(body):
    match = SELF_SCORE.search(body)
    if not match:
        return None
    for n, line in enumerate(body[match.start():].splitlines()):
        found = re.search(r"\b(\d{1,2})\s*/\s*20\b", line)
        # The total sits either in the section heading itself or on a "Total"/"Self-score" line.
        if found and (n == 0 or re.search(r"total|self-score", line, re.I)):
            return int(found.group(1))
    return None


def parse_events(stdout):
    events = []
    for line in stdout.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def session_log(events):
    """Every tool call a session made: tool, path (relative to WORK) and whether it succeeded."""
    failed = {
        block["tool_use_id"]: bool(block.get("is_error"))
        for event in events if event.get("type") == "user"
        for block in (event["message"]["content"] if isinstance(event["message"]["content"], list)
                      else [])
        if block.get("type") == "tool_result"
    }
    calls = []
    for event in events:
        if event.get("type") != "assistant":
            continue
        for block in event["message"]["content"]:
            if block.get("type") != "tool_use":
                continue
            raw = Path(block["input"].get("file_path", ""))
            try:
                path = raw.resolve().relative_to(WORK.resolve()).as_posix()
            except ValueError:
                path = raw.as_posix()
            calls.append({"tool": block["name"], "path": path,
                          "ok": not failed.get(block["id"], False)})
    return calls


def body_hash(body):
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def generate_one(pid, arm, plugin, version, today):
    slug, _, _, request = PROMPTS[pid]
    run = RUN_DIRS[(pid, arm)]
    cwd = WORK / run
    shutil.rmtree(cwd, ignore_errors=True)
    args = ["--tools", "Read", "--output-format", "stream-json", "--verbose"]
    prompt = request
    if arm == "with":
        args += ["--plugin-dir", str(plugin)]
        prompt = f"/dependency-first-architect {request}"
    log(f"generate {pid} {arm}: started in {cwd}")
    proc = claude(args, prompt, cwd)
    (WORK / "transcripts").mkdir(parents=True, exist_ok=True)
    (WORK / "transcripts" / f"{run}.jsonl").write_text(proc.stdout, encoding="utf-8")
    events = parse_events(proc.stdout)
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    if proc.returncode or not result or result.get("is_error") or not result.get("result"):
        raise RuntimeError(f"generate {pid} {arm} failed (exit {proc.returncode}): "
                           f"{proc.stderr[-800:]}")
    tools = session_log(events)
    body = result["result"].strip() + "\n"
    how =("**without the skill** (cold: plain `claude --bare -p`, no skill, empty directory)"
           if arm == "without" else
           f"**with the skill** (`/dependency-first-architect`, skill at commit `{version}`)")
    header = (
        f"> **{pid}** · {how}  \n"
        f"> Prompt: *\"{request}\"*  \n"
        f"> Generated {today} with `{MODEL}` (effort {EFFORT}) in an isolated session. "
        f"One run per arm, not cherry-picked.  \n"
        f"> Everything below the line is the model's output, verbatim. "
        f"Judged score: [SCORECARD.md](../SCORECARD.md)."
    )
    path = example_path(pid, arm)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(f"{header}{HEADER_END}{body}".encode("utf-8"))
    log(f"generate {pid} {arm}: done, {len(tools)} tool calls, turns={result.get('num_turns')}")
    return {"prompt": pid, "arm": arm, "run_dir": run, "turns": result.get("num_turns"),
            "sha256": body_hash(body), "tool_calls": tools}


def structured(task, score_key, cwd, label, attempts=3):
    """One scored response, cached per judge + prompt so an interrupted run resumes for free.

    The key must include the label: the 3 judges of a plan get an identical prompt, and keying on
    the prompt alone would hand a later judge an earlier judge's verdict.
    """
    args = ["--tools", "", "--output-format", "json", "--json-schema", json.dumps(schema(score_key))]
    identity = "|".join([label, MODEL, EFFORT, CLI_VERSION, json.dumps(schema(score_key)), task])
    cache = WORK / "cache" / f"{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:20]}.json"
    if cache.exists() and not FRESH:
        return json.loads(cache.read_text(encoding="utf-8"))
    for attempt in range(1, attempts + 1):
        stdout, why, started = "", "", time.monotonic()
        debug = WORK / "debug" / f"{label.replace(' ', '_').replace('#', '')}-{attempt}.log"
        debug.parent.mkdir(parents=True, exist_ok=True)
        try:
            proc = claude(args + ["--debug-file", str(debug)], task, cwd)
            stdout = proc.stdout
            result = json.loads(stdout)
            out = result.get("structured_output")
            if not out:
                why = (f"no structured output (subtype={result.get('subtype')}, "
                       f"api_error={result.get('api_error_status')}, exit={proc.returncode})")
            else:
                dims = sorted(out["dimensions"], key=lambda d: d["dim"])
                if [d["dim"] for d in dims] == list(range(1, 11)):
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    cache.write_text(json.dumps([dims, out["note"]]), encoding="utf-8")
                    return dims, out["note"]
                why = f"dimension ids {[d['dim'] for d in dims]} are not 1-10"
        except subprocess.TimeoutExpired:
            why = "timed out"
        except ValueError:
            why = f"stdout is not JSON (exit {proc.returncode}): {proc.stderr[-300:]!r}"
        except (KeyError, TypeError) as exc:
            why = f"malformed structured output: {exc!r}"
        failed = WORK / "failures" / f"{label.replace(' ', '_').replace('#', '')}-{attempt}.out"
        failed.parent.mkdir(parents=True, exist_ok=True)
        failed.write_text(stdout, encoding="utf-8")
        log(f"{label}: attempt {attempt} failed after {time.monotonic() - started:.0f}s: {why}")
    raise RuntimeError(f"{label}: no valid structured output after {attempts} attempts")


def judge_one(pid, arm, n):
    _, _, kind, request = PROMPTS[pid]
    task = JUDGE_TASK.format(request=request, kind=f"{kind} system", rubric=RUBRIC,
                             plan=blind_copy(plan_body(pid, arm)))
    label = f"judge {pid} {arm} #{n}"
    dims, note = structured(task, "score", WORK / "j" / f"{RUN_DIRS[(pid, arm)]}{n}", label)
    log(f"{label}: {sum(d['score'] for d in dims)}/20")
    return {
        "judge": n,
        "scores": [d["score"] for d in dims],
        "evidence": [d["evidence"] for d in dims],
        "rationale": [d["rationale"] for d in dims],
        "note": note,
    }


def audit_one(plan, n):
    pid, arm = plan["prompt"], plan["arm"]
    _, _, kind, request = PROMPTS[pid]
    role = "skeptic" if arm == "with" else "advocate"
    medians = [int(statistics.median(s)) for s in zip(*(j["scores"] for j in plan["judges"]))]
    current = ", ".join(f"D{i} {s}" for i, s in enumerate(medians, 1))
    task = AUDIT_TASK[role] + "\n\n" + AUDIT_TAIL.format(
        request=request, kind=f"{kind} system", rubric=RUBRIC, current=current,
        plan=blind_copy(plan_body(pid, arm)))
    label = f"audit {pid} {arm} #{n} ({role})"
    dims, note = structured(task, "proposed", WORK / "a" / f"{RUN_DIRS[(pid, arm)]}{n}", label)
    proposed = [d["proposed"] for d in dims]
    # Directional by design: a skeptic may only lower a score, an advocate only raise it.
    clamp = min if role == "skeptic" else max
    proposed = [clamp(p, m) for p, m in zip(proposed, medians)]
    log(f"{label}: {sum(medians)} -> {sum(proposed)}")
    return {
        "auditor": n,
        "proposed": proposed,
        "evidence": [d["evidence"] for d in dims],
        "reason": [d["rationale"] for d in dims],
        "note": note,
    }


def pool_map(fn, items, jobs):
    """Run fn over items concurrently; if anything fails, report every failure, then exit."""
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = []
        for i, item in enumerate(items):
            if i < jobs:
                time.sleep(3)  # stagger the first wave of session starts
            futures.append(pool.submit(fn, *item))
    failures = [f.exception() for f in futures if f.exception()]
    for exc in failures:
        log(f"FAILED: {exc}")
    if failures:
        raise SystemExit(f"{len(failures)} of {len(items)} sessions failed; re-run to retry")
    return [f.result() for f in futures]


def run_order(record):
    return list(PROMPTS).index(record["prompt"]), ARMS.index(record["arm"])


def cmd_generate(args):
    WORK.mkdir(parents=True, exist_ok=True)
    plugin, version = build_plugin(), skill_version()
    today = datetime.date.today().isoformat()
    jobs = [(pid, arm, plugin, version, today) for pid in args.prompts for arm in ARMS]
    fresh = pool_map(generate_one, jobs, args.jobs)
    # Merge: a subset re-run replaces only its own prompts' records.
    old = json.loads(GENERATION.read_text(encoding="utf-8"))["runs"] if GENERATION.exists() else []
    runs = [r for r in old if r["prompt"] not in args.prompts] + fresh
    data = {"date": today, "model": MODEL, "effort": EFFORT, "claude_cli": CLI_VERSION,
            "skill": version, "runs": sorted(runs, key=run_order)}
    GENERATION.write_bytes((json.dumps(data, indent=2) + "\n").encode("utf-8"))
    log(f"generated {len(fresh)} plans; protocol log: {GENERATION.relative_to(ROOT).as_posix()}")


def cmd_judge(args):
    jobs = [(pid, arm, n) for pid in args.prompts for arm in ARMS for n in range(1, JUDGES + 1)]
    results = dict(zip(jobs, pool_map(judge_one, jobs, args.jobs)))
    plans = []
    for pid in args.prompts:
        slug, title, kind, _ = PROMPTS[pid]
        for arm in ARMS:
            body = plan_body(pid, arm)
            plans.append({
                "prompt": pid, "title": title, "kind": kind, "arm": arm,
                "file": f"{slug}/{arm}-skill.md",
                "self_score": claimed_self_score(body) if arm == "with" else None,
                "judges": [results[(pid, arm, n)] for n in range(1, JUDGES + 1)],
            })
    # Merge: re-judged plans replace their old entries (and drop their now-stale audits).
    old = json.loads(JUDGMENTS.read_text(encoding="utf-8"))["plans"] if JUDGMENTS.exists() else []
    plans = sorted([p for p in old if p["prompt"] not in args.prompts] + plans, key=run_order)
    data = {
        "rubric": "reference/evals.md (10 dimensions, 0-2 each, /20)",
        "date": datetime.date.today().isoformat(),
        "model": f"{MODEL}, effort {EFFORT}, isolated `claude --bare -p` sessions",
        "claude_cli": CLI_VERSION,
        "judges_per_plan": JUDGES,
        "dimensions": DIMENSIONS,
        "plans": plans,
    }
    write_judgments(data)


def cmd_audit(args):
    data = json.loads(JUDGMENTS.read_text(encoding="utf-8"))
    targets = [p for p in data["plans"] if p["prompt"] in args.prompts]
    jobs = [(plan, n) for plan in targets for n in range(1, JUDGES + 1)]
    results = pool_map(audit_one, jobs, args.jobs)
    for i, plan in enumerate(targets):
        plan["audit"] = {
            "role": "skeptic" if plan["arm"] == "with" else "advocate",
            "auditors": results[i * JUDGES:(i + 1) * JUDGES],
        }
    write_judgments(data)


def write_judgments(data):
    JUDGMENTS.write_bytes((json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    log(f"wrote {JUDGMENTS.relative_to(ROOT).as_posix()}")


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("step", choices=["generate", "judge", "audit"])
    parser.add_argument("--prompts", nargs="+", default=list(PROMPTS), choices=list(PROMPTS))
    parser.add_argument("--jobs", type=int, default=10, help="concurrent claude sessions")
    parser.add_argument("--fresh", action="store_true", help="ignore cached judge/audit verdicts")
    args = parser.parse_args(argv)
    global CLI_VERSION, FRESH
    CLI_VERSION, FRESH = cli_version(), args.fresh
    {"generate": cmd_generate, "judge": cmd_judge, "audit": cmd_audit}[args.step](args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
