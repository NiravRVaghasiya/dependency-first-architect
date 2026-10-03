"""Command line for the evaluation harness: `python eval/run.py <command> --help`.

Stages, each reading and writing one run directory:
  plan       print the experiment matrix and call counts (a dry run: no calls, no files)
  generate   one isolated session per (prompt, condition, generator, run)
  blind      write the blind copies judges see, and the sealed key
  judge      score the blind copies with every judge and rubric
  probe      leakage probe: can a judge tell which plans followed a methodology?
  lint       deterministic plan-structure checks (plan-template v2)
  aggregate  summary.json from the raw records (--check: recompute and byte-compare)
  report     REPORT.md from summary.json (--check: byte-compare)
  validate   check every record of a run against its schema and its files
  all        generate, blind, judge, probe, lint, aggregate, report
Repository checks:
  matrix     docs/evaluation-matrix.md from the committed runs (--check: byte-compare)
  check      what CI runs: rubric/checklist/control-skill/prompt provenance, then validate +
             aggregate --check + report --check for every run in eval/results, then matrix --check
  outcomes   the outcome benchmark (delegates to dfa_eval/outcomes.py)
"""

import argparse
import difflib
import importlib
import importlib.util
import json
import re
import sys
from pathlib import Path

from . import blinding
from . import config as configmod
from . import generate, judge, providers, records, schema, schemas
from .config import ConfigError
from .generate import HarnessError
from .providers import ProviderError

ROOT = configmod.ROOT
DEFAULT_CONFIG = ROOT / "eval" / "benchmark.json"
MATRIX_DOC = Path("docs") / "evaluation-matrix.md"
OUTCOMES = "outcomes"
EVALS_PROMPT = re.compile(r"\*\*(P\d) \([^)]*\):\*\* \"([^\"]+)\"")
CONTROL_FILES = (("SKILL.md", "skill_md"),
                 ("reference/document-template.md", "document_template_md"),
                 ("reference/guidance.md", "guidance_md"))


def _analysis(name):
    """An analysis module written separately (lint, aggregate, report, matrix, outcomes)."""
    try:
        return importlib.import_module(f"{__package__}.{name}")
    except ModuleNotFoundError as exc:
        if exc.name == f"{__package__}.{name}":
            raise HarnessError(f"eval/dfa_eval/{name}.py is missing") from None
        raise


def _has_module(name):
    return importlib.util.find_spec(f"{__package__}.{name}") is not None


def _lf(path):
    return Path(path).read_bytes().decode("utf-8").replace("\r\n", "\n")


