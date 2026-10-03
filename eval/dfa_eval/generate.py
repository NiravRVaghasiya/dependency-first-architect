"""Generation stage: one isolated session per (prompt, condition, generator, run) of the matrix.

Each session starts in a fresh, empty directory with a neutral name (README.md only, if the config
says so), gets its skill (if any) as a session plugin, and writes nothing into the repository; the
harness records what came back. Every call leaves three files in the run directory:

    generations/<gen_id>.md    the plan exactly as returned (LF line endings, trailing newline)
    generations/<gen_id>.json  metadata: request, seed, model check, usage, cost, tool calls,
                               which instruction files the session actually loaded
    raw/gen-<gen_id>.txt       the provider's raw stdout, for audit

A failed call (provider error, empty output, or a model mismatch: the CLI silently substitutes
unknown model ids) is recorded with status "error" and no .md, so it is visible, never counted,
and retried by --retry-failed. So is a skill-condition session that did not have its skill as the
config says: one that listed its skills without the condition's own and loaded none of its files
(the plugin did not load), one that could use another condition's skill, or one that listed a
second, same-named copy of its own skill (a personal install) next to the session plugin.
Metrics (words, chars, visible_tokens_est = ceil(chars / 4)) are computed on the stored .md
text, so anyone can re-derive them from the committed file.

Resume and cost: existing ok records are skipped; the cost cap counts the cost of every record
already in the run (generations, judgments, probes, and superseded records) and stops launching
calls once it is reached (calls in flight finish). For claude-cli, SKILL.md itself is injected by
the slash command without a tool call, so `instructions_loaded` lists it only when the transcript
shows its whole text; reference files count when the session read them successfully.

Superseded records: a record that a new call replaces (--retry-failed, or a judgment of a blind
copy that changed since) is never overwritten. Before the new record is written, `supersede`
moves the old one and its raw files into the run's superseded/ folder, numbered per record id
(1, 2, ...), never overwritten or deleted:

    superseded/<stage>/<record id>.<n>.json   the replaced record, with raw_file pointing to its
                                              moved raw file (<stage>: generations, judgments,
                                              probes; the record keeps its stage's schema)
    superseded/generations/<gen id>.<n>.md    its plan text, if it had one
    superseded/raw/<raw name>.<n>.txt         its raw files: e.g. gen-<id>.<n>.txt,
                                              judge-<id>.<n>.txt, judge-<id>.attempt1.<n>.txt

Superseded calls were paid for, so `run_cost` (the cost cap) counts their cost_usd.
"""

import datetime
import math
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import HARNESS_VERSION
from . import config as configmod
from . import providers, records, schema, schemas, workspace

ROOT = configmod.ROOT
STAGGER_S = 2.0  # seconds between the first wave of session starts, as in run_eval.py
SUPERSEDED = "superseded"
RECORD_STAGES = (records.GENERATIONS, records.JUDGMENTS, records.PROBES)
_PRINT_LOCK = threading.Lock()


class HarnessError(RuntimeError):
    """A stage cannot run as asked; the message says why and what to do instead."""


def default_log(message):
    with _PRINT_LOCK:
        print(message, flush=True)


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


class CostTracker:
    """Shared spend across a stage's threads; `allow()` is asked before every call starts."""

    def __init__(self, cap, spent=0.0):
        self.cap = cap
        self.spent = float(spent)
        self.skipped = 0
        self._lock = threading.Lock()

    def allow(self, count_skip=True):
        with self._lock:
            if self.cap is not None and self.spent >= self.cap:
                self.skipped += 1 if count_skip else 0
                return False
            return True

    def add(self, cost):
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            with self._lock:
                self.spent += cost


