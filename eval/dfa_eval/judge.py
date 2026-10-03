"""Judging and leakage probes: independent model judges score the blind copies.

A judge sees one plan and the original request, nothing else: no gen id, condition, file name,
length cap, slash command or other plan. methodology-adherence-v1 renders exactly the v1.0.0
judge prompt on the `plain` copy; rubrics listed in blinding.neutralize_terms_for (by default
engineering-quality-v1, with the prompt's domain checklist and traps) score the `neutralized`
copy. Calls run in a seeded random order (`order_index`), so position effects can be checked.

Every response is validated against the rubric's schema, and dimensions, checklist items and
traps must each appear exactly once. A response that fails is retried (ATTEMPTS calls in all);
if none passes, the judgment is recorded with status "invalid" and the readable reasons, and it
is excluded from every statistic. Provider errors and model mismatches are status "error". Raw
output of every attempt is kept: raw/judge-<id>.txt (the last) and .attempt<N>.txt (earlier);
a record that a new call replaces moves, with its raw files, to superseded/ (generate.supersede).

Every call runs in a fresh directory of its own (emptied again before each retry, removed
afterwards), so concurrent calls never share files and no call can find another's prompt or
answer. Each record keeps the sha256 of the blind copy it scored (`blind_sha256`); a record whose
blind copy has changed since (blinding was re-run with other results) is stale, and judge/probe
redo it.

The leakage probe asks a judge to guess, from a neutralized copy, whether a plan was written with
a methodology; aggregate.py turns the answers into an AUC (0.5 = cannot tell).
"""

import json
import math
import random
import shutil
import string
from pathlib import Path

from . import config as configmod
from . import providers, records, schema, schemas, workspace
from .generate import (STAGGER_S, CostTracker, HarnessError, call_mismatch, check_work_root,
                       default_log, default_work_root, mismatch_error, remove_if_empty, run_cost,
                       run_id_of, run_pool, supersede)

ROOT = configmod.ROOT
ATTEMPTS = 3
TEMPLATE_FIELDS = ("request", "kind", "rubric", "plan", "dimensions", "rules", "items", "traps")
PROBE_TASK = (
    "You are helping audit a blinded study. Some of the plans in this study were written by an AI "
    "model following a specific written planning methodology; the others were written by the "
    "same model without one. Read the plan below and estimate the probability (0 to 1) that it "
    "was written following a specific planning methodology. List up to 5 textual cues you relied "
    "on.\n\nThe plan answers this request: \"{request}\"\n\n<<<PLAN\n{plan}\nPLAN>>>"
)


# --------------------------------------------------------------------------------------------
# Rubrics and prompts
# --------------------------------------------------------------------------------------------

def rubric_path(repo_root, rubric_id):
    name = records.check_id(rubric_id, "rubric id")
    return Path(repo_root) / configmod.RUBRICS_DIR / f"{name}.json"


def rubric_sha256(repo_root, rubric_id):
    return records.sha256_bytes(workspace.read_lf(rubric_path(repo_root, rubric_id)))