def _rel(path, root=None):
    path = Path(path).resolve()
    try:
        return path.relative_to(Path(root or ROOT).resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def compare_text(path, expected, label, fix_hint):
    """[] if `path` holds exactly `expected` (CRLF read as LF); else print a diff, return why."""
    path = Path(path)
    if not path.is_file():
        return [f"{label}: {_rel(path)} is missing; {fix_hint}"]
    actual = _lf(path)
    if actual == expected:
        return []
    sys.stdout.writelines(difflib.unified_diff(
        actual.splitlines(keepends=True), expected.splitlines(keepends=True),
        fromfile=f"{_rel(path)} (committed)", tofile=f"{_rel(path)} (regenerated)"))
    return [f"{label}: {_rel(path)} is stale; {fix_hint}"]


# --------------------------------------------------------------------------------------------
# Analysis stages (lint/aggregate/report/matrix live in their own modules)
# --------------------------------------------------------------------------------------------

def summary_text(run_dir):
    summary = _analysis("aggregate").aggregate(Path(run_dir))
    schema.check(summary, schemas.SUMMARY, "summary")
    return records.dumps(summary)


def report_text(run_dir):
    run = records.RunDir(run_dir)
    if not run.summary_path.is_file():
        raise HarnessError(f"{_rel(run.summary_path)} is missing; run `aggregate` first")
    return _analysis("report").render_report(records.read_json(run.summary_path), run.manifest())


def aggregate_run(run_dir, check=False):
    run = records.RunDir(run_dir)
    text = summary_text(run_dir)
    if check:
        return compare_text(run.summary_path, text, "aggregate --check",
                            f"run `python eval/run.py aggregate --run {_rel(run.root)}`")
    records.write_text(run.summary_path, text)
    print(f"wrote {_rel(run.summary_path)}")
    return []


def report_run(run_dir, check=False):
    run = records.RunDir(run_dir)
    text = report_text(run_dir)
    if check:
        return compare_text(run.report_path, text, "report --check",
                            f"run `python eval/run.py report --run {_rel(run.root)}`")
    records.write_text(run.report_path, text)
    print(f"wrote {_rel(run.report_path)}")
    return []


def matrix_text(repo_root):
    return _analysis("matrix").render_matrix(Path(repo_root))


def matrix_doc(repo_root, check=False):
    path = Path(repo_root) / MATRIX_DOC
    text = matrix_text(repo_root)
    if check:
        return compare_text(path, text, "matrix --check", "run `python eval/run.py matrix`")
    records.write_text(path, text)
    print(f"wrote {_rel(path, repo_root)}")
    return []


# --------------------------------------------------------------------------------------------
# validate
# --------------------------------------------------------------------------------------------

def _load(path, errors):
    try:
        return records.read_json(path)
    except (OSError, ValueError) as exc:
        errors.append(f"{path.parent.name}/{path.name}: cannot read: {exc}")
        return None


def _schema_ok(record, record_schema, label, errors):
    found = schema.validate(record, record_schema)
    errors += [f"{label}: {e}" for e in found]
    return not found


def _raw_exists(run, record, label, errors):
    raw = record.get("raw_file")
    if raw and not _inside_run(raw):
        errors.append(f"{label}: raw file {raw!r} is not a path inside the run directory")
    elif raw and not (run.root / raw).is_file():
        errors.append(f"{label}: raw file {raw} is missing")


def _inside_run(rel):
    """A recorded path stays inside the run: relative, POSIX, and without '..'."""
    parts = rel.split("/")
    return not (rel.startswith("/") or ":" in rel or "\\" in rel or ".." in parts
                or "" in parts)


def _matches_raw(run, rec, fields_of, label, errors):
    """An ok judgment or probe must hold exactly what its raw provider output says: re-parse the
    raw file the way the provider did and re-derive the record's answer fields."""
    raw = rec.get("raw_file")
    if rec["status"] != "ok" or not raw or not (run.root / raw).is_file():
        return
    structured = providers.structured_from_raw(rec["provider"],
                                               (run.root / raw).read_bytes().decode("utf-8"))
    if structured is providers.UNVERIFIABLE:
        return
    try:
        expected = fields_of(structured) if isinstance(structured, dict) else None
    except (KeyError, TypeError):
        expected = None
    if expected is None:
        errors.append(f"{label}: its raw output {raw} holds no valid structured answer")
        return
    for name, value in expected.items():
        if rec.get(name) != value:
            errors.append(f"{label}: {name} differs from its raw output {raw}")


def _v1_generation_sha(root):
    """{(prompt, condition): sha256} recorded in examples/scoring/generation.json for v1."""
    path = root / "examples" / "scoring" / "generation.json"
    if not path.is_file():
        return {}
    arms = _analysis("importer").ARMS
    return {(e["prompt"], arms[e["arm"]]): e["sha256"]
            for e in json.loads(_lf(path)).get("runs", []) if e.get("arm") in arms}


def validate_run(run_dir, repo_root=None):
    """Every inconsistency in a run directory: schemas, ids, hashes, files, references."""
    root = Path(repo_root) if repo_root else ROOT
    run = records.RunDir(run_dir)
    errors = []
    if not run.manifest_path.is_file():
        return [f"{_rel(run.root)}: no manifest.json"]
    manifest = _load(run.manifest_path, errors)
    if manifest is None or not _schema_ok(manifest, schemas.MANIFEST, "manifest.json", errors):
        return errors
    config = manifest["config"]
    if not _schema_ok(config, schemas.CONFIG, "manifest.json config", errors):
        return errors
    if manifest["run_id"] != run.root.resolve().name:
        errors.append(f"manifest.json: run_id {manifest['run_id']!r} is not the directory name")
    if configmod.config_sha256(config) != manifest["config_sha256"]:
        errors.append("manifest.json: config_sha256 does not match the config it records")
    if manifest["seed"] != config["seed"]:
        errors.append("manifest.json: seed differs from the config's seed")
    prompts = configmod.by_id(config["prompts"])
    generators = configmod.by_id(config["generators"])
    judges = configmod.by_id(config.get("judges", []))

    gens, v1_sha = {}, None
    for path in sorted((run.root / records.GENERATIONS).glob("*.json")):
        label = f"{records.GENERATIONS}/{path.name}"
        rec = _load(path, errors)
        if rec is None or not _schema_ok(rec, schemas.GENERATION, label, errors):
            continue
        gid = rec["gen_id"]
        gens[gid] = rec
        if path.name != f"{gid}.json":
            errors.append(f"{label}: holds gen_id {gid}")
        try:
            expected = records.gen_id(rec["prompt_id"], rec["condition"], rec["generator"],
                                      rec["run_index"])
        except ValueError as exc:
            expected = str(exc)
        if expected != gid:
            errors.append(f"{label}: gen_id should be {expected}")
        for field, known in (("prompt_id", prompts), ("condition", config["conditions"]),
                             ("generator", generators)):
            if rec[field] not in known:
                errors.append(f"{label}: {field} {rec[field]!r} is not in the run's config")
        if rec["provider"] == "imported":  # made elsewhere (e.g. the v1 examples): no seed
            if rec["seed"] is not None:
                errors.append(f"{label}: an imported plan cannot carry a harness seed")
            if v1_sha is None:
                v1_sha = _v1_generation_sha(root)
            if v1_sha.get((rec["prompt_id"], rec["condition"])) != rec["output_sha256"]:
                errors.append(f"{label}: output_sha256 is not the sha256 that "
                              "examples/scoring/generation.json recorded for this plan")
        elif rec["seed"] != configmod.derive_seed(config["seed"], gid):
            errors.append(f"{label}: seed is not the one derived from the config seed")
        _raw_exists(run, rec, label, errors)
        md = run.root / records.GENERATIONS / f"{gid}.md"  # schema-checked id: no separators
        if rec["status"] == "ok":
            if rec["model_mismatch"]:
                errors.append(f"{label}: status ok despite a model mismatch")
            if rec["output_file"] != f"{records.GENERATIONS}/{gid}.md" or not md.is_file():
                errors.append(f"{label}: the plan file {rec['output_file']} is missing")
                continue
            data = md.read_bytes()
            if records.sha256_bytes(data) != rec["output_sha256"]:
                errors.append(f"{label}: {rec['output_file']} does not match output_sha256 "
                              "(the plan was edited after generation)")
            elif generate.text_metrics(data.decode("utf-8")) != rec["metrics"]:
                errors.append(f"{label}: metrics do not match {rec['output_file']}")
        elif rec["output_file"] is not None or md.exists():
            errors.append(f"{label}: a failed generation must not have a plan file")
    for md in sorted((run.root / records.GENERATIONS).glob("*.md")):
        if gens.get(md.stem, {}).get("status") != "ok":
            errors.append(f"{records.GENERATIONS}/{md.name}: no ok generation record for it")

    ok_gens = {gid for gid, rec in gens.items() if rec["status"] == "ok"}
    entries = {}
    key = None
    if run.blind_key_path.is_file():
        key = _load(run.blind_key_path, errors)
        if key is not None and not _schema_ok(key, schemas.BLIND_KEY, "blind/key.json", errors):
            key = None
    if key is not None:
        salt = blinding.blind_salt(manifest["seed"], manifest["run_id"])
        if key["salt"] != salt:
            errors.append("blind/key.json: salt is not the one derived from the run")
        ids = [e["blind_id"] for e in key["entries"]]
        if ids != sorted(set(ids)):
            errors.append("blind/key.json: entries must be unique and sorted by blind_id")
        for entry in key["entries"]:
            bid = entry["blind_id"]
            entries[bid] = entry
            if entry["gen_id"] not in ok_gens:
                errors.append(f"blind/key.json: {bid} points to {entry['gen_id']}, which is not "
                              "an ok generation")
            if bid != blinding.blind_id(salt, entry["gen_id"], entry["variant"]):
                errors.append(f"blind/key.json: {bid} is not the id derived for "
                              f"{entry['gen_id']} {entry['variant']}")
            path = run.root / records.BLIND / f"{bid}.md"
            if not path.is_file():
                errors.append(f"blind/{bid}.md is missing")
            elif records.sha256_bytes(path.read_bytes()) != entry["sha256"]:
                errors.append(f"blind/{bid}.md does not match its sha256 in the key")
        covered = {(e["gen_id"], e["variant"]) for e in key["entries"]}
        for gid in sorted(ok_gens):
            for variant in ("plain", "neutralized"):
                if (gid, variant) not in covered:
                    errors.append(f"blind/key.json: no {variant} copy of {gid} (re-run blind)")
        for path in sorted((run.root / records.BLIND).glob("*.md")):
            if path.stem not in entries:
                errors.append(f"blind/{path.name} is not in the key")

    neutral = set(key["neutralize_terms_for"]) if key else set()
    rubrics = {}
    for path in sorted((run.root / records.JUDGMENTS).glob("*.json")):
        label = f"{records.JUDGMENTS}/{path.name}"
        rec = _load(path, errors)
        if rec is None or not _schema_ok(rec, schemas.JUDGMENT, label, errors):
            continue
        jid = rec["judgment_id"]
        try:
            expected = records.judgment_id(rec["blind_id"], rec["rubric"], rec["judge"],
                                           rec["repeat"])
        except ValueError as exc:
            expected = str(exc)
        if path.name != f"{jid}.json" or expected != jid:
            errors.append(f"{label}: judgment_id does not match its file name and fields")
        if rec["judge"] not in judges:
            errors.append(f"{label}: judge {rec['judge']!r} is not in the run's config")
        if rec["rubric"] not in config.get("rubrics", []):
            errors.append(f"{label}: rubric {rec['rubric']!r} is not in the run's config")
            continue
        if rec["repeat"] > config.get("judge_repeats", 1):
            errors.append(f"{label}: repeat {rec['repeat']} exceeds judge_repeats")
        _raw_exists(run, rec, label, errors)
        entry = entries.get(rec["blind_id"])
        if entry is None:
            errors.append(f"{label}: blind_id {rec['blind_id']} is not in blind/key.json")
            continue
        want = "neutralized" if rec["rubric"] in neutral else "plain"
        if entry["variant"] != want:
            errors.append(f"{label}: scored the {entry['variant']} copy; {rec['rubric']} uses "
                          f"the {want} copy")
        if rec.get("blind_sha256") is not None and rec["blind_sha256"] != entry["sha256"]:
            errors.append(f"{label}: scored an earlier text of blind/{rec['blind_id']}.md (its "
                          "blind_sha256 is not the key's); run `judge` to redo it")
        if rec["rubric"] not in rubrics:
            try:
                rubrics[rec["rubric"]] = (judge.load_rubric(root, rec["rubric"]),
                                          judge.rubric_sha256(root, rec["rubric"]))
            except ValueError as exc:
                errors.append(f"{label}: {exc}")
                rubrics[rec["rubric"]] = None
        if rubrics[rec["rubric"]] is None:
            continue
        rubric, sha = rubrics[rec["rubric"]]
        if rec["rubric_sha256"] != sha:
            errors.append(f"{label}: scored with a different version of "
                          f"eval/rubrics/{rec['rubric']}.json (rubric files are immutable once "
                          "used; add a new rubric id instead of editing one)")
        snapshot = run.root / "rubrics" / f"{rec['rubric']}.json"  # what aggregate reads
        if snapshot.is_file() and records.sha256_text(_lf(snapshot)) != rec["rubric_sha256"]:
            errors.append(f"{label}: rubrics/{rec['rubric']}.json in the run is not the rubric "
                          "this judgment recorded (rubric_sha256)")
        if rec["status"] != "ok":
            if rec["scores"]:
                errors.append(f"{label}: a {rec['status']} judgment must have no scores")
            continue
        if rec["model_mismatch"]:
            errors.append(f"{label}: status ok despite a model mismatch")
        prompt_cfg = prompts.get(gens.get(entry["gen_id"], {}).get("prompt_id"))
        checklist = None
        if rubric.get("uses_checklists") and prompt_cfg and prompt_cfg.get("checklist"):
            checklist = configmod.load_checklist(root, prompt_cfg["checklist"])
        response = {"dimensions": rec["scores"], "note": rec["note"] or ""}
        if checklist:
            response.update(checklist=rec["checklist"] or [], traps=rec["traps"] or [])
        errors += [f"{label}: {e}" for e in judge.validate_response(response, rubric, checklist)]
        _matches_raw(run, rec, judge.judgment_fields, label, errors)

    probe_judges = set(config.get("probe", {}).get("judges", []))
    for path in sorted((run.root / records.PROBES).glob("*.json")):
        label = f"{records.PROBES}/{path.name}"
        rec = _load(path, errors)
        if rec is None or not _schema_ok(rec, schemas.PROBE, label, errors):
            continue
        try:
            expected = records.probe_id(rec["blind_id"], rec["judge"])
        except ValueError as exc:
            expected = str(exc)
        if path.name != f"{rec['probe_id']}.json" or expected != rec["probe_id"]:
            errors.append(f"{label}: probe_id does not match its file name and fields")
        if rec["judge"] not in probe_judges:
            errors.append(f"{label}: {rec['judge']!r} is not a probe judge in the run's config")
        _raw_exists(run, rec, label, errors)
        entry = entries.get(rec["blind_id"])
        if entry is None or entry["variant"] != "neutralized":
            errors.append(f"{label}: {rec['blind_id']} is not a neutralized copy in the key")
        elif rec.get("blind_sha256") is not None and rec["blind_sha256"] != entry["sha256"]:
            errors.append(f"{label}: probed an earlier text of blind/{rec['blind_id']}.md (its "
                          "blind_sha256 is not the key's); run `probe` to redo it")
        if rec["status"] == "ok" and rec["p_methodology"] is None:
            errors.append(f"{label}: an ok probe needs p_methodology")
        _matches_raw(run, rec, judge.probe_fields, label, errors)

    # Records a retry replaced (generate.supersede): kept for audit and counted in the cost.
    for stage, record_schema, id_field in ((records.GENERATIONS, schemas.GENERATION, "gen_id"),
                                           (records.JUDGMENTS, schemas.JUDGMENT, "judgment_id"),
                                           (records.PROBES, schemas.PROBE, "probe_id")):
        for path in sorted((run.root / generate.SUPERSEDED / stage).glob("*.json")):
            label = f"{generate.SUPERSEDED}/{stage}/{path.name}"
            rec = _load(path, errors)
            if rec is None or not _schema_ok(rec, record_schema, label, errors):
                continue
            stem, _, number = path.stem.rpartition(".")
            if stem != rec[id_field] or not number.isdigit():
                errors.append(f"{label}: a superseded record is named <{id_field}>.<n>.json")
            _raw_exists(run, rec, label, errors)

    for path in sorted((run.root / records.LINT).glob("*.json")):
        label = f"{records.LINT}/{path.name}"
        rec = _load(path, errors)
        if rec is None or not _schema_ok(rec, schemas.LINT, label, errors):
            continue
        if path.name != f"{rec['gen_id']}.json":
            errors.append(f"{label}: holds gen_id {rec['gen_id']}")
        if rec["gen_id"] not in ok_gens:
            errors.append(f"{label}: {rec['gen_id']} is not an ok generation")
    if run.summary_path.is_file():
        summary = _load(run.summary_path, errors)
        if summary is not None and _schema_ok(summary, schemas.SUMMARY, "summary.json", errors):
            if summary["run_id"] != manifest["run_id"]:
                errors.append("summary.json: run_id differs from the manifest")
    if (run.root / OUTCOMES / "config.json").is_file():  # an outcome run: its task is pinned
        errors += [f"{OUTCOMES}/task-files.json: the task changed since this run: {change}"
                   for change in _analysis("outcomes").task_changes(run.root, root)]
    for path in sorted((run.root / OUTCOMES).glob("*.json")):  # outcome-benchmark attempts
        if path.name in ("config.json", "task-files.json"):
            continue
        label = f"{OUTCOMES}/{path.name}"
        rec = _load(path, errors)
        if rec is None or not _schema_ok(rec, schemas.OUTCOME_ATTEMPT, label, errors):
            continue
        if path.name != f"{rec['attempt_id']}.json":
            errors.append(f"{label}: holds attempt_id {rec['attempt_id']}")
        _check_attempt_identity(rec, run.root, label, errors)
        plan = rec["plan_gen_id"]
        if plan is not None and gens.get(plan, {}).get("status") != "ok":
            errors.append(f"{label}: plan {plan} is not an ok generation")
        elif plan is not None:
            text, _ = blinding.strip_self_score(records.read_text(run.generation_text(plan)))
            if records.sha256_text(text) != rec["plan_sha256"]:
                errors.append(f"{label}: plan_sha256 is not the sha256 of plan {plan}")
        _check_attempt_files(run, rec, run.root / OUTCOMES / rec["attempt_id"], label, errors)
    # Attempts a retry replaced (outcomes.supersede_attempt), kept with their code and raw output.
    for path in sorted((run.root / generate.SUPERSEDED / OUTCOMES).glob("*.json")):
        label = f"{generate.SUPERSEDED}/{OUTCOMES}/{path.name}"
        rec = _load(path, errors)
        if rec is None or not _schema_ok(rec, schemas.OUTCOME_ATTEMPT, label, errors):
            continue
        stem, _, number = path.stem.rpartition(".")
        if stem != rec["attempt_id"] or not number.isdigit():
            errors.append(f"{label}: a superseded attempt is named <attempt_id>.<n>.json")
            continue
        _check_attempt_files(run, rec, path.with_suffix(""), label, errors)
    errors += _orphan_raw_files(run)
    return errors


def _check_attempt_identity(rec, run_root, label, errors):
    """attempt_id, arm, run_index and plan must agree with each other and outcomes/config.json."""
    expected = f"{rec['task']}.{rec['arm']}.r{rec['run_index']:02d}"
    if rec["attempt_id"] != expected:
        errors.append(f"{label}: attempt_id should be {expected} (task.arm.rNN)")
    saved = Path(run_root) / OUTCOMES / "config.json"
    if not saved.is_file():
        errors.append(f"{label}: the run has no {OUTCOMES}/config.json")
        return
    ocfg = records.read_json(saved)
    if rec["arm"] not in ocfg.get("arms", {}):
        errors.append(f"{label}: arm {rec['arm']!r} is not in {OUTCOMES}/config.json")
        return
    condition = ocfg["arms"][rec["arm"]]
    want = None if condition is None else records.gen_id(rec["task"], condition,
                                                        ocfg["planner"]["id"], rec["run_index"])
    if rec["plan_gen_id"] != want:
        errors.append(f"{label}: plan_gen_id should be {want} for arm {rec['arm']}")


def _check_attempt_files(run, rec, snapshots, label, errors):
    """An outcome attempt's numbers must follow from its per-test rows, and its file list and
    code hash from the code snapshot it kept."""
    outcomes = _analysis("outcomes")
    for item in rec["rounds"]:
        n = item["round"]
        _raw_exists(run, item["implementer"], f"{label} round {n}", errors)
        tests = item.get("tests")
        if tests and tests["status"] == "ok" and \
                outcomes.category_counts(tests["tests"]) != tests["by_category"]:
            errors.append(f"{label}: round {n} by_category does not follow from its test rows")
        files = {}
        for listed in item["files"]:
            if not _inside_run(listed["path"]):
                errors.append(f"{label}: round {n} lists a file outside its snapshot: "
                              f"{listed['path']!r}")
                continue
            path = snapshots / f"round{n}" / listed["path"]
            if not path.is_file():
                errors.append(f"{label}: round {n} file {listed['path']} is not in its snapshot")
                continue
            files[listed["path"]] = path.read_bytes()
            if outcomes.file_sha256(files[listed["path"]]) != listed["sha256"]:
                errors.append(f"{label}: round {n} snapshot of {listed['path']} does not match "
                              "its sha256")
        if item.get("code_sha256") is not None and len(files) == len(item["files"]) and \
                outcomes.code_sha256(dict(sorted(files.items()))) != item["code_sha256"]:
            errors.append(f"{label}: round {n} code_sha256 does not match its snapshot")


def _orphan_raw_files(run):
    """Raw outputs in raw/ that no current record points to: a record was deleted, or a raw
    file was added. (superseded/raw/ may hold the raw output of a call that crashed before its
    record was written; it is kept for audit and never used.)"""
    referenced = set()
    for stage_dir in (run.root / records.GENERATIONS, run.root / records.JUDGMENTS,
                      run.root / records.PROBES, run.root / OUTCOMES):
        for path in stage_dir.glob("*.json") if stage_dir.is_dir() else []:
            try:
                rec = records.read_json(path)
            except (OSError, ValueError):
                continue
            if not isinstance(rec, dict):
                continue
            raws = [rec.get("raw_file")] + [(r.get("implementer") or {}).get("raw_file")
                                            for r in rec.get("rounds") or [] if isinstance(r, dict)]
            referenced.update(raw for raw in raws if raw)
    problems = []
    folder = run.root / records.RAW
    for path in sorted(folder.glob("*.txt")) if folder.is_dir() else []:
        rel = f"{records.RAW}/{path.name}"
        earlier = re.fullmatch(r"(.+)\.attempt\d+\.txt", path.name)  # an earlier attempt of a call
        if rel in referenced or (earlier and f"{records.RAW}/{earlier.group(1)}.txt" in referenced):
            continue
        problems.append(f"{rel}: no record refers to this raw output")
    return problems


def is_outcome_run(run_dir):
    """An outcome-benchmark run (eval/outcomes): plans plus implementation attempts."""
    return (Path(run_dir) / OUTCOMES / "config.json").is_file()


def check_outcome_summary(run_dir):
    if not _analysis("outcomes").write_or_check(Path(run_dir), check=True):
        return [f"{_rel(run_dir)}: outcomes-summary.json or OUTCOMES.md is stale; run `python "
                f"eval/run.py outcomes aggregate --run {_rel(run_dir)}`"]
    return []


# --------------------------------------------------------------------------------------------
# check (CI)
# --------------------------------------------------------------------------------------------

def _load_run_eval(repo_root):
    path = Path(repo_root) / "examples" / "scoring" / "run_eval.py"
    spec = importlib.util.spec_from_file_location("dfa_v1_run_eval", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _one_newline(text):
    return text.replace("\r\n", "\n").rstrip("\n") + "\n"


def check_rubric_files(root):
    problems = []
    rubrics_dir = root / configmod.RUBRICS_DIR
    for path in sorted(rubrics_dir.glob("*.json")):
        try:
            judge.load_rubric(root, path.stem)
        except ValueError as exc:
            problems.append(str(exc))
    for path in sorted((rubrics_dir / "checklists").glob("*.json")):
        try:
            data = json.loads(_lf(path))
        except ValueError as exc:
            problems.append(f"{_rel(path, root)}: not valid JSON: {exc}")
            continue
        items, traps = data.get("items"), data.get("traps")
        if (data.get("schema") != "dfa-eval/checklist@1" or data.get("prompt_id") != path.stem
                or not isinstance(items, list) or not isinstance(traps, list)):
            problems.append(f"{_rel(path, root)}: needs schema dfa-eval/checklist@1, prompt_id "
                            f"{path.stem!r}, and items and traps lists")
            continue
        ids = [i.get("id") for i in items + traps if isinstance(i, dict)]
        if len(ids) != len(items) + len(traps) or len(set(ids)) != len(ids):
            problems.append(f"{_rel(path, root)}: item and trap ids must be present and unique")
        for item in items:
            if not all(isinstance(item.get(k), str) for k in ("id", "title", "addressed_means",
                                                              "category")):
                problems.append(f"{_rel(path, root)}: item {item.get('id')} needs id, title, "
                                "addressed_means and category")
        for trap in traps:
            if not all(isinstance(trap.get(k), str) for k in ("id", "title", "recognize")):
                problems.append(f"{_rel(path, root)}: trap {trap.get('id')} needs id, title and "
                                "recognize")
    return problems


def check_authoring(root):
    """Checklists and the EQ rubric must equal the blind-authored structured output exactly."""
    problems = []
    rubrics_dir = root / configmod.RUBRICS_DIR
    rubric_files = {p.stem: json.loads(_lf(p)) for p in sorted(rubrics_dir.glob("*.json"))}
    covered = set()
    for path in sorted((rubrics_dir / "authoring").glob("*.json")):
        rel = _rel(path, root)
        record = json.loads(_lf(path))
        session = record.get("session", {})
        if records.sha256_text(session.get("prompt", "")) != session.get("prompt_sha256"):
            problems.append(f"{rel}: session.prompt_sha256 does not match session.prompt")
        output = record.get("structured_output", {})
        for checklist in output.get("checklists", []):
            pid = checklist.get("prompt_id")
            target = rubrics_dir / "checklists" / f"{pid}.json"
            covered.add(f"{pid}.json")
            if not target.is_file():
                problems.append(f"{rel}: has a checklist for {pid}, but "
                                f"{_rel(target, root)} does not exist")
                continue
            committed = json.loads(_lf(target))
            for field in ("prompt_id", "items", "traps"):
                if committed.get(field) != checklist.get(field):
                    problems.append(f"{_rel(target, root)}: {field} differs from the authoring "
                                    f"record {rel}")
        if "dimensions" in output or "scoring_rules" in output:
            owners = [rid for rid, rubric in rubric_files.items()
                      if (rubric.get("provenance") or {}).get("authoring_record") == rel]
            if not owners:
                problems.append(f"{rel}: no rubric names it as provenance.authoring_record")
            for rid in owners:
                rubric = rubric_files[rid]
                dims = {d.get("id"): d for d in rubric.get("dimensions", [])}
                for authored in output.get("dimensions", []):
                    committed = dims.get(authored.get("id"), {})
                    for field, value in authored.items():
                        if committed.get(field) != value:
                            problems.append(f"eval/rubrics/{rid}.json: dimension "
                                            f"{authored.get('id')} {field} differs from {rel}")
                if len(dims) != len(output.get("dimensions", [])):
                    problems.append(f"eval/rubrics/{rid}.json: dimension count differs from {rel}")
                if rubric.get("scoring_rules") != output.get("scoring_rules"):
                    problems.append(f"eval/rubrics/{rid}.json: scoring_rules differ from {rel}")
    for path in sorted((rubrics_dir / "checklists").glob("*.json")):
        if path.name not in covered:
            problems.append(f"{_rel(path, root)}: no authoring record produced it")
    return problems


def check_v1_rubric(root):
    """methodology-adherence-v1 must be exactly the v1.0.0 judge prompt in run_eval.py."""
    problems = []
    run_eval = _load_run_eval(root)
    rubric = json.loads(_lf(root / configmod.RUBRICS_DIR / "methodology-adherence-v1.json"))
    where = "eval/rubrics/methodology-adherence-v1.json"
    if rubric.get("rubric_text") != run_eval.RUBRIC:
        problems.append(f"{where}: rubric_text differs from examples/scoring/run_eval.py RUBRIC")
    if rubric.get("judge_template") != run_eval.JUDGE_TASK:
        problems.append(f"{where}: judge_template differs from run_eval.py JUDGE_TASK")
    if [d.get("name") for d in rubric.get("dimensions", [])] != run_eval.DIMENSIONS:
        problems.append(f"{where}: dimension names differ from run_eval.py DIMENSIONS")
    if rubric.get("kind_suffix") != " system" or rubric.get("scale") != {"min": 0, "max": 2}:
        problems.append(f"{where}: v1 used kind + ' system' and a 0-2 scale")
    return problems


def check_control_skill(root):
    """The control skill must be the authoring session's output, unedited."""
    problems = []
    base = root / "eval" / "conditions" / "generic-architect"
    output = json.loads(_lf(base / "authoring-session.json")).get("structured_output", {})
    for rel, field in CONTROL_FILES:
        path = base / rel
        if not path.is_file():
            problems.append(f"{_rel(path, root)} is missing")
        elif not isinstance(output.get(field), str):
            problems.append(f"{_rel(base / 'authoring-session.json', root)}: no {field}")
        elif _one_newline(_lf(path)) != _one_newline(output[field]):
            problems.append(f"{_rel(path, root)} differs from authoring-session.json {field}")
    return problems


def check_prompts(root, config, where="eval/benchmark.json", require_all=True):
    """P1-P5 = the prompts quoted in reference/evals.md (all five required in the benchmark; in
    another config, every one it uses); each checklist names its prompt's request."""
    problems = []
    quoted = dict(EVALS_PROMPT.findall(_lf(root / "reference" / "evals.md")))
    if sorted(quoted) != ["P1", "P2", "P3", "P4", "P5"]:
        problems.append("reference/evals.md: expected quoted prompts P1-P5, found "
                        f"{sorted(quoted)}")
    requests = {p["id"]: p["request"] for p in config["prompts"]}
    for pid, request in sorted(quoted.items()):
        if (require_all or pid in requests) and requests.get(pid) != request:
            problems.append(f"{where} {pid}: request differs from reference/evals.md "
                            f"({requests.get(pid)!r} vs {request!r})")
    own = f"{configmod.RUBRICS_DIR.as_posix()}/checklists"
    for prompt in config["prompts"]:
        if prompt.get("checklist"):
            if prompt["id"] in quoted and prompt["checklist"] != f"{own}/{prompt['id']}.json":
                problems.append(f"{where} {prompt['id']}: uses checklist {prompt['checklist']}, "
                                f"not {own}/{prompt['id']}.json")
            checklist = configmod.load_checklist(root, prompt["checklist"])
            if checklist.get("request") != prompt["request"]:
                problems.append(f"{prompt['checklist']}: request differs from {where} "
                                f"{prompt['id']}")
    return problems


def check_experiment_prompts(root, runs):
    """The prompt check for every experiment config and every committed run's own config."""
    problems = []
    for path in sorted((root / "eval" / "experiments").glob("*.json")):
        config = configmod.load_config(path, root)
        problems += check_prompts(root, config, _rel(path, root), require_all=False)
    for run_dir in runs:
        manifest = records.read_json(run_dir / records.MANIFEST)
        problems += check_prompts(root, manifest["config"],
                                  f"{_rel(run_dir, root)}/manifest.json", require_all=False)
    return problems


def committed_runs(results_dir):
    results_dir = Path(results_dir)
    if not results_dir.is_dir():
        return []
    return [p for p in sorted(results_dir.iterdir()) if (p / records.MANIFEST).is_file()]


RESULT_FILES = ("summary.json", "REPORT.md", "outcomes-summary.json", "OUTCOMES.md")


def stray_results(results_dir, root):
    """Result files in a directory that is not a committed run: nothing verifies them, so they
    must not be there (a partial commit, a deleted manifest, a hand-written summary)."""
    results_dir = Path(results_dir)
    problems = []
    for path in sorted(results_dir.iterdir()) if results_dir.is_dir() else []:
        if path.is_dir() and not (path / records.MANIFEST).is_file():
            found = [name for name in RESULT_FILES if (path / name).is_file()]
            if found:
                problems.append(f"{_rel(path, root)}: has {', '.join(found)} but no manifest.json, "
                                "so nothing it reports can be verified")
    return problems


def lint_run(run_dir):
    ok, diff = _analysis("lint").check_lint(run_dir)
    if ok:
        return []
    sys.stdout.write(diff)
    return [f"{_rel(run_dir)}: lint records differ from what lint computes; run `python "
            f"eval/run.py lint --run {_rel(run_dir)}`"]


V1_CONDITION = Path("eval") / "conditions" / "dfa-v1.0.0"
# The skill files at commit 3ba6b70, the version the v1.0.0 scorecard measured (`git show
# 3ba6b70:<file> | sha256sum`). Pinned here because CI's shallow clone has no history, and the
# SHA256SUMS next to the files could be regenerated along with an edit.
V1_DIGESTS = {
    "SKILL.md": "e7065359694646516fc22ef7c1d5dbcb2d887e12a80d5ed86f241583a3f0d1c5",
    "reference/ai-systems.md": "d9019167e05dac3611774515a2bf5e7fa8d84d95e999c4080eaa7d130b7cb986",
    "reference/evals.md": "0963dcac6ec329c02bb18cf61be28bef74fd706baf296a8726575d3bcc286b07",
    "reference/layer-map.md": "ff4531a685bfdd0cb2940f2138f689193e0a93f7eee69524d2dbfdaeb7525169",
    "reference/plan-template.md": "7ee2689530ddeb6e7687d0b4fb047e05fa76399fa898b7b64cea9dd27e06d55f",
}


def check_v1_condition(root):
    """eval/conditions/dfa-v1.0.0 must hold exactly the skill files of commit 3ba6b70 (pinned in
    V1_DIGESTS, and listed in its SHA256SUMS), so a v1-vs-v2 comparison really runs v1."""
    folder = root / V1_CONDITION
    sums = folder / "SHA256SUMS"
    if not sums.is_file():
        return [f"{V1_CONDITION.as_posix()}/SHA256SUMS is missing"]
    problems, listed = [], set()
    for line in _lf(sums).splitlines():
        digest, _, rel = line.partition("  ")
        listed.add(rel)
        path = folder / rel
        if V1_DIGESTS.get(rel) != digest:
            problems.append(f"{V1_CONDITION.as_posix()}/SHA256SUMS: {rel} is not the pinned "
                            "3ba6b70 digest")
        if not path.is_file():
            problems.append(f"{V1_CONDITION.as_posix()}/{rel} is missing")
        elif records.sha256_bytes(path.read_bytes().replace(b"\r\n", b"\n")) != digest:
            problems.append(f"{V1_CONDITION.as_posix()}/{rel} differs from the 3ba6b70 file")
    for rel in sorted(set(V1_DIGESTS) - listed):
        problems.append(f"{V1_CONDITION.as_posix()}/SHA256SUMS does not list {rel}")
    skill_files = {p.relative_to(folder).as_posix() for p in folder.rglob("*.md")
                   if p.name != "README.md"}
    for rel in sorted(skill_files - listed):
        problems.append(f"{V1_CONDITION.as_posix()}/{rel} is not part of v1.0.0 (not in SHA256SUMS)")
    return problems


def run_check(repo_root=None, results_dir=None):
    """Everything CI verifies; returns the list of problems (also printed as it goes)."""
    root = Path(repo_root) if repo_root else ROOT
    results_dir = Path(results_dir) if results_dir else root / "eval" / "results"
    failures = []

    def step(label, fn):
        try:
            problems = fn()
        except (ConfigError, HarnessError, OSError, ValueError, KeyError) as exc:
            problems = [f"{label}: {exc}"]
        for problem in problems:
            print(f"FAIL: {problem}")
        if not problems:
            print(f"OK: {label}")
        failures.extend(problems)

    holder = {}

    def load_benchmark():
        holder["config"] = configmod.load_config(root / "eval" / "benchmark.json", root)
        return []

    step("eval/benchmark.json loads and every condition, rubric and checklist exists",
         load_benchmark)
    step("rubric and checklist files parse", lambda: check_rubric_files(root))
    step("checklists and the engineering-quality rubric equal their blind-authored records",
         lambda: check_authoring(root))
    step("methodology-adherence-v1 equals the v1.0.0 RUBRIC and JUDGE_TASK in run_eval.py",
         lambda: check_v1_rubric(root))
    step("the control skill equals its authoring session output", lambda: check_control_skill(root))
    step("the dfa-v1 condition is byte-for-byte the skill at commit 3ba6b70 (pinned digests)",
         lambda: check_v1_condition(root))
    if "config" in holder:
        step("benchmark prompts P1-P5 equal reference/evals.md; checklists match their prompts",
             lambda: check_prompts(root, holder["config"]))
    runs = committed_runs(results_dir)
    step("every results directory with results is a committed run (has manifest.json)",
         lambda: stray_results(results_dir, root))
    step("experiment configs and committed runs use the original P1-P5 and their checklists",
         lambda: check_experiment_prompts(root, runs))
    if not runs:
        print(f"OK: no committed runs under {_rel(results_dir, root)}")
    for run_dir in runs:
        rel = _rel(run_dir, root)
        step(f"{rel}: every record validates", lambda: validate_run(run_dir, root))
        if is_outcome_run(run_dir):
            step(f"{rel}: outcomes-summary.json and OUTCOMES.md are what outcomes.py computes",
                 lambda: check_outcome_summary(run_dir))
            if not (run_dir / records.SUMMARY).is_file():
                if (run_dir / records.REPORT).is_file():
                    step(f"{rel}: REPORT.md has a summary.json behind it",
                         lambda: [f"{rel}: REPORT.md exists without summary.json, so nothing "
                                  "it says can be recomputed"])
                continue
        else:
            step(f"{rel}: every lint record is what lint computes", lambda: lint_run(run_dir))
        step(f"{rel}: summary.json is what aggregate computes",
             lambda: aggregate_run(run_dir, check=True))
        step(f"{rel}: REPORT.md is what report renders", lambda: report_run(run_dir, check=True))
    if _has_module("matrix"):
        step(f"{MATRIX_DOC.as_posix()} is what matrix renders",
             lambda: matrix_doc(root, check=True))
    else:
        print("SKIP: eval/dfa_eval/matrix.py is not present, so the matrix is not checked")
    if (root / "README.md").is_file() and "BEGIN GENERATED evidence" in _lf(root / "README.md"):
        step("README.md's evidence block is what evidence renders",
             lambda: evidence_block(root, check=True))
    return failures


# --------------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------------

def _config_for(args):
    """The config to run: --config, else the existing run's manifest config, else the default."""
    if args.config:
        return configmod.load_config(args.config), args.config
    run = records.RunDir(args.run) if getattr(args, "run", None) else None
    if run is not None and run.manifest_path.is_file():
        manifest = run.manifest()
        return configmod.check_config(manifest["config"], where="manifest config"), \
            manifest.get("config_file")
    return configmod.load_config(DEFAULT_CONFIG), _rel(DEFAULT_CONFIG)


def cmd_plan(args):
    config, path = _config_for(args)
    generate.run_generate(args.run, config, path, args.jobs, args.max_cost_usd, args.prompts,
                          args.retry_failed, dry_run=True)
    return 0


def cmd_generate(args):
    config, path = _config_for(args)
    counts = generate.run_generate(args.run, config, path, args.jobs, args.max_cost_usd,
                                   args.prompts, args.retry_failed, force_config=args.force_config)
    return 1 if counts.get("error") else 0


def require_run(run_dir):
    """The run directory, or a HarnessError saying why it is not one."""
    run = records.RunDir(run_dir)
    if not run.manifest_path.is_file():
        raise HarnessError(f"{_rel(run.root)} is not a run directory (no manifest.json); "
                           "`generate` creates one")
    return run


def cmd_blind(args):
    require_run(args.run)
    blinding.run_blind(args.run, log=generate.default_log)
    return 0


def cmd_judge(args):
    counts = judge.run_judge(args.run, args.jobs, args.max_cost_usd, args.retry_failed)
    return 1 if counts["error"] else 0


def cmd_probe(args):
    counts = judge.run_probe(args.run, args.jobs, args.max_cost_usd, args.retry_failed)
    return 1 if counts["error"] else 0


def cmd_lint(args):
    require_run(args.run)
    _analysis("lint").run_lint(Path(args.run))
    print(f"wrote {_rel(Path(args.run) / records.LINT)}/")
    return 0


def _report_problems(problems):
    for problem in problems:
        print(f"ERROR: {problem}", file=sys.stderr)
    return 1 if problems else 0


def cmd_aggregate(args):
    require_run(args.run)
    return _report_problems(aggregate_run(args.run, args.check))


def cmd_report(args):
    require_run(args.run)
    return _report_problems(report_run(args.run, args.check))


def cmd_matrix(args):
    return _report_problems(matrix_doc(ROOT, args.check))


def evidence_block(repo_root, check=False):
    """README.md's generated evidence block; [] when it is current, else the problems."""
    ok, message = _analysis("evidence").write_or_check(repo_root, check)
    print(message)
    return [] if ok else [message]


def cmd_evidence(args):
    return _report_problems(evidence_block(ROOT, args.check))


def cmd_validate(args):
    problems = validate_run(args.run)
    if not problems:
        print(f"OK: every record in {_rel(args.run)} is valid")
    return _report_problems(problems)


def cmd_check(args):
    failures = run_check(results_dir=args.results)
    if failures:
        print(f"\ncheck FAILED: {len(failures)} problem(s)", file=sys.stderr)
        return 1
    print("\ncheck passed")
    return 0


def cmd_all(args):
    config, path = _config_for(args)
    counts = generate.run_generate(args.run, config, path, args.jobs, args.max_cost_usd,
                                   args.prompts, args.retry_failed, force_config=args.force_config)
    blinding.run_blind(args.run, log=generate.default_log)
    judged = judge.run_judge(args.run, args.jobs, args.max_cost_usd, args.retry_failed)
    probed = judge.run_probe(args.run, args.jobs, args.max_cost_usd, args.retry_failed)
    _analysis("lint").run_lint(Path(args.run))
    aggregate_run(args.run)
    report_run(args.run)
    return 1 if counts.get("error") or judged["error"] or probed["error"] else 0


def cmd_import_v1(args):
    config = configmod.load_config(args.config or DEFAULT_CONFIG)
    if not config["judges"]:
        raise HarnessError("import-v1 takes its judges from --config, which lists none")
    _analysis("importer").import_v1_examples(args.run, config["judges"],
                                             log=generate.default_log)
    return 0


def cmd_outcomes(args):
    return run_outcomes(args.rest)


def run_outcomes(argv):
    """`outcomes ...` is passed through untouched (its --help included) to outcomes.main."""
    module = _analysis("outcomes")
    if not callable(getattr(module, "main", None)):
        raise HarnessError("eval/dfa_eval/outcomes.py has no main(argv) function")
    return module.main(list(argv)) or 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python eval/run.py",
        description="Dependency-First Architect evaluation harness (standard library only).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("\n\n", 1)[1])
    sub = parser.add_subparsers(dest="command", metavar="command")
    sub.required = True

    def add(name, fn, help_text, run=True, run_required=True, config=False, calls=False,
            check=False):
        p = sub.add_parser(name, help=help_text, description=help_text)
        if run:
            p.add_argument("--run", metavar="DIR", required=run_required,
                           help="run directory (e.g. eval/results/core-v2-2026-10-03)")
        if config:
            p.add_argument("--config", metavar="FILE",
                           help="benchmark config (default: the run's manifest config, else "
                                "eval/benchmark.json)")
            p.add_argument("--prompts", nargs="+", metavar="ID", help="only these prompt ids")
            p.add_argument("--force-config", action="store_true",
                           help="continue a run whose config or skill files changed")
        if calls:
            p.add_argument("--jobs", type=int, metavar="N", help="concurrent calls (default: "
                                                                  "limits.jobs)")
            p.add_argument("--max-cost-usd", type=float, metavar="X",
                           help="stop launching calls once the run has cost this much "
                                "(default: limits.max_cost_usd)")
            p.add_argument("--retry-failed", action="store_true",
                           help="redo failed (error/invalid) records instead of skipping them")
        if check:
            p.add_argument("--check", action="store_true",
                           help="write nothing; exit 1 with a diff if the committed file is stale")
        p.set_defaults(func=fn)
        return p

    add("plan", cmd_plan, "print the experiment matrix and call counts; makes no calls",
        run_required=False, config=True, calls=True)
    add("generate", cmd_generate, "generate every missing plan of the matrix", config=True,
        calls=True)
    add("blind", cmd_blind, "write the blind copies and blind/key.json")
    add("judge", cmd_judge, "score every blind copy with every judge and rubric", calls=True)
    add("probe", cmd_probe, "leakage probe on the neutralized copies", calls=True)
    add("lint", cmd_lint, "deterministic plan-structure checks (plan-template v2)")
    add("aggregate", cmd_aggregate, "compute summary.json from the raw records", check=True)
    add("report", cmd_report, "render REPORT.md from summary.json", check=True)
    add("validate", cmd_validate, "check every record of a run against its schema and files")
    add("matrix", cmd_matrix, "render docs/evaluation-matrix.md from the committed runs",
        run=False, check=True)
    add("evidence", cmd_evidence, "render README.md's evidence block from the committed runs",
        run=False, check=True)
    p = add("check", cmd_check, "everything CI verifies (provenance, committed runs, matrix)",
            run=False)
    p.add_argument("--results", metavar="DIR", help="committed runs directory "
                                                    "(default: eval/results)")
    add("all", cmd_all, "generate, blind, judge, probe, lint, aggregate, report", config=True,
        calls=True)
    p = add("import-v1", cmd_import_v1, "import the ten v1.0.0 example plans as a run, to judge "
                                        "them again (then blind, judge, ...)")
    p.add_argument("--config", metavar="FILE", help="config whose judges the run will use "
                                                    "(default: eval/benchmark.json)")
    p = add("outcomes", cmd_outcomes, "outcome benchmark: python eval/run.py outcomes --help",
            run=False)
    p.add_argument("rest", nargs=argparse.REMAINDER, help="arguments for the outcomes command")
    return parser


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv[:1] == ["outcomes"]:
            return run_outcomes(argv[1:])
        args = build_parser().parse_args(argv)
        return args.func(args)
    except (ConfigError, HarnessError, ProviderError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted; re-run the same command to resume", file=sys.stderr)
        return 130