def run_cost(run):
    """Total cost_usd of every record in the run (generations, judgments, probes), superseded
    records included: a call replaced by a retry was still paid for."""
    total = 0.0
    for rec in run.generations() + run.judgments() + run.probes() + superseded_records(run):
        cost = rec.get("cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            total += cost
    return total


# --------------------------------------------------------------------------------------------
# Superseded records (see the module docstring)
# --------------------------------------------------------------------------------------------

def superseded_records(run, stage=None):
    """The records in <run>/superseded/<stage>/ (every record stage if None); unreadable files
    are skipped here (validate reports them)."""
    found = []
    for sub in [stage] if stage else RECORD_STAGES:
        for path in sorted((Path(run.root) / SUPERSEDED / sub).glob("*.json")):
            try:
                data = records.read_json(path)
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                found.append(data)
    return found


def _numbers_in(folder, pattern):
    """Every n in the names in `folder` that fully match `pattern` (whose one group is n)."""
    if not folder.is_dir():
        return set()
    return {int(m.group(1)) for m in (pattern.fullmatch(p.name) for p in folder.iterdir()) if m}


def supersede(run, stage, record_id, raw_stem):
    """Move what a new call is about to replace into <run>/superseded/ under the next free number
    (see the module docstring): the record <stage>/<record_id>.json, its plan text, and its raw
    files raw/<raw_stem>.txt and raw/<raw_stem>.attempt<k>.txt. Raw files are moved even without
    a record (a crash between writing them and the record). Returns the moved record, or None."""
    root = Path(run.root)
    records.check_id(record_id, "record id")
    record_path = root / stage / f"{record_id}.json"
    text_path = root / stage / f"{record_id}.md"
    raw_dir = root / records.RAW
    raw_name = re.compile(re.escape(raw_stem) + r"(?:\.attempt\d+)?\.txt")
    raws = sorted(p for p in raw_dir.iterdir() if raw_name.fullmatch(p.name)) \
        if raw_dir.is_dir() else []
    has_text = stage == records.GENERATIONS and text_path.is_file()
    if not (raws or has_text or record_path.is_file()):
        return None
    out_dir, out_raw = root / SUPERSEDED / stage, root / SUPERSEDED / records.RAW
    numbered_record = re.compile(re.escape(record_id) + r"\.(\d+)\.(?:json|md)")
    numbered_raw = re.compile(re.escape(raw_stem) + r"(?:\.attempt\d+)?\.(\d+)\.txt")
    used = _numbers_in(out_dir, numbered_record) | _numbers_in(out_raw, numbered_raw)
    n = max(used, default=0) + 1
    out_dir.mkdir(parents=True, exist_ok=True)
    moved = {}
    for path in raws:
        out_raw.mkdir(parents=True, exist_ok=True)
        target = out_raw / f"{path.name[:-len('.txt')]}.{n}.txt"
        os.replace(path, target)
        moved[f"{records.RAW}/{path.name}"] = f"{SUPERSEDED}/{records.RAW}/{target.name}"
    if has_text:
        os.replace(text_path, out_dir / f"{record_id}.{n}.md")
    if not record_path.is_file():
        return None
    target = out_dir / f"{record_id}.{n}.json"
    try:
        record = records.read_json(record_path)
    except (OSError, ValueError):  # keep even an unreadable record, byte for byte
        os.replace(record_path, target)
        return None
    if isinstance(record, dict) and record.get("raw_file") in moved:
        record["raw_file"] = moved[record["raw_file"]]
    records.write_json(target, record)
    record_path.unlink()
    return record


def run_pool(items, fn, jobs, stagger_s=0.0):
    """fn(item) for every item on `jobs` threads, in order; results in order.

    The first wave of starts is staggered. Exceptions escaping fn are harness bugs: every one is
    reported, then the first is raised. Ctrl-C cancels queued items and is passed on to the CLIs
    in flight, whose calls then end and are recorded.
    """
    pool = ThreadPoolExecutor(max_workers=max(1, jobs))
    futures = []
    try:
        for index, item in enumerate(items):
            if stagger_s and 0 < index < jobs:
                time.sleep(stagger_s)
            futures.append(pool.submit(fn, item))
        for future in futures:
            future.exception()  # wait
    except KeyboardInterrupt:
        pool.shutdown(wait=False, cancel_futures=True)
        providers.interrupt_running()
        raise
    finally:
        pool.shutdown(wait=True)
    errors = [f.exception() for f in futures if f.exception() is not None]
    for error in errors:
        default_log(f"INTERNAL ERROR: {type(error).__name__}: {error}")
    if errors:
        raise errors[0]
    return [f.result() for f in futures]


def default_work_root(run_id):
    return Path(tempfile.gettempdir()) / "dfa-eval" / run_id


def check_work_root(work_root, repo_root):
    work, repo = Path(work_root).resolve(), Path(repo_root).resolve()
    if work == repo or repo in work.parents:
        raise HarnessError(f"session workspaces must live outside the repository, not in {work}")


def run_id_of(run_dir):
    try:
        return records.check_id(Path(run_dir).resolve().name, "run id (the run directory name)")
    except ValueError as exc:
        raise HarnessError(str(exc)) from None


# --------------------------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------------------------

def repo_commit(repo_root):
    """`git rev-parse --short HEAD`, + "+uncommitted" if SKILL.md, reference/ or eval/ (outside
    eval/results) have changes; "unknown" without git."""
    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(repo_root),
                             capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "SKILL.md", "reference",
                                "eval", ":(exclude)eval/results"], cwd=str(repo_root),
                               capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return (rev or "unknown") + ("+uncommitted" if dirty else "")