def load_rubric(repo_root, rubric_id):
    """The rubric eval/rubrics/<rubric_id>.json; ValueError if it cannot be used as a rubric."""
    path = rubric_path(repo_root, rubric_id)
    try:
        rubric = json.loads(workspace.read_lf(path).decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path.as_posix()}: cannot load: {exc}") from None
    problems = rubric_problems(rubric, rubric_id)
    if problems:
        raise ValueError(f"{path.as_posix()}: " + "; ".join(problems))
    return rubric


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def rubric_problems(rubric, rubric_id=None):
    """What makes `rubric` unusable (empty list: fine)."""
    if not isinstance(rubric, dict):
        return ["a rubric must be a JSON object"]
    problems = []
    if rubric.get("schema") != "dfa-eval/rubric@1":
        problems.append(f"schema must be 'dfa-eval/rubric@1', not {rubric.get('schema')!r}")
    if rubric_id is not None and rubric.get("id") != rubric_id:
        problems.append(f"id is {rubric.get('id')!r}, expected {rubric_id!r}")
    scale = rubric.get("scale")
    if not (isinstance(scale, dict) and _is_int(scale.get("min")) and _is_int(scale.get("max"))
            and scale["min"] < scale["max"]):
        return problems + ["scale must be {min, max} integers with min < max"]
    dims = rubric.get("dimensions")
    if not isinstance(dims, list) or not dims or not all(isinstance(d, dict) for d in dims):
        return problems + ["dimensions must be a non-empty list of objects"]
    ids = [d.get("id") for d in dims]
    if ids != list(range(1, len(dims) + 1)):
        problems.append(f"dimension ids must be 1..{len(dims)} in order, got {ids}")
    if not all(isinstance(d.get("name"), str) and d["name"] for d in dims):
        problems.append("every dimension needs a name")
    template = rubric.get("judge_template")
    if not isinstance(template, str):
        return problems + ["judge_template must be a string"]
    try:
        fields = {f for _, f, _, _ in string.Formatter().parse(template) if f is not None}
    except ValueError as exc:
        return problems + [f"judge_template is not a valid format string: {exc}"]
    unknown = sorted(fields - set(TEMPLATE_FIELDS))
    if unknown:
        problems.append(f"judge_template uses unknown fields {unknown}; known: "
                        f"{', '.join(TEMPLATE_FIELDS)}")
    for needed in ("request", "plan"):
        if needed not in fields:
            problems.append(f"judge_template never shows the {needed}")
    if "rubric" in fields and not isinstance(rubric.get("rubric_text"), str):
        problems.append("judge_template shows {rubric} but rubric_text is missing")
    if "kind" in fields and not isinstance(rubric.get("kind_suffix", ""), str):
        problems.append("kind_suffix must be a string")
    if "dimensions" in fields:
        anchors = [str(s) for s in range(scale["min"], scale["max"] + 1)]
        for dim in dims:
            if not (isinstance(dim.get("definition"), str) and isinstance(dim.get("look_for"), str)
                    and isinstance(dim.get("anchors"), dict)
                    and sorted(dim["anchors"]) == sorted(anchors)):
                problems.append(f"dimension {dim.get('id')} needs definition, look_for and anchors "
                                f"{anchors[0]}..{anchors[-1]}")
    if "rules" in fields and not (isinstance(rubric.get("scoring_rules"), list) and all(
            isinstance(r, str) for r in rubric["scoring_rules"])):
        problems.append("judge_template shows {rules} but scoring_rules is not a list of strings")
    if ({"items", "traps"} & fields) and not rubric.get("uses_checklists"):
        problems.append("judge_template shows a checklist but uses_checklists is not true")
    return problems


def render_dimensions(rubric):
    lo, hi = rubric["scale"]["min"], rubric["scale"]["max"]
    blocks = []
    for dim in rubric["dimensions"]:
        lines = [f"{dim['id']}. {dim['name']}: {dim['definition']}",
                 f"   Look for: {dim['look_for']}"]
        lines += [f"   {s}: {dim['anchors'][str(s)]}" for s in range(lo, hi + 1)]
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def render_judge_prompt(rubric, prompt_cfg, plan, checklist=None):
    """The full judge prompt: the rubric's template filled in. Shows the original request only
    (no length cap, no slash command) and the blind copy of the plan."""
    fields = {
        "request": prompt_cfg["request"],
        "kind": prompt_cfg["kind"] + rubric.get("kind_suffix", ""),
        "rubric": rubric.get("rubric_text", ""),
        "plan": plan,
        "dimensions": "",
        "rules": "\n".join(f"- {rule}" for rule in rubric.get("scoring_rules", [])),
        "items": "- (none)",
        "traps": "- (none)",
    }
    if "{dimensions}" in rubric["judge_template"]:
        fields["dimensions"] = render_dimensions(rubric)
    if checklist:
        fields["items"] = "\n".join(
            f"- {i['id']} [{i['category']}] {i['title']}: {i['addressed_means']}"
            for i in checklist["items"])
        fields["traps"] = "\n".join(f"- {t['id']} {t['title']}: {t['recognize']}"
                                    for t in checklist["traps"])
    return rubric["judge_template"].format(**fields)


