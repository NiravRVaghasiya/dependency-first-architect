"""Outcome benchmark: plan -> implement -> hidden tests -> change request -> test again.

    python eval/run.py outcomes plan      --config eval/outcomes/outcomes.json --run DIR
    python eval/run.py outcomes implement --config ... --run DIR --allow-code-execution
    python eval/run.py outcomes aggregate --run DIR [--check]
    python eval/run.py outcomes report    --run DIR [--check]

`plan` writes one plan per (plan condition, repetition) with the ordinary generation stage, so
plans get the same records, provenance and model checks as any other generation. `implement`
gives each arm's plan (or none) to a fixed implementer model in a sandbox, runs the task's hidden
tests (eval/outcomes/runner.py, in a child process with a scrubbed environment), then applies the
task's change request and tests again. It executes model-written code, so it refuses to run
without --allow-code-execution; run it in a container or VM.

Records, under the run directory: outcomes/config.json (the outcomes config),
outcomes/task-files.json (the SHA-256 of every task file and of the runner, pinned when the run
starts; a later change makes `implement` and `check` refuse), outcomes/<attempt>.json
(schemas.OUTCOME_ATTEMPT), outcomes/<attempt>/round<N>/ (the code after each round, verbatim),
raw/impl-<attempt>-r<N>.txt (the implementer's raw output), outcomes-summary.json and OUTCOMES.md.

An attempt re-run with --retry-failed is never overwritten: its record, code and raw output move
to superseded/outcomes/<attempt>.<n>.json, superseded/outcomes/<attempt>.<n>/ and
superseded/raw/impl-<attempt>-r<N>.<n>.txt first (n = 1, 2, ...). Superseded attempts count in
the cost cap and in the summary's cost and flags, so a retry cannot hide a failure or its cost.
"""

import argparse
import datetime
import difflib
import json
import os
import shutil
import sys
from pathlib import Path

from . import blinding, records, schema, schemas, stats, workspace
from . import config as configmod
from . import generate, providers

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "eval" / "outcomes" / "runner.py"
OUTCOMES = "outcomes"
SUMMARY = "outcomes-summary.json"
TASK_FILES = "task-files.json"
REPORT = "OUTCOMES.md"
DEFAULT_LIMITS = {"max_cost_usd": None, "jobs": 4, "timeout_s": 3600,
                  "implement_timeout_s": 2400, "test_timeout_s": 120}
# Contrasts reported when both arms exist (treatment, control).
CONTRASTS = [("dfa-plan", "baseline-plan"), ("baseline-plan", "no-plan"), ("dfa-plan", "no-plan"),
             ("generic-plan", "baseline-plan"), ("dfa-plan", "generic-plan")]
SMALL_N = 10


class OutcomeError(RuntimeError):
    pass


def _runner():
    """The runner module, loaded by path (it deliberately imports nothing from the harness)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("dfa_outcome_runner", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------------------------
# Config and task
# --------------------------------------------------------------------------------------------

def load_task(repo_root, task_path):
    task_dir = Path(repo_root) / task_path
    task = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    if task.get("schema") != "dfa-eval/outcome-task@1":
        raise OutcomeError(f"{task_dir / 'task.json'} is not a dfa-eval/outcome-task@1 file")
    return task_dir, task


def load_outcomes_config(path, repo_root=None):
    """Validate an outcomes config (schema, then semantics) and return it with defaults."""
    repo_root = Path(repo_root) if repo_root else ROOT
    data = json.loads(Path(path).read_bytes().decode("utf-8"))
    errors = schema.validate(data, schemas.OUTCOMES_CONFIG)
    if errors:
        raise configmod.ConfigError(Path(path).as_posix(), errors)
    problems = []
    try:
        task_dir, task = load_task(repo_root, data["task"])
    except (OSError, ValueError, OutcomeError) as exc:
        problems.append(f"task: {exc}")
        task = None
    for arm, cond in data["arms"].items():
        records.check_id(arm, "arm")
        if cond is not None and cond not in data["conditions"]:
            problems.append(f"arms.{arm}: condition {cond!r} is not defined under conditions")
    if data["implementer"]["provider"] not in ("claude-cli", "command", "fake"):
        problems.append("implementer: provider must be able to run agent sessions "
                        "(claude-cli, command or fake)")
    if problems:
        raise configmod.ConfigError(Path(path).as_posix(), problems)
    data = dict(data)
    data["limits"] = dict(DEFAULT_LIMITS, **data.get("limits", {}))
    data.setdefault("workspace", {"readme": None})
    if task is not None:
        configmod.check_config(planning_config(data, task, task_dir), repo_root)
    return data


def planning_config(ocfg, task, task_dir):
    """The ordinary benchmark config that generates this experiment's plans."""
    brief = (Path(task_dir) / task["brief"]).read_text(encoding="utf-8").strip()
    used = sorted({c for c in ocfg["arms"].values() if c is not None})
    limits = ocfg["limits"]
    return {
        "schema": "dfa-eval/config@1",
        "name": ocfg["name"],
        "description": f"Plans for the {task['id']} outcome task",
        "seed": ocfg["seed"],
        "prompts": [{"id": task["id"], "title": task["title"], "kind": task.get("kind", "non-AI"),
                     "request": task["planner_request"].format(brief=brief)}],
        "conditions": {cid: ocfg["conditions"][cid] for cid in used},
        "generators": [ocfg["planner"]],
        "runs_per_cell": ocfg["runs_per_arm"],
        "workspace": ocfg.get("workspace", {"readme": None}),
        "limits": {"max_cost_usd": limits.get("max_cost_usd"), "jobs": limits["jobs"],
                   "timeout_s": limits["timeout_s"]},
    }