def skill_snapshot(repo_root):
    """{"version": VERSION or "unknown", "files": sha256 of SKILL.md and every reference/*.md}."""
    repo_root = Path(repo_root)
    version_file = repo_root / "VERSION"
    version = version_file.read_text(encoding="utf-8").strip() if version_file.is_file() else ""
    files = {}
    if (repo_root / "SKILL.md").is_file():
        files["SKILL.md"] = records.sha256_bytes(workspace.read_lf(repo_root / "SKILL.md"))
    for path in sorted((repo_root / "reference").glob("*.md")):
        files[f"reference/{path.name}"] = records.sha256_bytes(workspace.read_lf(path))
    return {"version": version or "unknown", "files": files}


def package_conditions(config, repo_root, plugins_root):
    """Build one session plugin per distinct skill; describe each skill condition's package.

    {condition: {"plugin", "skill_name", "files": [{path, packaged, sha256, words, repo_path,
    abs}], "excluded": [repo paths]}}. Plugin directories get hashed names.
    """
    out, built = {}, {}
    for cid, cond in config["conditions"].items():
        if cond["kind"] != "skill":
            continue
        key = (Path(cond["skill_dir"]).as_posix(), cond["skill_name"])
        if key not in built:
            source = Path(repo_root) / cond["skill_dir"]
            dest = Path(plugins_root) / ("p" + records.sha256_text(":".join(key))[:8])
            plugin, files = workspace.build_plugin(source, cond["skill_name"], dest)
            _, excluded = workspace.package_contents(source, cond["skill_name"])
            built[key] = (plugin, files, excluded)
        plugin, files, excluded = built[key]
        out[cid] = {
            "plugin": plugin,
            "skill_name": cond["skill_name"],
            "files": [dict(f, repo_path=workspace.source_path(cond["skill_dir"], f["path"]),
                           abs=plugin / f["packaged"]) for f in files],
            "excluded": [workspace.source_path(cond["skill_dir"], rel) for rel in excluded],
        }
    return out


def condition_files(packages):
    return {cid: {f["repo_path"]: f["sha256"] for f in pkg["files"]}
            for cid, pkg in packages.items()}


def config_file_label(config_path, repo_root):
    if not config_path:
        return None
    path = Path(config_path).resolve()
    try:
        return path.relative_to(Path(repo_root).resolve()).as_posix()
    except ValueError:
        return path.name


def tool_versions(config, repo_root):
    """{"claude": version} when any generator or judge uses the Claude CLI."""
    for spec in config["generators"] + config.get("judges", []):
        if spec["provider"] == "claude-cli":
            return {"claude": providers.make_provider(spec, repo_root).version()}
    return {}


