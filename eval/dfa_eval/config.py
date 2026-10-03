"""Load and check a benchmark config, and expand it into the experiment matrix.

A config names everything a run depends on (prompts, conditions, models, rubrics, limits), and a
copy of it goes into the run manifest, so every check that can fail before money is spent fails
here, all at once: `ConfigError` lists every problem rather than the first. Paths in a config are
relative to the repository root.

Seeds are derived, never drawn: `derive_seed(base, key)` hashes the config seed with a stable key
(a gen_id, "judge-order"), so the same config always yields the same matrix, seeds and judge
order, on every machine.
"""

import copy
import hashlib
import json
from pathlib import Path

from . import records, schema, schemas, workspace

ROOT = Path(__file__).resolve().parents[2]
RUBRICS_DIR = Path("eval") / "rubrics"
NEEDS_MODEL = ("claude-cli", "openai-compatible")
DEFAULTS = {
    "judges": [],
    "rubrics": [],
    "judge_repeats": 1,
    "contrasts": [],
}
DEFAULT_SECTIONS = {
    "blinding": {"strip_self_score": True, "neutralize_terms_for": ["engineering-quality-v1"]},
    "probe": {"enabled": False, "judges": []},
    "workspace": {"readme": None},
    "limits": {"max_cost_usd": None, "jobs": 4, "timeout_s": 3600},
}


class ConfigError(ValueError):
    """A config that cannot be run; `problems` lists every reason."""

    def __init__(self, where, problems):
        self.problems = list(problems)
        count = len(self.problems)
        super().__init__(f"{where}: {count} problem{'' if count == 1 else 's'}:\n"
                         + "\n".join(f"  - {p}" for p in self.problems))


def derive_seed(base, key):
    """A 32-bit seed for `key` under config seed `base` (sha256, so stable everywhere)."""
    return int(hashlib.sha256(f"{base}:{key}".encode("utf-8")).hexdigest()[:8], 16)


def config_sha256(config):
    """sha256 of the canonical JSON dump (sorted keys, no whitespace) of a loaded config."""
    canonical = json.dumps(config, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                           allow_nan=False)
    return records.sha256_text(canonical)


def load_config(path, repo_root=None):
    """Read, validate and complete a config file; raise ConfigError listing every problem."""
    path = Path(path)
    try:
        data = json.loads(path.read_bytes().decode("utf-8"))
    except OSError as exc:
        raise ConfigError(path, [f"cannot read the file: {exc}"]) from None
    except ValueError as exc:
        raise ConfigError(path, [f"not valid JSON: {exc}"]) from None
    return check_config(data, repo_root, where=path.as_posix())


def check_config(data, repo_root=None, where="config"):
    """Validate a config dict (schema, then semantics) and return a copy with defaults applied."""
    root = Path(repo_root) if repo_root else ROOT
    errors = schema.validate(data, schemas.CONFIG)
    if errors:
        raise ConfigError(where, errors)
    problems = semantic_problems(data, root)
    if problems:
        raise ConfigError(where, problems)
    return apply_defaults(data)


def _duplicates(values):
    seen, dupes = set(), []
    for value in values:
        if value in seen and value not in dupes:
            dupes.append(value)
        seen.add(value)
    return dupes


def load_checklist(repo_root, rel):
    return json.loads((Path(repo_root) / rel).read_bytes().decode("utf-8"))