def _comparable(value):
    if isinstance(value, (str, int, float)) or value is None:
        return value
    return json.dumps(value, sort_keys=True)


def _exact_ids(path, key, items, expected):
    """Readable errors unless every expected id appears exactly once and nothing else does."""
    if not isinstance(items, list):
        return []  # the schema check already says what is wrong
    got = [_comparable(item.get(key)) for item in items if isinstance(item, dict)]
    errors = []
    missing = [e for e in expected if e not in got]
    repeated = [g for n, g in enumerate(got) if got.count(g) > 1 and got.index(g) == n]
    unexpected = [g for n, g in enumerate(got) if g not in expected and got.index(g) == n]
    if missing:
        errors.append(f"{path}: missing {key} {', '.join(map(str, missing))}")
    if repeated:
        errors.append(f"{path}: {key} {', '.join(map(str, repeated))} given more than once")
    if unexpected:
        errors.append(f"{path}: unexpected {key} {', '.join(map(str, unexpected))}")
    return errors


def validate_response(structured, rubric, checklist=None):
    """Every problem with a judge's structured response (empty list: valid)."""
    if not isinstance(structured, dict):
        got = "nothing" if structured is None else type(structured).__name__
        return [f"$: no structured JSON object in the response (got {got})"]
    errors = schema.validate(structured, schemas.judge_response_schema(rubric, checklist))
    errors += _exact_ids("$.dimensions", "dim", structured.get("dimensions"),
                         list(range(1, len(rubric["dimensions"]) + 1)))
    if rubric.get("uses_checklists") and checklist:
        errors += _exact_ids("$.checklist", "id", structured.get("checklist"),
                             [i["id"] for i in checklist["items"]])
        errors += _exact_ids("$.traps", "id", structured.get("traps"),
                             [t["id"] for t in checklist["traps"]])
    return errors


def validate_probe(structured):
    if not isinstance(structured, dict):
        got = "nothing" if structured is None else type(structured).__name__
        return [f"$: no structured JSON object in the response (got {got})"]
    return schema.validate(structured, schemas.PROBE_RESPONSE)


# --------------------------------------------------------------------------------------------
# Task lists
# --------------------------------------------------------------------------------------------

def _context(run_dir):
    run = records.RunDir(run_dir)
    if not run.manifest_path.is_file():
        raise HarnessError(f"{run.root} has no manifest.json; run `generate` first")
    key = run.blind_key()
    if key is None:
        raise HarnessError(f"{run.root} has no blind/key.json; run `blind` first")
    manifest = run.manifest()
    gens = {g["gen_id"]: g for g in run.generations()}
    usable = [e for e in key["entries"]
              if gens.get(e["gen_id"], {}).get("status") == "ok"]
    return run, manifest, manifest["config"], usable, gens, key


def blind_plan(run, entry):
    """The text of a blind copy, which must be the one its key entry describes (the sha256 every
    judgment and probe of it records)."""
    data = run.blind_text(entry["blind_id"]).read_bytes()
    if records.sha256_bytes(data) != entry["sha256"]:
        raise HarnessError(f"blind/{entry['blind_id']}.md does not match its sha256 in "
                           "blind/key.json; run `blind` again before judging")
    return data.decode("utf-8")