def new_manifest(run_dir, config, config_path, repo_root, packages):
    notes = [f"{cid}: {', '.join(pkg['excluded'])} not packaged in the session plugin (SKILL.md "
             "names it only to say it is not loaded while planning; it is the scoring rubric)"
             for cid, pkg in packages.items() if pkg["excluded"]]
    manifest = {
        "schema": "dfa-eval/manifest@1",
        "run_id": run_id_of(run_dir),
        "created": datetime.date.today().isoformat(),
        "harness_version": HARNESS_VERSION,
        "repo_commit": repo_commit(repo_root),
        "skill": skill_snapshot(repo_root),
        "condition_files": condition_files(packages),
        "config_file": config_file_label(config_path, repo_root),
        "config_sha256": configmod.config_sha256(config),
        "config": config,
        "seed": config["seed"],
        "platform": {"python": platform.python_version(), "implementation":
                     platform.python_implementation(), "platform": platform.platform()},
        "tools": tool_versions(config, repo_root),
    }
    if notes:
        manifest["notes"] = "\n".join(notes)
    schema.check(manifest, schemas.MANIFEST, "manifest")
    return manifest


def _add_note(manifest, note):
    manifest["notes"] = (manifest.get("notes", "") + "\n" + note).strip()


def open_run(run_dir, config, config_path, repo_root, packages, force_config=False, log=None):
    """Create the run directory and manifest, or check an existing one still matches."""
    log = log or default_log
    run = records.RunDir(run_dir)
    if not run.manifest_path.is_file():
        if run.root.is_dir() and any(run.root.iterdir()):
            raise HarnessError(f"{run.root} exists and is not empty, but has no manifest.json; "
                               "use a new directory for a new run")
        manifest = new_manifest(run_dir, config, config_path, repo_root, packages)
        run.root.mkdir(parents=True, exist_ok=True)
        records.write_json(run.manifest_path, manifest)
        log(f"new run {manifest['run_id']} (config sha256 {manifest['config_sha256'][:12]})")
        return manifest
    manifest = run.manifest()
    changed = False
    new_sha = configmod.config_sha256(config)
    if manifest["config_sha256"] != new_sha:
        message = (f"this run was started with a different config:\n"
                   f"  manifest config sha256: {manifest['config_sha256']}\n"
                   f"  {config_path or 'given config'} sha256: {new_sha}")
        if not force_config:
            raise HarnessError(message + "\nStart a new run, or pass --force-config to continue "
                               "this one with the new config.")
        log("WARNING: " + message + "\ncontinuing with the new config (--force-config)")
        _add_note(manifest, f"{datetime.date.today().isoformat()}: config replaced with "
                            f"--force-config (previous sha256 {manifest['config_sha256']})")
        manifest["config"], manifest["config_sha256"] = config, new_sha
        changed = True
    current = condition_files(packages)
    recorded = manifest.get("condition_files", {})
    drift = sorted(f"{cid}: {path}" for cid, files in current.items()
                   for path, sha in files.items() if recorded.get(cid, {}).get(path) != sha)
    drift += sorted(f"{cid}: {path} (no longer packaged)" for cid, files in recorded.items()
                    for path in files if path not in current.get(cid, {}))
    if drift:
        message = ("the skill files changed since this run started, so new generations would "
                   "use different instructions:\n  " + "\n  ".join(drift))
        if not force_config:
            raise HarnessError(message + "\nStart a new run, or pass --force-config to mix them.")
        log("WARNING: " + message + "\ncontinuing anyway (--force-config)")
        _add_note(manifest, f"{datetime.date.today().isoformat()}: generations after this date "
                            "used changed skill files: " + "; ".join(drift))
        changed = True
    if changed:
        schema.check(manifest, schemas.MANIFEST, "manifest")
        records.write_json(run.manifest_path, manifest)
    return manifest


# --------------------------------------------------------------------------------------------
# One generation
# --------------------------------------------------------------------------------------------

def call_mismatch(requested, result):
    """The model check for one call (providers.model_mismatch: the main-loop model must be the
    requested one). A failed call that reported no model is not a mismatch (there is nothing to
    compare); a call that succeeded without reporting one is."""
    if result.error and not result.models_used and not result.main_models:
        return False
    return providers.model_mismatch(requested, result.models_used, result.main_models)