def semantic_problems(cfg, root):
    """Everything wrong with a schema-valid config that the schema cannot express."""
    problems = []
    prompts = cfg["prompts"]
    conditions = cfg["conditions"]
    generators = cfg["generators"]
    judges = cfg.get("judges", [])
    rubrics = cfg.get("rubrics", [])

    for what, ids in (("prompt", [p["id"] for p in prompts]),
                      ("generator", [g["id"] for g in generators]),
                      ("judge", [j["id"] for j in judges]),
                      ("rubric", list(rubrics))):
        for dupe in _duplicates(ids):
            problems.append(f"duplicate {what} id {dupe!r}")

    for cid, cond in conditions.items():
        if not records.ID_RE.match(cid) or ".." in cid:
            problems.append(f"condition id {cid!r} is not a valid id (letters, digits, '.', '_', "
                            "'-'; it becomes part of file names)")
        if cond["kind"] == "plain":
            if "skill_dir" in cond or "skill_name" in cond:
                problems.append(f"condition {cid!r} is plain but sets skill_dir/skill_name")
            continue
        if "skill_dir" not in cond or "skill_name" not in cond:
            problems.append(f"condition {cid!r} is a skill condition and needs both skill_dir and "
                            "skill_name")
            continue
        skill_dir = root / cond["skill_dir"]
        if not (skill_dir / "SKILL.md").is_file():
            problems.append(f"condition {cid!r}: skill_dir {cond['skill_dir']!r} has no SKILL.md")
            continue
        try:
            name, _ = workspace.read_skill(skill_dir)
            if name != cond["skill_name"]:
                problems.append(f"condition {cid!r}: {cond['skill_dir']}/SKILL.md is named "
                                f"{name!r}, not skill_name {cond['skill_name']!r}")
            else:
                workspace.package_contents(skill_dir, name)
        except ValueError as exc:
            problems.append(f"condition {cid!r}: {exc}")

    for n, contrast in enumerate(cfg.get("contrasts", [])):
        for arm in ("treatment", "control"):
            if contrast[arm] not in conditions:
                problems.append(f"contrasts[{n}]: {arm} {contrast[arm]!r} is not a condition")
        if contrast["treatment"] == contrast["control"]:
            problems.append(f"contrasts[{n}]: treatment and control are the same condition")
    pairs = [(c["treatment"], c["control"]) for c in cfg.get("contrasts", [])]
    for dupe in _duplicates(pairs):
        problems.append(f"contrast {dupe[0]} vs {dupe[1]} is listed more than once")

    for kind, models in (("generator", generators), ("judge", judges)):
        for model in models:
            label = f"{kind} {model['id']!r}"
            if model["provider"] in NEEDS_MODEL and not model.get("model"):
                problems.append(f"{label}: provider {model['provider']} needs a model (the harness "
                                "checks the model actually used against it)")
            if model["provider"] == "command":
                argv = (model.get("options") or {}).get("argv")
                if (not isinstance(argv, list) or not argv
                        or not all(isinstance(a, str) and a for a in argv)):
                    problems.append(f"{label}: provider command needs options.argv, a non-empty "
                                    "list of strings")
            if kind == "generator" and "conditions" in model:
                if not model["conditions"]:
                    problems.append(f"{label}: conditions is empty, so it would generate nothing")
                for cid in model["conditions"]:
                    if cid not in conditions:
                        problems.append(f"{label}: condition {cid!r} does not exist")
            if kind == "generator" and "rubrics" in model:
                problems.append(f"{label}: rubrics applies to judges only")
            if kind == "judge" and "conditions" in model:
                problems.append(f"{label}: conditions applies to generators only")
            if kind == "judge" and "rubrics" in model:
                if not model["rubrics"]:
                    problems.append(f"{label}: rubrics is empty, so it would judge nothing")
                for rubric_id in model["rubrics"]:
                    if rubric_id not in rubrics:
                        problems.append(f"{label}: rubric {rubric_id!r} is not in the config's "
                                        "rubrics")

    checklist_rubrics = []
    for rubric_id in rubrics:
        path = root / RUBRICS_DIR / f"{rubric_id}.json"
        if not records.ID_RE.match(rubric_id) or not path.is_file():
            problems.append(f"rubric {rubric_id!r}: {RUBRICS_DIR.as_posix()}/{rubric_id}.json does "
                            "not exist")
            continue
        try:
            rubric = json.loads(path.read_bytes().decode("utf-8"))
        except ValueError as exc:
            problems.append(f"rubric {rubric_id!r}: not valid JSON: {exc}")
            continue
        if not isinstance(rubric, dict) or rubric.get("id") != rubric_id:
            problems.append(f"rubric {rubric_id!r}: the file's id is "
                            f"{rubric.get('id') if isinstance(rubric, dict) else None!r}")
        elif rubric.get("uses_checklists"):
            checklist_rubrics.append(rubric_id)

    for prompt in prompts:
        rel = prompt.get("checklist")
        if rel is None:
            for rubric_id in checklist_rubrics:
                problems.append(f"prompt {prompt['id']!r} has no checklist, but rubric "
                                f"{rubric_id!r} scores one per prompt")
            continue
        if not (root / rel).is_file():
            problems.append(f"prompt {prompt['id']!r}: checklist {rel!r} does not exist")
            continue
        try:
            checklist = load_checklist(root, rel)
        except ValueError as exc:
            problems.append(f"prompt {prompt['id']!r}: checklist {rel!r} is not valid JSON: {exc}")
            continue
        if not isinstance(checklist, dict) or checklist.get("prompt_id") != prompt["id"]:
            found = checklist.get("prompt_id") if isinstance(checklist, dict) else None
            problems.append(f"prompt {prompt['id']!r}: checklist {rel!r} is for prompt {found!r}")
        elif not all(isinstance(checklist.get(k), list) for k in ("items", "traps")):
            problems.append(f"prompt {prompt['id']!r}: checklist {rel!r} needs items and traps "
                            "lists")

    judge_ids = {j["id"] for j in judges}
    for judge_id in (cfg.get("probe") or {}).get("judges", []):
        if judge_id not in judge_ids:
            problems.append(f"probe.judges: {judge_id!r} is not a configured judge")
    for rubric_id in (cfg.get("blinding") or {}).get("neutralize_terms_for", []):
        known = rubric_id in rubrics or (records.ID_RE.match(rubric_id)
                                         and (root / RUBRICS_DIR / f"{rubric_id}.json").is_file())
        if not known:
            problems.append(f"blinding.neutralize_terms_for: {rubric_id!r} is not a known rubric")
    readme = (cfg.get("workspace") or {}).get("readme")
    if readme is not None and not (root / readme).is_file():
        problems.append(f"workspace.readme: {readme!r} does not exist")
    return problems