def attempts(ocfg, task):
    """Every (arm, repetition), interleaved by repetition so a run stopped early (cost cap)
    leaves the arms evenly covered."""
    out = []
    for run_index in range(1, ocfg["runs_per_arm"] + 1):
        for arm in ocfg["arms"]:
            out.append({"attempt_id": records.check_id(f"{task['id']}.{arm}.r{run_index:02d}"),
                        "arm": arm, "run_index": run_index, "plan_condition": ocfg["arms"][arm]})
    return out


# --------------------------------------------------------------------------------------------
# Plan stage
# --------------------------------------------------------------------------------------------

def task_files(repo_root, task_path):
    """The task as run: the SHA-256 (LF-normalized) of every file of the task directory (brief,
    change request, starter, hidden tests, references, mutants) and of the test runner."""
    task_dir = Path(repo_root) / task_path
    files = {}
    for path in sorted(task_dir.rglob("*")):
        rel = path.relative_to(task_dir)
        if path.is_file() and "__pycache__" not in rel.parts:
            files[rel.as_posix()] = file_sha256(path.read_bytes())
    runner = Path(repo_root) / "eval" / "outcomes" / "runner.py"
    return {"task": task_path, "files": files, "runner_sha256": file_sha256(runner.read_bytes())}


def task_changes(run_dir, repo_root=None):
    """What differs between the task files a run recorded and the task as it is now ([] if
    nothing; a one-item list if the run recorded none)."""
    saved = Path(run_dir) / OUTCOMES / TASK_FILES
    if not saved.is_file():
        return [f"{OUTCOMES}/{TASK_FILES} is missing, so the task version these results come "
                "from is unknown"]
    recorded = records.read_json(saved)
    current = task_files(Path(repo_root) if repo_root else ROOT, recorded["task"])
    changed = sorted(rel for rel in set(recorded["files"]) | set(current["files"])
                     if recorded["files"].get(rel) != current["files"].get(rel))
    if recorded["runner_sha256"] != current["runner_sha256"]:
        changed.append("eval/outcomes/runner.py")
    return changed


def pin_task(run, ocfg, repo_root):
    """Record the task files on first use; afterwards refuse to go on if they have changed (a
    task is immutable once a run used it: add a new task id instead of editing one)."""
    saved = run.root / OUTCOMES / TASK_FILES
    if not saved.is_file():
        if load_attempts(run.root):  # attempts already ran against a task nobody recorded
            raise OutcomeError(f"{run.root} has attempts but no {OUTCOMES}/{TASK_FILES}, so the "
                               "task version they ran against is unknown; use a new run directory")
        records.write_json(saved, task_files(repo_root, ocfg["task"]))
        return
    changed = task_changes(run.root, repo_root)
    if changed:
        raise OutcomeError(f"the task changed since this run started ({', '.join(changed)}); "
                           "results from two task versions cannot be mixed: use a new run "
                           "directory, and a new task id if the change is to the tests")


def run_plan(run_dir, ocfg, config_path, jobs=None, max_cost_usd=None, retry_failed=False,
             repo_root=None, work_root=None, log=None):
    repo_root = Path(repo_root) if repo_root else ROOT
    task_dir, task = load_task(repo_root, ocfg["task"])
    cfg = configmod.check_config(planning_config(ocfg, task, task_dir), repo_root)
    run = records.RunDir(run_dir)
    saved = run.root / OUTCOMES / "config.json"
    if saved.is_file() and records.read_json(saved) != ocfg:
        raise OutcomeError(f"{saved} holds a different outcomes config; use a new run directory")
    if (run.root / OUTCOMES / TASK_FILES).is_file():
        pin_task(run, ocfg, repo_root)
    counts = generate.run_generate(run_dir, cfg, config_path, jobs, max_cost_usd,
                                   retry_failed=retry_failed, repo_root=repo_root,
                                   work_root=work_root, log=log)
    records.write_json(saved, ocfg)
    pin_task(run, ocfg, repo_root)
    return counts


def plan_text(run, task_id, condition, planner_id, run_index):
    """(gen_id, plan text with any self-score removed) for an arm's plan, or raise."""
    gid = records.gen_id(task_id, condition, planner_id, run_index)
    path = run.generation_json(gid)
    if not path.is_file():
        raise OutcomeError(f"no plan {gid}: run `outcomes plan` first")
    record = records.read_json(path)
    if record["status"] != "ok":
        raise OutcomeError(f"plan {gid} failed ({record['error']}); retry `outcomes plan`")
    text, _ = blinding.strip_self_score(records.read_text(run.generation_text(gid)))
    return gid, text