def mismatch_error(requested, result):
    """The error recorded for a call whose model check failed."""
    seen = (f"the main model {result.main_models} (models used: {result.models_used})"
            if result.main_models else f"{result.models_used}")
    return (f"model mismatch: requested {requested!r}, but the provider reported {seen}; "
            "not counted")


def _norm(path):
    return os.path.normcase(str(Path(path).resolve()))


def instructions_loaded(result, package, repo_root):
    """The instruction files this session demonstrably loaded (None for plain conditions)."""
    if package is None:
        return None
    loaded = {_norm(p) for p in result.loaded_paths}
    found = {}
    packaged = set()
    for f in package["files"]:
        packaged.add(_norm(f["abs"]))
        if _norm(f["abs"]) in loaded:
            found[f["repo_path"]] = {"path": f["repo_path"], "sha256": f["sha256"],
                                     "words": f["words"]}
    for path in result.loaded_paths:  # e.g. an adapter file a command provider installed
        if _norm(path) in packaged:
            continue
        try:
            rel = Path(path).resolve().relative_to(Path(repo_root).resolve()).as_posix()
            data = workspace.read_lf(path)
        except (ValueError, OSError):
            continue
        found[rel] = {"path": rel, "sha256": records.sha256_bytes(data),
                      "words": len(data.decode("utf-8", errors="replace").split())}
    return [found[k] for k in sorted(found)]


def skill_offered(skill_name, skills_available):
    """False only when the session listed its skills and this one (or plugin:name) is absent."""
    if skills_available is None:
        return True
    return any(name == skill_name or name.endswith(":" + skill_name) for name in skills_available)


def skill_missing(skills_available, package, loaded=None):
    """Why a skill-condition session ran without its skill, or None: it listed the skills it
    offered, the condition's own skill (or <plugin>:<skill>) was not among them, and nothing it
    did shows the skill anyway (`loaded`, its instructions_loaded, is empty: no injected SKILL.md,
    no reference file read). The session plugin did not load or the slash command could not
    resolve, so the plan was written without the skill, and counting it in the treatment arm
    would dilute the contrast."""
    if package is None or loaded or skill_offered(package["skill_name"], skills_available):
        return None
    return (f"skill not offered: the session listed its skills and {package['skill_name']!r} "
            "was not among them, and it loaded none of the skill's files (did the session plugin "
            "load?), so the plan was written without it; not counted")


def skill_reads(tool_calls, skill_name):
    """(attempted, succeeded): the session's Read calls of files inside the skill's directory.
    A read of a directory fails whatever the permissions, so only paths that name a file (the
    last part has a suffix, as every packaged file does) count."""
    marker = f"/skills/{skill_name}/"
    reads = [c for c in tool_calls or [] if c.get("tool") == "Read"
             and marker in "/" + (c.get("path") or "")
             and "." in (c.get("path") or "").rstrip("/").rsplit("/", 1)[-1]]
    return len(reads), sum(1 for c in reads if c.get("ok"))


def skill_reads_denied(tool_calls, package):
    """Why a skill session wrote its plan without the skill's reference files, or None: it asked
    for files inside the skill's directory and every read failed (in pilot-models, Haiku 4.5 ran
    in permission mode `default`, which refused them). Such a plan follows SKILL.md alone, not the
    skill as shipped, so it is not counted."""
    if package is None:
        return None
    attempted, succeeded = skill_reads(tool_calls, package["skill_name"])
    if attempted == 0 or succeeded:
        return None
    return (f"skill files not readable: all {attempted} reads of the skill's files failed "
            "(permissions?), so the plan follows SKILL.md alone; not counted")


def contamination(skills_available, config, condition):
    """A skill this session could use but must not, or None.

    `claude --bare` still offers the user's personal skills. If one of them is a skill under test
    (say the user installed dependency-first-architect), a baseline session could use it, and the
    comparison would be meaningless. The same holds for the condition's own skill: a plugin skill
    is listed as <plugin>:<skill>, so a plain <skill> listed next to it is a second, personal or
    project copy (perhaps another version, perhaps with files the plugin withholds), which the
    slash command may run instead. Such a generation is recorded as an error.
    """
    if not skills_available:
        return None
    own = config["conditions"][condition].get("skill_name")
    others = sorted({c["skill_name"] for c in config["conditions"].values()
                     if c["kind"] == "skill" and c["skill_name"] != own})
    for name in others:
        if any(s == name or s.endswith(":" + name) for s in skills_available):
            return name
    if own and own in skills_available and any(s.endswith(":" + own) for s in skills_available):
        return own
    return None