def plan_judging(run_dir, repo_root=None):
    """Every judge call the run needs, in execution order, each with its full prompt.

    rubrics x blind copies (the rubric's variant) x judges x repeats, shuffled with
    random.Random(derive_seed(seed, "judge-order")); `order_index` is the position.
    """
    root = Path(repo_root) if repo_root else ROOT
    run, _, config, entries, gens, key = _context(run_dir)
    prompts = configmod.by_id(config["prompts"])
    neutral = set(key["neutralize_terms_for"])
    checklists = {}
    tasks = []
    for rubric_id in config["rubrics"]:
        try:
            rubric = load_rubric(root, rubric_id)
        except ValueError as exc:
            raise HarnessError(str(exc)) from None
        sha = rubric_sha256(root, rubric_id)
        variant = "neutralized" if rubric_id in neutral else "plain"
        for entry in entries:
            if entry["variant"] != variant:
                continue
            prompt_cfg = prompts[gens[entry["gen_id"]]["prompt_id"]]
            checklist = None
            if rubric.get("uses_checklists") and prompt_cfg.get("checklist"):
                rel = prompt_cfg["checklist"]
                if rel not in checklists:
                    checklists[rel] = configmod.load_checklist(root, rel)
                checklist = checklists[rel]
            plan = blind_plan(run, entry)
            text = render_judge_prompt(rubric, prompt_cfg, plan, checklist)
            response_schema = schemas.judge_response_schema(rubric, checklist)
            for judge in config["judges"]:
                if judge.get("rubrics") and rubric_id not in judge["rubrics"]:
                    continue  # this judge scores only some of the rubrics
                for repeat in range(1, config["judge_repeats"] + 1):
                    jid = records.judgment_id(entry["blind_id"], rubric_id, judge["id"], repeat)
                    tasks.append({"id": jid, "blind_id": entry["blind_id"],
                                  "blind_sha256": entry["sha256"], "rubric": rubric_id,
                                  "rubric_sha256": sha, "judge": judge["id"], "repeat": repeat,
                                  "prompt": text, "schema": response_schema,
                                  "seed": configmod.derive_seed(config["seed"], jid),
                                  "check": (rubric, checklist)})
    random.Random(configmod.derive_seed(config["seed"], "judge-order")).shuffle(tasks)
    for index, task in enumerate(tasks):
        task["order_index"] = index
    return tasks


def plan_probes(run_dir, repo_root=None):
    """Every leakage-probe call: neutralized copies x probe judges, in a seeded random order."""
    run, _, config, entries, gens, _ = _context(run_dir)
    if not config["probe"]["enabled"]:
        return []
    prompts = configmod.by_id(config["prompts"])
    tasks = []
    for entry in entries:
        if entry["variant"] != "neutralized":
            continue
        plan = blind_plan(run, entry)
        request = prompts[gens[entry["gen_id"]]["prompt_id"]]["request"]
        text = PROBE_TASK.format(request=request, plan=plan)
        for judge_id in config["probe"]["judges"]:
            pid = records.probe_id(entry["blind_id"], judge_id)
            tasks.append({"id": pid, "blind_id": entry["blind_id"],
                          "blind_sha256": entry["sha256"], "judge": judge_id,
                          "prompt": text, "schema": schemas.PROBE_RESPONSE,
                          "seed": configmod.derive_seed(config["seed"], pid)})
    random.Random(configmod.derive_seed(config["seed"], "probe-order")).shuffle(tasks)
    for index, task in enumerate(tasks):
        task["order_index"] = index
    return tasks


# --------------------------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------------------------

def call_until_valid(provider, spec, task, check, workdir, timeout_s, tracker):
    """Up to ATTEMPTS calls, until `check(structured)` returns no errors.

    Returns (status, error, validation_errors, results); status is ok, invalid or error.
    Retries stop early if the cost cap is reached. `workdir` is this task's own directory; it is
    emptied before each retry, so no attempt sees what an earlier one left behind.
    """
    results, problems = [], []
    status, error = "invalid", None
    for attempt in range(1, ATTEMPTS + 1):
        if attempt > 1 and not tracker.allow(count_skip=False):
            problems.append(f"attempt {attempt}: not made, the cost cap was reached")
            break
        if attempt > 1:
            shutil.rmtree(workdir, ignore_errors=True)
            Path(workdir).mkdir(parents=True, exist_ok=True)
        try:
            result = provider.judge(task["prompt"], task["schema"], workdir, task["seed"],
                                    timeout_s)
        except Exception as exc:  # recorded as an error, never silently dropped
            result = providers.CallResult(error=f"the provider raised {type(exc).__name__}: {exc}")
        results.append(result)
        tracker.add(result.cost_usd)
        if result.error:
            status, error = "error", result.error
            break
        if call_mismatch(spec.get("model"), result):
            status, error = "error", mismatch_error(spec.get("model"), result)
            break
        errors = check(result.structured)
        if not errors:
            status = "ok"
            break
        problems += [f"attempt {attempt}: {e}" for e in errors]
    return status, error, problems if status != "ok" else [], results