# --------------------------------------------------------------------------------------------
# Implement stage
# --------------------------------------------------------------------------------------------

def make_sandbox(task_dir, task, base, name, plan):
    path = Path(base) / name
    if path.exists():
        shutil.rmtree(path)
    shutil.copytree(Path(task_dir) / task["starter"], path,
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copyfile(Path(task_dir) / task["brief"], path / "BRIEF.md")
    if plan is not None:
        records.write_text(path / "PLAN.md", plan)
    return path


def code_files(root, globs):
    files = {}
    for pattern in globs:
        for path in sorted(Path(root).glob(pattern)):
            if path.is_file() and "__pycache__" not in path.parts:
                files[path.relative_to(root).as_posix()] = path.read_bytes()
    return dict(sorted(files.items()))


def file_sha256(data):
    """SHA-256 of a code file with LF line endings: git normalizes text files on commit
    (.gitattributes), so a committed snapshot verifies on every OS."""
    return records.sha256_bytes(data.replace(b"\r\n", b"\n"))


def snapshot(files, dest):
    """Copy `files` ({relative path: bytes}) under dest; return the record's file list."""
    out = []
    for rel, data in files.items():
        target = Path(dest) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        text = data.decode("utf-8", errors="replace")
        out.append({"path": rel, "sha256": file_sha256(data), "lines": len(text.splitlines())})
    return out


def code_sha256(files):
    joined = "".join(f"{rel}\0{file_sha256(data)}\n" for rel, data in files.items())
    return records.sha256_text(joined)


def category_counts(rows):
    """{category: {passed, total}} from per-test rows, as runner.py computes it."""
    by_category = {}
    for row in rows:
        counts = by_category.setdefault(row["category"], {"passed": 0, "total": 0})
        counts["total"] += 1
        counts["passed"] += row["outcome"] == "pass"
    return dict(sorted(by_category.items()))


def diff_stats(before, after):
    """Files changed and lines added/removed between two {path: bytes} snapshots."""
    changed = added = removed = 0
    for rel in sorted(set(before) | set(after)):
        a = before.get(rel, b"").decode("utf-8", errors="replace").splitlines()
        b = after.get(rel, b"").decode("utf-8", errors="replace").splitlines()
        if a == b:
            continue
        changed += 1
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
            if tag in ("replace", "delete"):
                removed += i2 - i1
            if tag in ("replace", "insert"):
                added += j2 - j1
    return {"files_changed": changed, "lines_added": added, "lines_removed": removed}


def round_prompt(task, round_number, has_plan):
    if round_number == 1:
        clause = task["plan_clause"] if has_plan else ""
        return task["implementer_prompt"].format(plan_clause=clause)
    clause = task["change_request_plan_clause"] if has_plan else ""
    return task["change_request_prompt"].format(plan_clause=clause)


def implementer_fields(provider, spec, result, mismatch, raw_file):
    return {"provider": provider.name, "model_requested": spec.get("model"),
            "models_used": list(result.models_used), "model_mismatch": mismatch,
            "usage": result.usage, "cost_usd": result.cost_usd, "latency_s": result.latency_s,
            "raw_file": raw_file, "turns": result.turns}


def run_attempt(run, attempt, ocfg, task_dir, task, provider, work_root, tracker, log):
    spec = ocfg["implementer"]
    options = spec.get("options") or {}
    tools = list(options.get("tools") or ["Read", "Write", "Edit"])
    permission_mode = options.get("permission_mode", "acceptEdits")
    limits = ocfg["limits"]
    runner = _runner()
    plan, plan_gen_id, plan_sha = None, None, None
    if attempt["plan_condition"] is not None:
        plan_gen_id, plan = plan_text(run, task["id"], attempt["plan_condition"],
                                      ocfg["planner"]["id"], attempt["run_index"])
        plan_sha = records.sha256_text(plan)
    salt = f"outcome:{ocfg['seed']}:{run.root.name}"
    sandbox = make_sandbox(task_dir, task, work_root,
                           workspace.neutral_name(attempt["attempt_id"], salt), plan)
    rounds, previous = [], None
    try:
        for number in (1, 2):
            raw_id = f"impl-{attempt['attempt_id']}-r{number}"
            if rounds and rounds[-1]["status"] != "ok":
                rounds.append({"round": number, "status": "skipped",
                               "error": "the previous round did not complete",
                               "implementer": {}, "code_sha256": None, "files": [],
                               "tests": None, "diff_from_previous": None})
                continue
            if number == 2:
                shutil.copyfile(Path(task_dir) / task["change_request"],
                                sandbox / "CHANGE_REQUEST.md")
            log(f"{attempt['attempt_id']}: round {number} implementing")
            result = provider.agent(round_prompt(task, number, plan is not None), sandbox, tools,
                                    permission_mode, limits["implement_timeout_s"])
            tracker.add(result.cost_usd)
            records.write_text(run.raw_path(raw_id), result.raw or "")
            # The main-loop model must be the requested one (not merely some model the CLI used).
            mismatch = generate.call_mismatch(spec.get("model"), result) \
                if spec.get("model") else False
            error = result.error or (generate.mismatch_error(spec.get("model"), result)
                                     if mismatch else None)
            files = code_files(sandbox, task.get("code_globs", ["ledger/**/*.py"]))
            listed = snapshot(files, run.root / OUTCOMES / attempt["attempt_id"] / f"round{number}")
            tests = None
            if error is None:
                tests = runner.run_tests(task_dir, sandbox, number,
                                         limits.get("test_timeout_s") or task.get("test_timeout_s", 120))
                tests = {key: tests.get(key) for key in ("status", "error", "by_category", "tests")}
            rounds.append({
                "round": number, "status": "ok" if error is None else "error", "error": error,
                "implementer": implementer_fields(provider, spec, result, mismatch,
                                                  f"{records.RAW}/{raw_id}.txt"),
                "code_sha256": code_sha256(files) if files else None, "files": listed,
                "tests": tests,
                "diff_from_previous": diff_stats(previous, files) if previous is not None else None,
            })
            previous = files
            passed = sum(t["outcome"] == "pass" for t in (tests or {}).get("tests", []))
            total = len((tests or {}).get("tests", []))
            log(f"{attempt['attempt_id']}: round {number} {rounds[-1]['status']}, "
                f"{passed}/{total} hidden tests pass")
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
    record = {"schema": "dfa-eval/outcome-attempt@1", "attempt_id": attempt["attempt_id"],
              "task": task["id"], "arm": attempt["arm"], "run_index": attempt["run_index"],
              "plan_gen_id": plan_gen_id, "plan_sha256": plan_sha, "rounds": rounds}
    schema.check(record, schemas.OUTCOME_ATTEMPT, f"outcome attempt {attempt['attempt_id']}")
    for item in rounds:
        if item["tests"] is not None:
            schema.check(item["tests"], schemas.TEST_RESULTS, "test results")
    records.write_json(run.root / OUTCOMES / f"{attempt['attempt_id']}.json", record)
    return record


def load_attempts(run_dir):
    folder = Path(run_dir) / OUTCOMES
    if not folder.is_dir():
        return []
    return [records.read_json(p) for p in sorted(folder.glob("*.json"))
            if p.name not in ("config.json", TASK_FILES)]


def load_superseded_attempts(run_dir):
    folder = Path(run_dir) / generate.SUPERSEDED / OUTCOMES
    return [records.read_json(p) for p in sorted(folder.glob("*.json"))] if folder.is_dir() else []


def attempt_cost(record):
    return sum((r.get("implementer") or {}).get("cost_usd") or 0.0 for r in record["rounds"])


def supersede_attempt(run, attempt_id):
    """Move a recorded attempt that is about to be re-run, with its code snapshots and raw
    implementer output, into superseded/ under the next free number (module docstring)."""
    root = Path(run.root)
    records.check_id(attempt_id, "attempt id")
    record_path = root / OUTCOMES / f"{attempt_id}.json"
    snapshots = root / OUTCOMES / attempt_id
    raw_dir = root / records.RAW
    raws = sorted(raw_dir.glob(f"impl-{attempt_id}-r*.txt")) if raw_dir.is_dir() else []
    if not (record_path.is_file() or snapshots.is_dir() or raws):
        return None
    out, out_raw = root / generate.SUPERSEDED / OUTCOMES, root / generate.SUPERSEDED / records.RAW
    n = 1
    while (out / f"{attempt_id}.{n}.json").exists() or (out / f"{attempt_id}.{n}").exists():
        n += 1
    out.mkdir(parents=True, exist_ok=True)
    moved = {}
    for path in raws:
        out_raw.mkdir(parents=True, exist_ok=True)
        target = out_raw / f"{path.stem}.{n}.txt"
        os.replace(path, target)
        moved[f"{records.RAW}/{path.name}"] = f"{generate.SUPERSEDED}/{records.RAW}/{target.name}"
    if snapshots.is_dir():
        os.replace(snapshots, out / f"{attempt_id}.{n}")
    if not record_path.is_file():
        return None
    record = records.read_json(record_path)
    for item in record.get("rounds", []):
        implementer = item.get("implementer") or {}
        if implementer.get("raw_file") in moved:
            implementer["raw_file"] = moved[implementer["raw_file"]]
    records.write_json(out / f"{attempt_id}.{n}.json", record)
    record_path.unlink()
    return record


def attempt_ok(record):
    return len(record["rounds"]) == 2 and all(r["status"] == "ok" for r in record["rounds"])


def run_implement(run_dir, ocfg, allow_code_execution=False, jobs=None, max_cost_usd=None,
                  retry_failed=False, repo_root=None, work_root=None, log=None):
    if not allow_code_execution:
        raise OutcomeError(
            "refusing to run: `implement` executes code written by a model. Run it in a "
            "container or throwaway VM, then pass --allow-code-execution (see SECURITY.md).")
    log = log or generate.default_log
    repo_root = Path(repo_root) if repo_root else ROOT
    run = records.RunDir(run_dir)
    records.check_id(run.root.name, "run directory name")  # it names the work root deleted below
    saved = run.root / OUTCOMES / "config.json"
    if not saved.is_file():
        raise OutcomeError(f"{run.root} has no {OUTCOMES}/config.json: run `outcomes plan` first")
    if records.read_json(saved) != ocfg:
        raise OutcomeError(f"{saved} holds a different outcomes config; implement with the config "
                           "the run was planned with, or use a new run directory")
    task_dir, task = load_task(repo_root, ocfg["task"])
    pin_task(run, ocfg, repo_root)
    done = {r["attempt_id"]: r for r in load_attempts(run_dir)}
    todo = [a for a in attempts(ocfg, task) if a["attempt_id"] not in done
            or (retry_failed and not attempt_ok(done[a["attempt_id"]]))]
    limits = ocfg["limits"]
    cap = max_cost_usd if max_cost_usd is not None else limits["max_cost_usd"]
    spent = generate.run_cost(run) + sum(
        attempt_cost(a) for a in list(done.values()) + load_superseded_attempts(run_dir))
    tracker = generate.CostTracker(cap, spent)
    provider = providers.make_provider(ocfg["implementer"], repo_root)
    provider.preflight()
    base = Path(work_root) if work_root else generate.default_work_root(run.root.name) / "outcomes"
    generate.check_work_root(base, repo_root)
    base.mkdir(parents=True, exist_ok=True)
    counts = {"planned": len(attempts(ocfg, task)), "to_run": len(todo), "ok": 0, "error": 0,
              "skipped": 0}

    def one(attempt):
        if not tracker.allow():  # checked once: an attempt that starts runs both rounds
            log(f"{attempt['attempt_id']}: skipped (cost cap reached)")
            counts["skipped"] += 1
            return
        try:
            # A retry, or leftovers of a crashed attempt: keep them, never overwrite.
            supersede_attempt(run, attempt["attempt_id"])
            record = run_attempt(run, attempt, ocfg, task_dir, task, provider, base, tracker, log)
            counts["ok" if attempt_ok(record) else "error"] += 1
        except OutcomeError as exc:
            log(f"{attempt['attempt_id']}: skipped ({exc})")
            counts["skipped"] += 1

    generate.run_pool(todo, one, jobs or limits["jobs"])
    shutil.rmtree(base, ignore_errors=True)
    return counts


# --------------------------------------------------------------------------------------------
# Aggregate and report
# --------------------------------------------------------------------------------------------

def _rate(by_category, keep=None):
    passed = total = 0
    for category, counts in by_category.items():
        if keep is None or keep(category):
            passed += counts["passed"]
            total += counts["total"]
    return passed / total if total else None


def round_outcome(round_record):
    """'measured' when the round's hidden tests ran to a result (ok, or hung/crashed: the code
    failed), 'refused' when the screen kept the code from running, else 'missing'."""
    if round_record.get("status") != "ok" or not round_record.get("tests"):
        return "missing"
    status = round_record["tests"]["status"]
    return "refused" if status == "refused" else "measured"


def attempt_metrics(record, round1_categories=()):
    """Per-attempt outcome measures. Each round is scored on its own tests, independently of the
    other round: code whose tests hung or crashed passes nothing in that round (0.0), and a round
    that never produced tested code (implementer failure, refused code, skipped) is missing
    (None), not zero."""
    r1, r2 = (record["rounds"] + [{}, {}])[:2]
    s1, s2 = round_outcome(r1), round_outcome(r2)
    out = {"round1_pass_rate": None, "change_request_pass_rate": None,
           "regression_pass_rate": None, "rework_lines": None, "files_changed": None,
           "cost_usd": None, "output_tokens": None, "latency_s": None}
    if s1 == "measured":
        ok1 = r1["tests"]["status"] == "ok"
        c1 = r1["tests"]["by_category"] if ok1 else {}
        out["round1_pass_rate"] = _rate(c1) if ok1 else 0.0
        for category in sorted(set(round1_categories) | set(c1)):
            counts = c1.get(category)
            out[f"r1_{category}"] = (counts["passed"] / counts["total"]
                                     if counts and counts["total"] else 0.0)
    if s2 == "measured":
        ok2 = r2["tests"]["status"] == "ok"
        c2 = r2["tests"]["by_category"] if ok2 else {}
        out["change_request_pass_rate"] = (_rate(c2, lambda c: c == "change-request")
                                           if ok2 else 0.0)
        out["regression_pass_rate"] = _rate(c2, lambda c: c != "change-request") if ok2 else 0.0
    diff = r2.get("diff_from_previous")
    if diff and r1.get("status") == "ok" and r2.get("status") == "ok":
        out["rework_lines"] = diff["lines_added"] + diff["lines_removed"]
        out["files_changed"] = diff["files_changed"]
    ran = [r for r in record["rounds"] if r.get("implementer")]
    costs = [r["implementer"].get("cost_usd") for r in ran]
    out["cost_usd"] = sum(costs) if ran and all(c is not None for c in costs) else None
    tokens = [(r["implementer"].get("usage") or {}).get("output_tokens") for r in ran]
    out["output_tokens"] = sum(tokens) if ran and all(x is not None for x in tokens) else None
    latencies = [r["implementer"].get("latency_s") for r in ran]
    out["latency_s"] = sum(latencies) if ran and all(x is not None for x in latencies) else None
    return out


def _session_flags(run, attempt_records):
    """Output style and permission mode of the planner and implementer sessions, from the init
    event of each claude-cli transcript (user settings still apply under --bare)."""
    from .aggregate import init_event
    sessions = {"planner": [], "implementer": []}
    for gen in run.generations():
        if gen.get("provider") == "claude-cli" and gen.get("raw_file"):
            sessions["planner"].append(run.root / gen["raw_file"])
    for record in attempt_records:
        for item in record["rounds"]:
            implementer = item.get("implementer") or {}
            if implementer.get("provider") == "claude-cli" and implementer.get("raw_file"):
                sessions["implementer"].append(run.root / implementer["raw_file"])
    flags = []
    for role, paths in sessions.items():
        seen = {}
        for path in paths:
            init = init_event(path)
            key = (init.get("output_style") or "unknown", init.get("permissionMode") or "unknown")
            seen[key] = seen.get(key, 0) + 1
        if seen and set(style for style, _ in seen) != {"default"}:
            flags.append(f"{role} sessions ran with " + ", ".join(
                f"output style {style!r} and permission mode {mode} ({n})"
                for (style, mode), n in sorted(seen.items()))
                + " (the operator's Claude Code settings apply under --bare)")
    return flags


def aggregate(run_dir):
    run = records.RunDir(run_dir)
    ocfg = records.read_json(run.root / OUTCOMES / "config.json")
    records_ = load_attempts(run_dir)
    ok = [r for r in records_ if attempt_ok(r)]
    excluded = sorted(r["attempt_id"] for r in records_ if not attempt_ok(r))
    try:
        _, task = load_task(ROOT, ocfg["task"])
        round1_categories = sorted({task["categories"][m] for m in task["round1_modules"]})
        planned = attempts(ocfg, task)
    except (OSError, ValueError, KeyError, OutcomeError):
        round1_categories, planned = [], []
    per_arm = {}
    for record in records_:  # every attempt counts in each round it measured
        per_arm.setdefault(record["arm"], []).append(attempt_metrics(record, round1_categories))
    incomplete = sorted(f"{r['attempt_id']} round {rnd['round']} ({rnd['tests']['status']})"
                        for r in records_ for rnd in r["rounds"]
                        if rnd.get("tests") and rnd["tests"]["status"] not in ("ok", "refused"))
    refused = sorted(f"{r['attempt_id']} round {rnd['round']}" for r in records_
                     for rnd in r["rounds"] if round_outcome(rnd) == "refused")
    recorded_ids = {r["attempt_id"] for r in records_}
    unrecorded = sorted(a["attempt_id"] for a in planned if a["attempt_id"] not in recorded_ids)
    superseded = load_superseded_attempts(run_dir)
    retried = {}
    for record in superseded:
        retried[record["attempt_id"]] = retried.get(record["attempt_id"], 0) + 1
    metric_names = sorted({k for rows in per_arm.values() for row in rows for k in row})
    failed_tests = {}  # round -> test id -> arm -> attempts whose code failed it
    for record in records_:
        for item in record["rounds"]:
            if round_outcome(item) != "measured" or item["tests"]["status"] != "ok":
                continue
            for row in item["tests"]["tests"]:
                if row["outcome"] != "pass":
                    by_arm = failed_tests.setdefault(str(item["round"]), {}).setdefault(
                        row["id"], {})
                    by_arm[record["arm"]] = by_arm.get(record["arm"], 0) + 1
    failed_tests = {rnd: dict(sorted(tests.items())) for rnd, tests in sorted(failed_tests.items())}
    arms = []
    for arm in ocfg["arms"]:
        rows = per_arm.get(arm, [])
        arms.append({"arm": arm, "plan_condition": ocfg["arms"][arm],
                     "n_planned": sum(1 for a in planned if a["arm"] == arm),
                     "n": len(rows),
                     "n_superseded": sum(1 for r in superseded if r["arm"] == arm),
                     "metrics": {m: stats.describe([row.get(m) for row in rows])
                                 for m in metric_names}})
    contrasts = []
    for index, (t_arm, c_arm) in enumerate(CONTRASTS):
        if t_arm not in per_arm or c_arm not in per_arm:
            continue
        for metric in ("round1_pass_rate", "change_request_pass_rate", "regression_pass_rate",
                       "rework_lines", "cost_usd"):
            t = [row[metric] for row in per_arm[t_arm] if row.get(metric) is not None]
            c = [row[metric] for row in per_arm[c_arm] if row.get(metric) is not None]
            seed = configmod.derive_seed(ocfg["seed"], f"outcome:{t_arm}:{c_arm}:{metric}")
            mt, mc = stats.mean(t), stats.mean(c)
            notes = []
            if min(len(t), len(c)) < SMALL_N:
                notes.append(f"n < {SMALL_N} attempts per arm: descriptive only")
            contrasts.append({
                "treatment": t_arm, "control": c_arm, "metric": metric, "n_treatment": len(t),
                "n_control": len(c),
                "mean_diff": None if mt is None or mc is None else mt - mc,
                "bootstrap_ci95": stats.bootstrap_diff_ci(t, c, seed=seed),
                "cliffs_delta": stats.cliffs_delta(t, c), "notes": notes})
    flags = [f"incomplete attempt {a}: a round failed or was skipped; only its measured rounds "
             "count" for a in excluded]
    flags += [f"counted as passing nothing: {item}: its hidden tests did not complete"
              for item in incomplete]
    flags += [f"not counted: {item}: the screen refused to run its code" for item in refused]
    if unrecorded:
        flags.append(f"{len(unrecorded)} of {len(planned)} planned attempts have no record "
                     f"(cost cap or missing plan): {', '.join(unrecorded)}")
    flags += [f"re-run attempt {attempt_id}: {n} earlier record(s) kept in "
              f"{generate.SUPERSEDED}/{OUTCOMES}/" for attempt_id, n in sorted(retried.items())]
    flags += _session_flags(run, records_)
    implementer_usd = sum((attempt_cost(r) for r in records_), 0.0)
    superseded_usd = sum((attempt_cost(r) for r in superseded), 0.0)
    planning_usd = generate.run_cost(run)
    cost = {"planning_usd": planning_usd, "implementer_usd": implementer_usd,
            "superseded_implementer_usd": superseded_usd,
            "total_usd": planning_usd + implementer_usd + superseded_usd}
    summary = {"schema": "dfa-eval/outcomes-summary@1", "run_id": run.root.name,
               "task": ocfg["task"], "config_name": ocfg["name"],
               "implementer": {k: ocfg["implementer"].get(k) for k in ("id", "model", "effort")},
               "planner": {k: ocfg["planner"].get(k) for k in ("id", "model", "effort")},
               "attempts": {"planned": len(planned), "recorded": len(records_), "ok": len(ok),
                            "excluded": excluded, "superseded": len(superseded)},
               "arms": arms, "contrasts": contrasts, "failed_tests": failed_tests, "cost": cost,
               "flags": flags}
    return stats.rounded(summary)


def _fmt(value, digits=2):
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _dist(stat, digits=2):
    if not stat or stat["n"] == 0:
        return "n/a"
    text = f"{_fmt(stat['mean'], digits)}"
    if stat["sd"] is not None:
        text += f" ± {_fmt(stat['sd'], digits)}"
    return text + f" (n={stat['n']})"


def render_report(summary):
    out = ["<!-- GENERATED by eval/dfa_eval/outcomes.py from outcomes-summary.json — do not "
           "hand-edit -->", "", f"# Outcome benchmark: {summary['task']}", ""]
    out.append(f"Run `{summary['run_id']}` · planner `{summary['planner']['model']}` "
               f"(effort {summary['planner']['effort']}) · implementer "
               f"`{summary['implementer']['model']}` (effort {summary['implementer']['effort']}) · "
               f"{summary['attempts']['ok']} complete attempts; {summary['attempts']['recorded']} "
               f"recorded of {summary['attempts'].get('planned', 'n/a')} planned.")
    out += ["", "## How to read this", "",
            "- Pass rates are the share of hidden tests passed (0–1). Round 1 is the brief; round 2 "
            "adds the change request (`change-request`) and re-runs every round-1 test "
            "(`regression`). Rework is lines added plus removed between the two rounds.",
            "- Each attempt is one plan given to one implementer session. A claude-cli "
            "implementer has file tools only (Read, Write, Edit) and cannot run code; an agent "
            "behind the command provider may.",
            "- With fewer than 10 attempts per arm these are descriptive measurements, not "
            "evidence of a difference; intervals are percentile bootstraps over attempts.",
            "- One task in one domain. See eval/outcomes/README.md for what this does not measure.",
            "", "## Per arm (mean ± sd)", ""]
    out += ["| Arm | Plan | n | Round-1 pass rate | Change-request pass rate | Regression pass rate "
            "| Rework lines | Implementer cost (USD) |", "|---|---|---:|---:|---:|---:|---:|---:|"]
    for arm in summary["arms"]:
        m = arm["metrics"]
        out.append(f"| {arm['arm']} | {arm['plan_condition'] or '—'} | {arm['n']} | "
                   f"{_dist(m.get('round1_pass_rate'))} | {_dist(m.get('change_request_pass_rate'))} | "
                   f"{_dist(m.get('regression_pass_rate'))} | {_dist(m.get('rework_lines'), 0)} | "
                   f"{_dist(m.get('cost_usd'))} |")
    categories = sorted({k[3:] for arm in summary["arms"] for k in arm["metrics"]
                         if k.startswith("r1_")})
    if categories:
        out += ["", "## Round-1 pass rate by category (mean)", ""]
        out += ["| Arm | " + " | ".join(categories) + " |",
                "|---|" + "---:|" * len(categories)]
        for arm in summary["arms"]:
            cells = [_fmt((arm["metrics"].get(f"r1_{c}") or {}).get("mean")) for c in categories]
            out.append(f"| {arm['arm']} | " + " | ".join(cells) + " |")
    failed = summary.get("failed_tests") or {}
    if failed:
        arm_names = [arm["arm"] for arm in summary["arms"]]
        out += ["", "## Hidden tests that failed (attempts per arm)", "",
                "Where the failures are: how many attempts' code failed each test, per round.", "",
                "| Round | Test | " + " | ".join(arm_names) + " |",
                "|---|---|" + "---:|" * len(arm_names)]
        for rnd, tests in failed.items():
            for test_id, by_arm in tests.items():
                out.append(f"| {rnd} | `{test_id}` | "
                           + " | ".join(str(by_arm.get(a, 0)) for a in arm_names) + " |")
    if summary["contrasts"]:
        out += ["", "## Differences between arms", ""]
        out += ["| Treatment − control | Metric | Mean difference | 95% bootstrap CI | Cliff's δ "
                "| n | Notes |", "|---|---|---:|---|---:|---|---|"]
        for c in summary["contrasts"]:
            ci = c["bootstrap_ci95"]
            ci_text = f"[{_fmt(ci[0])}, {_fmt(ci[1])}]" if ci else "n/a"
            out.append(f"| {c['treatment']} − {c['control']} | {c['metric']} | "
                       f"{_fmt(c['mean_diff'])} | {ci_text} | {_fmt(c['cliffs_delta'])} | "
                       f"{c['n_treatment']} vs {c['n_control']} | {'; '.join(c['notes']) or '—'} |")
    cost = summary.get("cost")
    if cost:
        out += ["", "## Cost (USD, as recorded)", "",
                f"Planning {_fmt(cost['planning_usd'])} · implementer "
                f"{_fmt(cost['implementer_usd'])} · re-run (superseded) attempts "
                f"{_fmt(cost['superseded_implementer_usd'])} · total {_fmt(cost['total_usd'])}."]
    if summary["flags"]:
        out += ["", "## Flags", ""] + [f"- {flag}" for flag in summary["flags"]]
    return "\n".join(out) + "\n"


def _compare(path, expected):
    current = path.read_bytes().decode("utf-8").replace("\r\n", "\n") if path.is_file() else None
    if current == expected:
        return True, ""
    diff = "".join(difflib.unified_diff((current or "").splitlines(True), expected.splitlines(True),
                                        fromfile=f"{path.name} (committed)",
                                        tofile=f"{path.name} (recomputed)"))
    return False, diff or f"{path.name} is missing"


def _write_safely(stream, text):
    """Write text a console may not be able to encode (e.g. cp1252 on Windows) without failing."""
    encoding = getattr(stream, "encoding", None) or "utf-8"
    stream.write(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))