def apply_defaults(cfg):
    """A deep copy of a validated config with every optional field filled in."""
    cfg = copy.deepcopy(cfg)
    for key, value in DEFAULTS.items():
        cfg.setdefault(key, copy.deepcopy(value))
    for key, value in DEFAULT_SECTIONS.items():
        cfg[key] = {**copy.deepcopy(value), **cfg.get(key, {})}
    for prompt in cfg["prompts"]:
        prompt.setdefault("checklist", None)
        prompt.setdefault("tags", [])
    for cond in cfg["conditions"].values():
        cond.setdefault("length_cap_words", None)
    for model in cfg["generators"] + cfg["judges"]:
        model.setdefault("model", None)
        model.setdefault("effort", None)
        model.setdefault("options", {})
    return cfg


def expand_generations(config):
    """Every (prompt, condition, generator, run) of the matrix, in config order, with its seed."""
    out = []
    for prompt in config["prompts"]:
        for cid in config["conditions"]:
            for gen in config["generators"]:
                if "conditions" in gen and cid not in gen["conditions"]:
                    continue
                for run_index in range(1, config["runs_per_cell"] + 1):
                    gid = records.gen_id(prompt["id"], cid, gen["id"], run_index)
                    out.append({"gen_id": gid, "prompt_id": prompt["id"], "condition": cid,
                                "generator": gen["id"], "run_index": run_index,
                                "seed": derive_seed(config["seed"], gid)})
    return out


def by_id(items):
    return {item["id"]: item for item in items}