def merged_call_fields(provider, spec, status, results):
    """CALL_FIELDS for a record that took one or more attempts (usage and cost summed)."""
    usage = None
    for result in results:
        if result.usage:
            usage = usage or {k: None for k in providers.USAGE_KEYS}
            for k in providers.USAGE_KEYS:
                if result.usage.get(k) is not None:
                    usage[k] = (usage[k] or 0) + result.usage[k]
    costs = [r.cost_usd for r in results if r.cost_usd is not None]
    latencies = [r.latency_s for r in results if r.latency_s is not None]
    models = sorted({m for r in results for m in r.models_used})
    mismatch = any(call_mismatch(spec.get("model"), r) for r in results)
    return {
        "provider": provider.name,
        "model_requested": spec.get("model"),
        "models_used": models,
        "model_mismatch": mismatch,
        "usage": usage,
        "cost_usd": (costs[0] if len(costs) == 1 else math.fsum(costs)) if costs else None,
        "latency_s": round(math.fsum(latencies), 3) if latencies else None,
    }


def write_raw_attempts(run, raw_id, results):
    """raw/<raw_id>.txt holds the last attempt; earlier ones go to <raw_id>.attempt<N>.txt. Raw
    files of a record this one replaces were moved to superseded/ first (see _execute)."""
    for n, result in enumerate(results[:-1], 1):
        records.write_text(run.raw_path(f"{raw_id}.attempt{n}"), result.raw or "")
    records.write_text(run.raw_path(raw_id), results[-1].raw or "")
    return f"{records.RAW}/{raw_id}.txt"


def _head(schema_id, id_field, task):
    """The first fields of a judgment or probe record: schema, id, blind id, blind copy sha."""
    head = {"schema": schema_id, id_field: task["id"], "blind_id": task["blind_id"]}
    if task.get("blind_sha256"):
        head["blind_sha256"] = task["blind_sha256"]
    return head


def judgment_fields(structured):
    """The record fields a valid judge response becomes (also used by validate to re-derive a
    record from its raw output)."""
    fields = {"scores": sorted(({"dim": d["dim"], "score": d["score"], "evidence": d["evidence"],
                                 "rationale": d["rationale"]} for d in structured["dimensions"]),
                               key=lambda d: d["dim"]),
              "checklist": None, "traps": None, "note": structured["note"]}
    if "checklist" in structured:
        fields["checklist"] = sorted(({"id": c["id"], "status": c["status"],
                                       "evidence": c["evidence"]}
                                      for c in structured["checklist"]), key=lambda c: c["id"])
    if "traps" in structured:
        fields["traps"] = sorted(({"id": t["id"], "present": t["present"],
                                   "evidence": t["evidence"]} for t in structured["traps"]),
                                 key=lambda t: t["id"])
    return fields


def probe_fields(structured):
    return {"p_methodology": structured["p_methodology"], "cues": list(structured["cues"])}


def judgment_record(task, provider, spec, status, error, problems, results, run):
    structured = results[-1].structured if status == "ok" else None
    record = _head("dfa-eval/judgment@1", "judgment_id", task)
    record.update({
        "rubric": task["rubric"],
        "rubric_sha256": task["rubric_sha256"],
        "judge": task["judge"],
        "repeat": task["repeat"],
        "order_index": task["order_index"],
        "status": status,
        "error": error,
        "validation_errors": problems,
        "scores": [],
        "checklist": None,
        "traps": None,
        "note": None,
    })
    if structured is not None:
        record.update(judgment_fields(structured))
    record.update(merged_call_fields(provider, spec, status, results))
    record["raw_file"] = write_raw_attempts(run, f"judge-{task['id']}", results)
    schema.check(record, schemas.JUDGMENT, f"judgment record {task['id']}")  # bug guard
    return record