def write_or_check(run_dir, check=False):
    run_dir = Path(run_dir)
    summary = aggregate(run_dir)
    summary_text = records.dumps(summary)
    report_text = render_report(summary)
    ok = True
    for name, text in ((SUMMARY, summary_text), (REPORT, report_text)):
        path = run_dir / name
        if check:
            same, diff = _compare(path, text)
            if not same:
                _write_safely(sys.stdout, diff)
                print(f"ERROR: {path} does not match the attempt records", file=sys.stderr)
                ok = False
        else:
            records.write_text(path, text)
            print(f"wrote {path}")
    return ok


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(prog="python eval/run.py outcomes",
                                     description="The outcome benchmark (eval/outcomes/README.md).")
    sub = parser.add_subparsers(dest="step", required=True)
    for name, needs_config in (("plan", True), ("implement", True), ("aggregate", False),
                               ("report", False)):
        p = sub.add_parser(name)
        p.add_argument("--run", required=True, help="the run directory")
        if needs_config:
            p.add_argument("--config", default="eval/outcomes/outcomes.json")
            p.add_argument("--jobs", type=int)
            p.add_argument("--max-cost-usd", type=float)
            p.add_argument("--retry-failed", action="store_true")
        else:
            p.add_argument("--check", action="store_true", help="write nothing; exit 1 if stale")
        if name == "implement":
            p.add_argument("--allow-code-execution", action="store_true",
                           help="required: this step runs model-written code")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        if args.step in ("aggregate", "report"):
            return 0 if write_or_check(args.run, args.check) else 1
        ocfg = load_outcomes_config(args.config)
        if args.step == "plan":
            counts = run_plan(args.run, ocfg, args.config, args.jobs, args.max_cost_usd,
                              args.retry_failed)
        else:
            counts = run_implement(args.run, ocfg, args.allow_code_execution, args.jobs,
                                   args.max_cost_usd, args.retry_failed)
        print(json.dumps(counts))
        return 0
    except (OutcomeError, configmod.ConfigError, generate.HarnessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