def contamination_error(leaked, own):
    """The error recorded for a contaminated session (see contamination)."""
    if leaked == own:
        return (f"contaminated: the session listed a second copy of its own skill {leaked!r} "
                "next to the session plugin (installed for the user?), so it may have run that "
                "copy; not counted")
    return (f"contaminated: the session could use skill {leaked!r}, which belongs to another "
            "condition (installed for the user?); not counted")


def text_metrics(body):
    return {"words": len(body.split()), "chars": len(body),
            "visible_tokens_est": math.ceil(len(body) / 4)}


def write_generation(run, gen, spec, provider, request, result, package, started, finished,
                     repo_root):
    """Record one finished call (ok or error) and return the record. A record it replaces (a
    retry) is moved to superseded/ first, with its raw output."""
    gid = gen["gen_id"]
    mismatch = call_mismatch(spec.get("model"), result)
    error = result.error
    if error is None and mismatch:
        error = mismatch_error(spec.get("model"), result)
    if error is None and not (isinstance(result.text, str) and result.text.strip()):
        error = "the provider returned no text"
    raw_id = f"gen-{gid}"
    supersede(run, records.GENERATIONS, gid, raw_id)
    records.write_text(run.raw_path(raw_id), result.raw or "")
    md = run.generation_text(gid)
    output_file = output_sha = metrics = None
    if error is None:
        body = result.text.replace("\r\n", "\n")
        body = body if body.endswith("\n") else body + "\n"
        records.write_text(md, body)
        output_file = f"{records.GENERATIONS}/{gid}.md"
        output_sha = records.sha256_text(body)
        metrics = text_metrics(body)
    elif md.exists():
        md.unlink()
    record = {
        "schema": "dfa-eval/generation@1",
        "gen_id": gid,
        "prompt_id": gen["prompt_id"],
        "condition": gen["condition"],
        "generator": gen["generator"],
        "run_index": gen["run_index"],
        "seed": gen["seed"],
        "seed_honored": bool(provider.supports_seed and gen["seed"] is not None),
        "request": request,
        "status": "ok" if error is None else "error",
        "error": error,
        "output_file": output_file,
        "output_sha256": output_sha,
        "metrics": metrics,
        "turns": result.turns,
        "tool_calls": result.tool_calls,
        "skills_available": result.skills_available,
        "instructions_loaded": instructions_loaded(result, package, repo_root),
        "started_at": started,
        "finished_at": finished,
        "provider": provider.name,
        "model_requested": spec.get("model"),
        "models_used": list(result.models_used),
        "model_mismatch": mismatch,
        "usage": result.usage,
        "cost_usd": result.cost_usd,
        "latency_s": result.latency_s,
        "raw_file": f"{records.RAW}/{raw_id}.txt",
    }
    schema.check(record, schemas.GENERATION, f"generation record {gid}")  # bug guard
    records.write_json(run.generation_json(gid), record)
    return record


def _money(value):
    return "n/a" if value is None else f"${value:.2f}"


# --------------------------------------------------------------------------------------------
# The stage
# --------------------------------------------------------------------------------------------