def probe_record(task, provider, spec, status, error, problems, results, run):
    structured = results[-1].structured if status == "ok" else None
    record = _head("dfa-eval/probe@1", "probe_id", task)
    record.update({
        "judge": task["judge"],
        "order_index": task["order_index"],
        "status": status,
        "error": error,
        "validation_errors": problems,
        "p_methodology": None,
        "cues": [],
    })
    if structured is not None:
        record.update(probe_fields(structured))
    record.update(merged_call_fields(provider, spec, status, results))
    record["raw_file"] = write_raw_attempts(run, f"probe-{task['id']}", results)
    schema.check(record, schemas.PROBE, f"probe record {task['id']}")  # bug guard
    return record


def _fresh_workdir(stage_root, name, task_id):
    """The call's own empty directory. If an earlier call's directory of that name cannot be
    removed (a helper process still holds it open), take the next neutral name instead."""
    for attempt in range(10):
        candidate = name if attempt == 0 else workspace.neutral_name(task_id, f"{name}:{attempt}")
        try:
            return workspace.prepare_workdir(stage_root, candidate)
        except OSError:
            continue
    raise HarnessError(f"cannot create a fresh working directory under {stage_root}")


def call_workdirs(tasks, salt):
    """{task id: neutral directory name}, distinct for every task: concurrent calls must never
    share files (a command provider's prompt.txt and output.txt). Names are hashes of the id, so
    they say nothing about the plan; a rare collision is rehashed."""
    names, taken = {}, set()
    for task in tasks:
        name, n = workspace.neutral_name(task["id"], salt), 0
        while name in taken:
            n += 1
            name = workspace.neutral_name(task["id"], f"{salt}:{n}")
        taken.add(name)
        names[task["id"]] = name
    return names


def _execute(run_dir, tasks, stage, jobs, max_cost_usd, retry_failed, repo_root, work_root, log):
    root = Path(repo_root) if repo_root else ROOT
    run = records.RunDir(run_dir)
    manifest = run.manifest()
    config = manifest["config"]
    limits = config["limits"]
    jobs = jobs or limits["jobs"]
    cap = max_cost_usd if max_cost_usd is not None else limits["max_cost_usd"]
    folder = records.JUDGMENTS if stage == "judge" else records.PROBES
    raw_prefix = "judge" if stage == "judge" else "probe"
    existing = {r["judgment_id" if stage == "judge" else "probe_id"]: r
                for r in (run.judgments() if stage == "judge" else run.probes())}

    def stale(task):
        """The record scored an earlier text of its blind copy (records from before
        blind_sha256 existed cannot tell, and are kept)."""
        sha = (existing.get(task["id"]) or {}).get("blind_sha256")
        return sha is not None and sha != task.get("blind_sha256")

    def needed(task):
        record = existing.get(task["id"])
        return record is None or stale(task) or (retry_failed and record["status"] != "ok")

    todo = [t for t in tasks if needed(t)]
    outdated = sum(stale(t) for t in tasks)
    specs = configmod.by_id(config["judges"])
    instances = {jid: providers.make_provider(specs[jid], root)
                 for jid in sorted({t["judge"] for t in todo})}
    for provider in instances.values():
        provider.preflight()
    default_root = work_root is None
    work_root = default_work_root(run_id_of(run_dir)) if default_root else Path(work_root)
    check_work_root(work_root, root)
    stage_root = work_root / ("j" if stage == "judge" else "q")
    # A helper process of an earlier call (e.g. the CLI's telemetry server) can outlive it and
    # keep its directory open on Windows, so a leftover stage root may not be removable; every
    # call still gets a fresh directory of its own below it.
    shutil.rmtree(stage_root, ignore_errors=True)
    stage_root.mkdir(parents=True, exist_ok=True)
    workdirs = call_workdirs(todo, f"{stage}-workdir:{config['seed']}:{manifest['run_id']}")
    tracker = CostTracker(cap, run_cost(run))
    timeout_s = limits["timeout_s"]
    build = judgment_record if stage == "judge" else probe_record
    path_of = run.judgment_path if stage == "judge" else run.probe_path

    def one(task):
        if not tracker.allow():
            log(f"skip {stage} {task['id']}: cost cap reached")
            return "skipped"
        provider, spec = instances[task["judge"]], specs[task["judge"]]
        if stage == "judge":
            rubric, checklist = task["check"]

            def check(structured):
                return validate_response(structured, rubric, checklist)
        else:
            check = validate_probe
        workdir = _fresh_workdir(stage_root, workdirs[task["id"]], task["id"])
        try:
            status, error, problems, results = call_until_valid(provider, spec, task, check,
                                                                workdir, timeout_s, tracker)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        supersede(run, folder, task["id"], f"{raw_prefix}-{task['id']}")  # a retried/stale one
        record = build(task, provider, spec, status, error, problems, results, run)
        records.write_json(path_of(task["id"]), record)
        detail = f": {error}" if error else (f" ({len(problems)} problems)" if problems else "")
        log(f"{stage} {task['id']}: {status}{detail}")
        return status

    stagger = 0.0 if all(p.name == "fake" for p in instances.values()) else STAGGER_S
    if outdated:
        log(f"{stage}: {outdated} record(s) scored an earlier text of their blind copy (blinding "
            "changed since); judging them again, the old records move to superseded/")
    log(f"{stage}: {len(todo)} calls to make ({len(tasks) - len(todo)} already recorded), "
        f"{jobs} at a time")
    try:
        outcomes = run_pool(todo, one, jobs, stagger)
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)
        remove_if_empty(work_root, also_parent=default_root)
    counts = {"planned": len(tasks), "to_run": len(todo), "ok": outcomes.count("ok"),
              "invalid": outcomes.count("invalid"), "error": outcomes.count("error"),
              "skipped_cost": outcomes.count("skipped"), "spent_usd": round(tracker.spent, 6)}
    log(f"{stage}: {counts['ok']} ok, {counts['invalid']} invalid, {counts['error']} error, "
        f"{counts['skipped_cost']} skipped by the cost cap")
    return counts


RUN_RUBRICS = "rubrics"  # the run's own copy of every rubric file it was judged with


def snapshot_rubrics(run_dir, repo_root=None):
    """Copy each configured rubric file into <run>/rubrics/, so the run stays self-contained
    (aggregate reads the copies) even after eval/rubrics/ changes. Refuses to mix versions: if a
    copy exists and differs from the current file, judging this run again needs a new run."""
    root = Path(repo_root) if repo_root else ROOT
    run = records.RunDir(run_dir)
    config = run.manifest()["config"]
    for rubric_id in config.get("rubrics", []):
        source = rubric_path(root, rubric_id)
        data = source.read_bytes().replace(b"\r\n", b"\n")
        target = run.root / RUN_RUBRICS / f"{rubric_id}.json"
        if target.is_file():
            if target.read_bytes().replace(b"\r\n", b"\n") != data:
                raise HarnessError(f"{rubric_id}: eval/rubrics/{rubric_id}.json changed since this "
                                   "run was judged; judge with the new rubric in a new run")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def run_judge(run_dir, jobs=None, max_cost_usd=None, retry_failed=False, repo_root=None,
              work_root=None, log=None):
    """Judge every blind copy with every judge and rubric; return counts by outcome."""
    tasks = plan_judging(run_dir, repo_root)  # validates the run directory first
    snapshot_rubrics(run_dir, repo_root)
    return _execute(run_dir, tasks, "judge", jobs, max_cost_usd, retry_failed, repo_root,
                    work_root, log or default_log)


def run_probe(run_dir, jobs=None, max_cost_usd=None, retry_failed=False, repo_root=None,
              work_root=None, log=None):
    """Run the leakage probe on every neutralized copy (if the config enables it)."""
    log = log or default_log
    tasks = plan_probes(run_dir, repo_root)
    if not tasks:
        log("probe: disabled in the config, or nothing to probe")
        return {"planned": 0, "to_run": 0, "ok": 0, "invalid": 0, "error": 0,
                "skipped_cost": 0, "spent_usd": 0.0}
    return _execute(run_dir, tasks, "probe", jobs, max_cost_usd, retry_failed, repo_root,
                    work_root, log)