def run_generate(run_dir, config, config_path, jobs, max_cost_usd, only_prompts=None,
                 retry_failed=False, dry_run=False, force_config=False, repo_root=None,
                 work_root=None, log=None):
    """Generate every missing plan of the matrix; return counts by outcome.

    `config` is a loaded config (config.load_config / check_config); `jobs` and `max_cost_usd`
    default to its limits when None. `dry_run` prints the matrix and call counts and touches
    nothing. Session workspaces live under `work_root` (default: the system temp dir), never in
    the repository.
    """
    log = log or default_log
    repo_root = Path(repo_root) if repo_root else ROOT
    schema.check(config, schemas.CONFIG, "config")
    limits = config["limits"]
    jobs = jobs or limits["jobs"]
    cap = max_cost_usd if max_cost_usd is not None else limits["max_cost_usd"]
    if run_dir is None and not dry_run:
        raise HarnessError("generate needs a run directory (--run DIR)")
    run = records.RunDir(run_dir) if run_dir is not None else None

    matrix = configmod.expand_generations(config)
    if only_prompts:
        known = {p["id"] for p in config["prompts"]}
        unknown = sorted(set(only_prompts) - known)
        if unknown:
            raise HarnessError(f"unknown prompt id(s): {', '.join(unknown)}")
        matrix = [g for g in matrix if g["prompt_id"] in set(only_prompts)]
    existing = {r["gen_id"]: r for r in run.generations()} if run and run.root.is_dir() else {}
    state = {}
    for gen in matrix:
        rec = existing.get(gen["gen_id"])
        state[gen["gen_id"]] = "todo" if rec is None else ("done" if rec["status"] == "ok"
                                                           else "failed")
    todo = [g for g in matrix if state[g["gen_id"]] == "todo"
            or (retry_failed and state[g["gen_id"]] == "failed")]
    counts = {"planned": len(matrix), "done": sum(v == "done" for v in state.values()),
              "failed": sum(v == "failed" for v in state.values()), "to_run": len(todo)}
    if dry_run:
        print_plan(config, config_path, run, matrix, state, counts, cap, retry_failed, log)
        return counts
    run_id = run_id_of(run_dir)

    models = configmod.by_id(config["generators"])
    instances = {gid: providers.make_provider(models[gid], repo_root)
                 for gid in sorted({g["generator"] for g in todo})}
    for provider in instances.values():
        provider.preflight()
    default_root = work_root is None
    work_root = default_work_root(run_id) if default_root else Path(work_root)
    check_work_root(work_root, repo_root)
    work_root.mkdir(parents=True, exist_ok=True)
    plugins_root = work_root / "plugins"
    try:
        packages = package_conditions(config, repo_root, plugins_root)
        open_run(run_dir, config, config_path, repo_root, packages, force_config, log)
        return _generate(run, run_id, config, todo, counts, jobs, cap, packages, models,
                         instances, repo_root, work_root, log)
    finally:
        shutil.rmtree(plugins_root, ignore_errors=True)
        remove_if_empty(work_root, also_parent=default_root)


def remove_if_empty(work_root, also_parent=False):
    """Tidy a stage's work root away once its sessions are done (and .../dfa-eval/ with it)."""
    for folder in (work_root, work_root.parent) if also_parent else (work_root,):
        try:
            folder.rmdir()
        except OSError:
            return


def _generate(run, run_id, config, todo, counts, jobs, cap, packages, models, instances,
              repo_root, work_root, log):
    """The call loop of run_generate, once the run directory and plugins exist."""
    limits = config["limits"]
    readme = config["workspace"]["readme"]
    readme_text = (repo_root / readme).read_bytes().decode("utf-8") if readme else None
    salt = f"workdir:{config['seed']}:{run_id}"
    names = {workspace.neutral_name(g["gen_id"], salt) for g in todo}
    if len(names) != len(todo):
        raise HarnessError("two generations hash to the same workspace name; change the seed")
    prompts = configmod.by_id(config["prompts"])
    tracker = CostTracker(cap, run_cost(run))
    if cap is not None and tracker.spent >= cap:
        log(f"cost cap {_money(cap)} already reached ({_money(tracker.spent)} spent)")
    timeout_s = limits["timeout_s"]

    def one(gen):
        gid = gen["gen_id"]
        if not tracker.allow():
            log(f"skip {gid}: cost cap {_money(cap)} reached ({_money(tracker.spent)} spent)")
            return "skipped"
        provider = instances[gen["generator"]]
        cond = config["conditions"][gen["condition"]]
        session_prompt, request = workspace.build_request(prompts[gen["prompt_id"]], cond,
                                                          provider.slash_commands)
        package = packages.get(gen["condition"])
        plugin_dirs = [package["plugin"]] if package else []
        system_append = None
        if package and not provider.slash_commands:
            system_append = workspace.render_instructions(
                package["plugin"] / "skills" / package["skill_name"])
        workdir = workspace.prepare_workdir(work_root, gid, readme_text, salt)
        started = now_utc()
        try:
            result = provider.generate(session_prompt, workdir, plugin_dirs, system_append,
                                       gen["seed"], timeout_s)
        except Exception as exc:  # recorded, so a provider bug is visible and retryable
            result = providers.CallResult(error=f"the provider raised {type(exc).__name__}: {exc}")
        finished = now_utc()
        leaked = contamination(result.skills_available, config, gen["condition"])
        if leaked and result.error is None:
            result.error = contamination_error(leaked, cond.get("skill_name"))
        if package and result.error is None:
            result.error = skill_missing(result.skills_available, package,
                                         instructions_loaded(result, package, repo_root))
        if package and result.error is None:
            result.error = skill_reads_denied(result.tool_calls, package)
        tracker.add(result.cost_usd)
        record = write_generation(run, gen, models[gen["generator"]], provider, request, result,
                                  package, started, finished, repo_root)
        shutil.rmtree(workdir, ignore_errors=True)
        if record["status"] == "ok":
            log(f"generate {gid}: ok, {record['metrics']['words']} words, "
                f"{_money(record['cost_usd'])}, {len(record['tool_calls'])} tool calls")
        else:
            log(f"generate {gid}: ERROR {record['error']}")
        return record["status"]

    stagger = 0.0 if all(p.name == "fake" for p in instances.values()) else STAGGER_S
    log(f"generate: {len(todo)} to run ({counts['done']} already done), {jobs} at a time"
        + (f", cost cap {_money(cap)}" if cap is not None else ""))
    outcomes = run_pool(todo, one, jobs, stagger)
    counts.update(ok=outcomes.count("ok"), error=outcomes.count("error"),
                  skipped_cost=outcomes.count("skipped"), spent_usd=round(tracker.spent, 6))
    log(f"generate: {counts['ok']} ok, {counts['error']} error, {counts['skipped_cost']} "
        f"skipped by the cost cap; {_money(tracker.spent)} spent in this run so far")
    if counts["error"]:
        log("re-run with --retry-failed to retry the failed generations")
    return counts


def print_plan(config, config_path, run, matrix, state, counts, cap, retry_failed, log):
    n_prompts = len({g["prompt_id"] for g in matrix})
    n_conditions = len({g["condition"] for g in matrix})
    n_generators = len({g["generator"] for g in matrix})
    if run is None:
        where = "none given"
    else:
        where = run.root.as_posix() + ("" if run.manifest_path.is_file() else " (new)")
    log(f"config {config['name']} ({config_path or 'given config'}); run directory: {where}")
    log(f"{n_prompts} prompts x {n_conditions} conditions x {n_generators} generator(s) x "
        f"{config['runs_per_cell']} runs = {len(matrix)} generations")
    width = max([len(g["gen_id"]) for g in matrix] + [6])
    for gen in matrix:
        log(f"  {gen['gen_id']:<{width}}  seed {gen['seed']:>10}  {state[gen['gen_id']]}")
    log(f"generation calls to make: {counts['to_run']} (done {counts['done']}, failed "
        f"{counts['failed']}{'' if retry_failed else '; --retry-failed redoes failed ones'})")
    rubrics, judges = config["rubrics"], config["judges"]
    repeats = config["judge_repeats"]
    # A judge may score only some rubrics (its `rubrics` field).
    pairs = sum(len([r for r in rubrics if not j.get("rubrics") or r in j["rubrics"]])
                for j in judges)
    judging = len(matrix) * pairs * repeats
    log(f"then about {judging} judge calls ({len(matrix)} plans x {pairs} judge-rubric pairs x "
        f"{repeats} repeats)")
    if config["probe"]["enabled"]:
        probes = len(matrix) * len(config["probe"]["judges"])
        log(f"and about {probes} leakage-probe calls")
    log("cost cap: " + (_money(cap) if cap is not None else "none") + "; no calls were made")
