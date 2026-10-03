"""summary.json from a run's raw records: metrics per generation, then cells, conditions,
contrasts, judge reliability, leakage, length and cost. Deterministic, so CI recomputes the
committed summary and byte-compares it.

    aggregate(run_dir, rubrics_dir=None) -> dict   the summary (schemas.SUMMARY); writes nothing
    write_summary(run_dir) -> dict                  validates, then writes <run>/summary.json
    check_summary(run_dir) -> (ok, diff)            recomputes; diff against the committed file

Judgments are joined to generations through blind/key.json. Only ok generations and ok
judgments and probes count. Everything else is counted in `generated_from` (by status) and
named in `flags`, so a sparse or partly failed run still aggregates and says what is missing.
Records that fail their schema are skipped and flagged rather than trusted. A judgment or probe
that records `blind_sha256` (the blind copy it scored) is excluded and flagged when that differs
from the key's sha256: the copy was re-blinded after it was scored.

Rubric metadata (dimension count, scale, which engineering-quality dimensions overlap the
methodology) is read from eval/rubrics/<id>.json, the one input outside the run directory; a
flag says so if the file no longer matches the rubric_sha256 the judgments recorded.

Choices the spec leaves open, fixed here so reruns agree:
- The headline judged metrics (adherence_*, eq_total, eq_core, eq_dim_*, checklist_coverage,
  traps_present and their per-1k-token ratios) are complete-case: a generation counts for a
  rubric's metrics only if every judge the config assigns to that rubric (a judge's `rubrics`,
  when set, must list it) has an ok judgment of it for every repeat. Averaging whichever judges
  succeeded would shift an arm's mean by judge severity whenever one judge fails more often on
  that arm. The generations left out are flagged per rubric, condition and generator, and a
  contrast notes when an arm lost some. The per-judge eq_total@<judge> metrics keep every ok
  judgment of their judge. A judgment from a rubric, judge or repeat the config does not ask for
  is excluded and flagged.
- adherence_total is the v1.0.0 method: per-dimension median across the generation's ok
  judgments (every judge x repeat), summed. With an even number of judgments a median can be
  x.5. The other judged metrics are means over judgments.
- Contrast effect sizes at generation level (hedges_g, cliffs_delta) use the generations of the
  prompts where both arms have data, the same prompts the paired differences use.
- The cluster bootstrap seed is derive_seed(run seed, "contrast:<treatment>:<control>:<generator>")
  with derive_seed as in config.py (first 8 hex digits of sha256("<base>:<key>")).
- Leakage accuracy counts p_methodology > 0.5 as a "methodology" guess (0.5 itself as "none").
- Cap compliance compares exactly: words * 10 <= cap * 11.
- Cost sums every record that passed its schema, whatever its status: failed calls cost money.
  Records that a retry replaced, kept as JSON anywhere under <run>/superseded/, are paid calls
  too: the cost block counts them as "superseded" and in the total, and nothing else reads them.
"""

import difflib
import hashlib
import json
import math
import statistics
from pathlib import Path

from . import config as configmod
from . import records, schema, schemas, stats

RUBRICS_DIR = Path(__file__).resolve().parents[2] / "eval" / "rubrics"
BOOTSTRAP_RESAMPLES = 10000
CONTRAST_METRICS = ("adherence_total", "eq_total", "eq_core", "checklist_coverage",
                    "traps_present", "lint_score", "words", "eq_per_1k_tokens", "cost_usd")
ROLE_CATEGORY = {"adherence": "methodology-adherence", "eq": "engineering-quality"}
USAGE_KEYS = ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_input_tokens",
              "cache_creation_input_tokens")
POOLED_NOTE = ("pooled: generations of one prompt are not independent; the prompt-level "
               "statistics are the ones to read")
LEAKAGE_NOTE = ("AUC 0.5 = judges cannot tell; 1.0 = fully identifiable. Accuracy counts "
                "p_methodology > 0.5 as a 'methodology' guess; it is n/a when both arms used a "
                "skill, where only the AUC is meaningful.")
COMPLETE_CASE = "; a generation counts only if every configured judge x repeat scored it ok"
# Records that a retry replaced: <run>/superseded/**/*.json, read for their cost only.
SUPERSEDED = "superseded"
CALL_RECORD_SCHEMAS = {s["properties"]["schema"]["const"]: s
                       for s in (schemas.GENERATION, schemas.JUDGMENT, schemas.PROBE)}


def derive_seed(base, key):
    """Same derivation as config.derive_seed: a stable 32-bit seed per (base seed, key)."""
    return int(hashlib.sha256(f"{base}:{key}".encode("utf-8")).hexdigest()[:8], 16)


# --- loading --------------------------------------------------------------------------------

def _load_folder(folder, record_schema):
    """(valid records, names of files that are not valid JSON records), in file-name order."""
    valid, invalid = [], []
    if not folder.is_dir():
        return valid, invalid
    for path in sorted(folder.glob("*.json"), key=lambda p: p.name):
        if path.name == records.BLIND_KEY:
            continue
        try:
            data = records.read_json(path)
        except ValueError:
            invalid.append(path.name)
            continue
        if schema.validate(data, record_schema):
            invalid.append(path.name)
        else:
            valid.append(data)
    return valid, invalid


def _load_superseded(folder):
    """(valid generation, judgment and probe records, POSIX paths relative to `folder` of the
    other JSON files) found anywhere under `folder`, in path order. No folder: neither."""
    valid, invalid = [], []
    if not folder.is_dir():
        return valid, invalid
    # str order of the relative POSIX path, not Path order (case-insensitive on Windows).
    # superseded/outcomes/ holds outcome attempts (and their code), costed by outcomes.py.
    found = sorted((path.relative_to(folder).as_posix(), path) for path in folder.rglob("*.json")
                   if path.is_file() and path.relative_to(folder).parts[0] != "outcomes")
    for name, path in found:
        try:
            data = records.read_json(path)
        except (OSError, ValueError):
            invalid.append(name)
            continue
        kind = data.get("schema") if isinstance(data, dict) else None
        record_schema = CALL_RECORD_SCHEMAS.get(kind) if isinstance(kind, str) else None
        if record_schema is None or schema.validate(data, record_schema):
            invalid.append(name)
        else:
            valid.append(data)
    return valid, invalid


def _load_rubric(rubrics_dir, rubric_id):
    path = Path(rubrics_dir) / f"{rubric_id}.json"
    if not path.is_file():
        return None, None
    return records.read_json(path), records.sha256_file(path)


class _Run:
    """Everything aggregate needs from a run directory, loaded once and validated."""

    def __init__(self, run_dir, rubrics_dir):
        self.dir = records.RunDir(run_dir)
        self.manifest = schema.check(self.dir.manifest(), schemas.MANIFEST, "manifest.json")
        self.config = schema.check(self.manifest["config"], schemas.CONFIG, "manifest.json config")
        self.flags = []
        self.gens, bad_gens = _load_folder(self.dir.root / records.GENERATIONS, schemas.GENERATION)
        self.judgments, bad_judgments = _load_folder(self.dir.root / records.JUDGMENTS,
                                                     schemas.JUDGMENT)
        self.probes, bad_probes = _load_folder(self.dir.root / records.PROBES, schemas.PROBE)
        self.lints, bad_lints = _load_folder(self.dir.root / records.LINT, schemas.LINT)
        self.invalid = {"generations": bad_gens, "judgments": bad_judgments,
                        "probes": bad_probes, "lint": bad_lints}
        for what, names in self.invalid.items():
            if names:
                self.flags.append(f"skipped {len(names)} {what} file(s) that are not valid "
                                  f"records: {', '.join(names)}")
        # Only the cost block reads these: a replaced call was paid for, but it is not data.
        self.superseded, bad_superseded = _load_superseded(self.dir.root / SUPERSEDED)
        if bad_superseded:
            self.flags.append(f"skipped {len(bad_superseded)} file(s) under {SUPERSEDED}/ that "
                              "are not valid generation, judgment or probe records (their cost "
                              f"is not counted): {', '.join(bad_superseded)}")
        self.key = None
        if self.dir.blind_key_path.exists():
            try:
                key = records.read_json(self.dir.blind_key_path)
            except ValueError as exc:
                key = {"unreadable": str(exc)}
            errors = schema.validate(key, schemas.BLIND_KEY)
            if errors:
                self.flags.append(f"blind/key.json is not a valid blind key ({errors[0]}); "
                                  "no judgment or probe can be joined")
            else:
                self.key = {e["blind_id"]: e for e in key["entries"]}

        self.prompts = self.config["prompts"]
        self.prompt_ids = [p["id"] for p in self.prompts]
        self.conditions = self.config["conditions"]
        self.generators = self.config["generators"]
        self.judges = self.config.get("judges", [])
        self.repeats = self.config.get("judge_repeats", 1)
        self.contrasts = self.config.get("contrasts", [])
        self.probe_cfg = self.config.get("probe", {"enabled": False, "judges": []})

        self.rubrics = {}  # role -> {"id", "rubric", "sha256"}
        for rid in self.config.get("rubrics", []):
            rubric, sha = _load_rubric(rubrics_dir, rid)
            if rubric is None:
                self.flags.append(f"rubric {rid}: eval/rubrics/{rid}.json not found; its "
                                  "judgments are not summarized")
                continue
            role = next((r for r, c in ROLE_CATEGORY.items() if rubric.get("category") == c), None)
            if role is None or role in self.rubrics:
                self.flags.append(f"rubric {rid} (category {rubric.get('category')}) is not "
                                  "summarized: only the first methodology-adherence and the "
                                  "first engineering-quality rubric are")
                continue
            self.rubrics[role] = {"id": rid, "rubric": rubric, "sha256": sha}

    def allowed(self, generator, condition):
        subset = generator.get("conditions")
        return not subset or condition in subset

    def cell_keys(self):
        return [(p, c, g["id"]) for p in self.prompt_ids for c in self.conditions
                for g in self.generators if self.allowed(g, c)]

    def coders(self, rubric_id):
        """The (judge, repeat) pairs the config asks to score `rubric_id`, in config order: every
        judge without a `rubrics` subset or whose subset lists it, times every repeat."""
        return [(j["id"], k) for j in self.judges
                if not j.get("rubrics") or rubric_id in j["rubrics"]
                for k in range(1, self.repeats + 1)]


# --- per-generation metrics -----------------------------------------------------------------

def _registry(run):
    reg = {}

    def add(name, definition, lo, hi, category):
        reg[name] = {"definition": definition, "scale": {"min": lo, "max": hi},
                     "category": category}

    adherence = run.rubrics.get("adherence")
    if adherence:
        r = adherence["rubric"]
        n, top = len(r["dimensions"]), r["scale"]["max"]
        add("adherence_total", f"{adherence['id']}: per-dimension median across the "
            "generation's ok judgments (all judges x repeats), summed (the v1.0.0 method)"
            + COMPLETE_CASE, 0, n * top, "methodology-adherence")
        add("adherence_mean", f"{adherence['id']}: mean of the generation's ok judgment totals"
            + COMPLETE_CASE, 0, n * top, "methodology-adherence")
    eq = run.rubrics.get("eq")
    if eq:
        r = eq["rubric"]
        dims, top = r["dimensions"], r["scale"]["max"]
        core = [d for d in dims if not d.get("overlaps_methodology")]
        add("eq_total", f"{eq['id']}: mean of the generation's ok judgment totals" + COMPLETE_CASE,
            0, len(dims) * top, "engineering-quality")
        add("eq_core", f"{eq['id']}: mean judgment total without the dimensions flagged "
            "overlaps_methodology (" + ", ".join(d["name"] for d in dims
                                                 if d.get("overlaps_methodology")) + ")"
            + COMPLETE_CASE, 0, len(core) * top, "engineering-quality")
        for d in dims:
            add(f"eq_dim_{d['key']}", f"{eq['id']} dimension {d['id']} ({d['name']}): mean "
                "score across the generation's ok judgments" + COMPLETE_CASE, 0, top,
                "engineering-quality")
        for judge in eq_judges(run):
            add(f"eq_total@{judge}", f"{eq['id']}: mean of judge {judge}'s ok judgment totals "
                "only (does a difference hold for each judge?); every generation that judge "
                "scored ok counts", 0, len(dims) * top, "engineering-quality")
        if r.get("uses_checklists"):
            add("checklist_coverage", "mean over judgments of sum(item status) / (2 x items) "
                "on the prompt's checklist" + COMPLETE_CASE, 0, 1, "engineering-quality")
            add("traps_present", "mean over judgments of the number of the prompt's traps "
                "marked present" + COMPLETE_CASE, 0, None, "engineering-quality")
    add("lint_score", "share of plan-template v2 format checks passed (lint.py); format, not "
        "quality", 0, 1, "structure")
    add("words", "words in the plan (whitespace-separated)", 0, None, "length")
    add("visible_tokens_est", "ceil(characters / 4) of the plan", 0, None, "length")
    add("output_tokens", "output tokens billed by the provider (includes thinking when the "
        "provider counts it)", 0, None, "cost")
    add("cost_usd", "generation cost reported by the provider, USD", 0, None, "cost")
    add("latency_s", "generation wall-clock time measured by the harness, seconds", 0, None,
        "cost")
    if eq:
        add("eq_per_1k_tokens", "eq_total / (visible_tokens_est / 1000)", 0, None, "length")
    if adherence:
        add("adherence_per_1k_tokens", "adherence_total / (visible_tokens_est / 1000)", 0, None,
            "length")
    return reg


def eq_judges(run):
    """Judges that score the engineering-quality rubric, when there is more than one of them."""
    eq = run.rubrics.get("eq")
    if not eq:
        return []
    ids = [j["id"] for j in run.judges if not j.get("rubrics") or eq["id"] in j["rubrics"]]
    return ids if len(ids) > 1 else []


def _scores(judgment):
    return {s["dim"]: s["score"] for s in judgment["scores"]}


def _adherence(js, n_dims):
    if not js:
        return None, None
    per_judgment = [_scores(j) for j in js]
    medians = []
    for dim in range(1, n_dims + 1):
        values = [s[dim] for s in per_judgment if dim in s]
        if not values:
            return None, stats.mean([sum(s.values()) for s in per_judgment])
        medians.append(statistics.median(values))
    return sum(medians), stats.mean([sum(s.values()) for s in per_judgment])


def _eq(js, rubric):
    out = {}
    dims = rubric["dimensions"]
    overlap = {d["id"] for d in dims if d.get("overlaps_methodology")}
    per_judgment = [_scores(j) for j in js]
    out["eq_total"] = stats.mean([sum(s.values()) for s in per_judgment])
    out["eq_core"] = stats.mean([sum(v for k, v in s.items() if k not in overlap)
                                 for s in per_judgment])
    for d in dims:
        out[f"eq_dim_{d['key']}"] = stats.mean([s[d["id"]] for s in per_judgment if d["id"] in s])
    if rubric.get("uses_checklists"):
        coverage = [sum(i["status"] for i in j["checklist"]) / (2 * len(j["checklist"]))
                    for j in js if j.get("checklist")]
        traps = [sum(1 for t in j["traps"] if t["present"]) for j in js
                 if j.get("traps") is not None]
        out["checklist_coverage"] = stats.mean(coverage)
        out["traps_present"] = stats.mean(traps)
    return out


def _per_1k(value, tokens):
    if value is None or not tokens:
        return None
    return value / (tokens / 1000)


def _generation_metrics(run, gen, complete, judged, lint, registry):
    """Every registry metric of one ok generation (None where it has no value).

    `judged` maps a rubric id to the generation's usable ok judgments; `complete` has the same
    lists, but only for the rubrics that every configured judge x repeat scored. The headline
    judged metrics read `complete`; the per-judge eq_total@<judge> metrics read `judged`.
    """
    metrics = gen.get("metrics") or {}
    out = {name: None for name in registry}
    adherence = run.rubrics.get("adherence")
    if adherence:
        n_dims = len(adherence["rubric"]["dimensions"])
        out["adherence_total"], out["adherence_mean"] = _adherence(
            complete.get(adherence["id"], []), n_dims)
    eq = run.rubrics.get("eq")
    if eq and complete.get(eq["id"]):
        out.update(_eq(complete[eq["id"]], eq["rubric"]))
    if eq:
        for judge in eq_judges(run):
            out[f"eq_total@{judge}"] = stats.mean([sum(_scores(j).values())
                                                   for j in judged.get(eq["id"], [])
                                                   if j["judge"] == judge])
    out["lint_score"] = lint["score"] if lint else None
    out["words"] = metrics.get("words")
    out["visible_tokens_est"] = metrics.get("visible_tokens_est")
    out["output_tokens"] = (gen.get("usage") or {}).get("output_tokens")
    out["cost_usd"] = gen.get("cost_usd")
    out["latency_s"] = gen.get("latency_s")
    if "eq_per_1k_tokens" in registry:
        out["eq_per_1k_tokens"] = _per_1k(out["eq_total"], out["visible_tokens_est"])
    if "adherence_per_1k_tokens" in registry:
        out["adherence_per_1k_tokens"] = _per_1k(out["adherence_total"], out["visible_tokens_est"])
    return {name: out[name] for name in registry}


# --- sections -------------------------------------------------------------------------------

def _describe_metrics(values_by_gen, gids, registry):
    return {m: stats.describe([values_by_gen[g][m] for g in gids]) for m in registry}


def _cells(run, ok_by_cell, failed_by_cell, values, registry):
    out = []
    for p, c, g in run.cell_keys():
        gids = ok_by_cell.get((p, c, g), [])
        out.append({"prompt": p, "condition": c, "generator": g, "n_generations": len(gids),
                    "n_failed": len(failed_by_cell.get((p, c, g), [])),
                    "metrics": _describe_metrics(values, gids, registry)})
    return out


def _conditions(run, cells, ok_by_cell, values, registry):
    out = []
    for c, cond in run.conditions.items():
        for g in run.generators:
            if not run.allowed(g, c):
                continue
            mine = [cell for cell in cells
                    if cell["condition"] == c and cell["generator"] == g["id"]]
            gids = [gid for p in run.prompt_ids for gid in ok_by_cell.get((p, c, g["id"]), [])]
            metrics = {}
            for m in registry:
                prompt_means = [cell["metrics"][m]["mean"] for cell in mine
                                if cell["metrics"][m]["mean"] is not None]
                metrics[m] = {"prompt_means": stats.describe(prompt_means),
                              "pooled": stats.describe([values[gid][m] for gid in gids])}
            out.append({"condition": c, "generator": g["id"], "kind": cond["kind"],
                        "length_cap_words": cond.get("length_cap_words"),
                        "n_prompts": sum(1 for cell in mine if cell["n_generations"]),
                        "n_generations": len(gids), "note": POOLED_NOTE, "metrics": metrics})
    return out


def _judge_based(metric):
    return metric.startswith(("adherence", "eq_", "checklist", "traps"))


def _metric_rubric(run, metric):
    """Id of the rubric whose complete-case rule a headline metric follows; None for metrics
    that are not judged and for the per-judge eq_total@<judge> metrics."""
    if "@" in metric or not _judge_based(metric):
        return None
    info = run.rubrics.get("adherence" if metric.startswith("adherence") else "eq")
    return info["id"] if info else None


def _contrasts(run, ok_by_cell, values, registry, same_model_generators, lost):
    out = []
    seed = run.manifest["seed"]
    for contrast in run.contrasts:
        t, c = contrast["treatment"], contrast["control"]
        if t not in run.conditions or c not in run.conditions:
            run.flags.append(f"contrast {t} vs {c} names a condition that is not in the config; "
                             "skipped")
            continue
        for g in run.generators:
            if not (run.allowed(g, t) and run.allowed(g, c)):
                continue
            bootstrap_seed = derive_seed(seed, f"contrast:{t}:{c}:{g['id']}")
            per_judge = [m for m in registry if m.startswith("eq_total@")]
            for m in list(CONTRAST_METRICS) + per_judge:
                if m in registry:
                    out.append(_contrast(run, t, c, g["id"], m, ok_by_cell, values, registry,
                                         bootstrap_seed, same_model_generators.get(g["id"], []),
                                         lost))
    return out


def _lost_note(run, t, c, g, m, lost):
    """The note for an arm that lost generations to incomplete judging, or None."""
    rid = _metric_rubric(run, m)
    if rid is None:
        return None
    parts = []
    for arm, condition in (("treatment", t), ("control", c)):
        n_lost, n_ok = lost.get((rid, condition, g), (0, 0))
        if n_lost:
            parts.append(f"{n_lost} of {n_ok} {arm}")
    if not parts:
        return None
    return (f"incomplete judging left out {' and '.join(parts)} generation(s): not every "
            f"configured judge x repeat scored them ({rid})")


def _contrast(run, t, c, g, m, ok_by_cell, values, registry, bootstrap_seed, same_model, lost):
    per_prompt, groups, missing, t_all, c_all = [], {}, [], [], []
    for p in run.prompt_ids:
        tv = [values[gid][m] for gid in ok_by_cell.get((p, t, g), [])
              if values[gid][m] is not None]
        cv = [values[gid][m] for gid in ok_by_cell.get((p, c, g), [])
              if values[gid][m] is not None]
        if not tv or not cv:
            missing.append(p)
            continue
        tm, cm = stats.mean(tv), stats.mean(cv)
        per_prompt.append({"prompt": p, "treatment_mean": tm, "control_mean": cm,
                           "diff": tm - cm, "n_treatment": len(tv), "n_control": len(cv)})
        groups[p] = (tv, cv)
        t_all += tv
        c_all += cv
    diffs = [row["diff"] for row in per_prompt]
    described = stats.describe(diffs)
    interval = stats.cluster_bootstrap_diff(groups, n_resamples=BOOTSTRAP_RESAMPLES,
                                            seed=bootstrap_seed) if groups else None
    notes = []
    if not per_prompt:
        notes.append("no prompt has data for both arms")
    elif missing:
        notes.append("missing an arm on: " + ", ".join(missing))
    lost_note = _lost_note(run, t, c, g, m, lost)
    if lost_note:
        notes.append(lost_note)
    if len(per_prompt) < 5:
        notes.append("n_prompts < 5: interval is wide")
    top = registry[m]["scale"]["max"]
    ceiling = stats.ceiling_fraction(t_all, top) if top is not None else None
    if ceiling:
        notes.append(f"ceiling: {100 * ceiling:.0f}% of treatment generations at max")
    hedges_g = stats.stratified_hedges_g(groups)
    if described["sd"] == 0:
        notes.append("no variation in the per-prompt differences: dz undefined")
    if per_prompt and hedges_g is None:
        notes.append("no variation within prompts: g undefined")
    # same_model: the judges that run on this generator's model. A per-judge metric carries the
    # note only if its own judge is one of them.
    own_judge = m.split("@", 1)[1] if m.startswith("eq_total@") else None
    if _judge_based(m) and same_model and (own_judge is None or own_judge in same_model):
        notes.append("judge model also generated these plans")
    return {
        "treatment": t, "control": c, "generator": g, "metric": m,
        "n_prompts": len(per_prompt), "n_treatment": len(t_all), "n_control": len(c_all),
        "mean_diff": described["mean"], "diff": described,
        "dz": stats.paired_dz(diffs),
        # Within-prompt effect sizes: unequal cell sizes cannot mix prompt difficulty into them.
        "hedges_g": hedges_g,
        "cliffs_delta": stats.stratified_cliffs_delta(groups),
        "cluster_bootstrap_ci": list(interval) if interval else None,
        "bootstrap": {"resamples": BOOTSTRAP_RESAMPLES, "seed": bootstrap_seed},
        "per_prompt": per_prompt, "notes": notes,
    }


def _reliability(run, judged_by_gen, ok_gids):
    out = []
    for role in ("adherence", "eq"):
        info = run.rubrics.get(role)
        if not info:
            continue
        rid = info["id"]
        totals = {}  # gid -> {(judge, repeat): total}
        for gid in ok_gids:
            for j in judged_by_gen.get(gid, {}).get(rid, []):
                totals.setdefault(gid, {})[(j["judge"], j["repeat"])] = sum(
                    s["score"] for s in j["scores"])
        order = {j["id"]: k for k, j in enumerate(run.judges)}
        coders = sorted({coder for t in totals.values() for coder in t},
                        key=lambda jr: (order.get(jr[0], len(order)), jr[0], jr[1]))
        units = [[totals[gid].get(coder) for coder in coders] for gid in sorted(totals)]
        gaps = [abs(a - b) for unit in units for i, a in enumerate(unit) if a is not None
                for b in unit[i + 1:] if b is not None]
        per_judge = []
        for judge in sorted({jr[0] for jr in coders}, key=lambda j: (order.get(j, len(order)), j)):
            mine = [v for t in totals.values() for (j, _), v in t.items() if j == judge]
            per_judge.append({"judge": judge, "n": len(mine), "mean_total": stats.mean(mine)})
        notes = ["coders are (judge, repeat) pairs; units are generations"]
        if len(coders) < 2:
            notes.append("fewer than 2 coders: agreement cannot be estimated")
        # Alpha measures absolute agreement, so one judge scoring every plan a few points higher
        # lowers it even when both judges order the plans alike; rank consistency separates the two.
        consistency = []
        for i, a_coder in enumerate(coders):
            for b_coder in coders[i + 1:]:
                pairs = [(totals[g][a_coder], totals[g][b_coder]) for g in sorted(totals)
                         if a_coder in totals[g] and b_coder in totals[g]]
                consistency.append({
                    "coders": [f"{a_coder[0]}#{a_coder[1]}", f"{b_coder[0]}#{b_coder[1]}"],
                    "n": len(pairs),
                    "spearman": stats.spearman([x for x, _ in pairs], [y for _, y in pairs]),
                    "mean_offset": stats.mean([y - x for x, y in pairs]),
                })
        out.append({
            "rubric": rid, "coders": [f"{j}#{r}" for j, r in coders], "n_coders": len(coders),
            "n_units": sum(1 for unit in units if sum(v is not None for v in unit) >= 2),
            "alpha_interval": stats.krippendorff_alpha(units, "interval"),
            "mean_abs_diff": stats.mean(gaps), "rank_consistency": consistency,
            "per_judge": per_judge, "notes": notes,
        })
    return out


def _discrimination(pos, neg):
    n = len(pos) + len(neg)
    correct = sum(1 for p in pos if p > 0.5) + sum(1 for p in neg if p <= 0.5)
    return {"n": n, "n_positive": len(pos), "n_negative": len(neg), "auc": stats.auc(pos, neg),
            "accuracy": correct / n if n else None,
            "accuracy_ci95": list(stats.wilson_ci(correct, n)) if n else None}


def _leakage(run, gen_by_id, probes):
    """Per probe judge and generator: can the probe tell skill plans from plain ones?

    `probes` are the probe records to use: the run's, without those that scored an earlier
    blind copy. Probes are grouped by generator as well as condition, so differences between
    generators' styles do not count as identifiability. Accuracy at 0.5 is reported only when one
    arm is plain: if both arms used a skill, a correct "followed a methodology" guess on the
    control would count as an error, so only the AUC is meaningful there.
    """
    judges = list(run.probe_cfg.get("judges", []))
    judges += sorted({p["judge"] for p in probes} - set(judges))
    if not judges or (not run.probe_cfg.get("enabled") and not probes):
        return []
    kinds = {c: cond["kind"] for c, cond in run.conditions.items()}
    out = []
    for judge in judges:
        mine = [p for p in probes if p["judge"] == judge]
        mapped = {}  # probe id -> generation, for every probe that joins
        for probe in mine:
            entry = (run.key or {}).get(probe["blind_id"])
            gen = gen_by_id.get(entry["gen_id"]) if entry else None
            if gen is not None:
                mapped[probe["probe_id"]] = gen
        for g in run.generators:
            by_condition = {}
            for probe in mine:
                gen = mapped.get(probe["probe_id"])
                if (gen is None or gen["generator"] != g["id"] or probe["status"] != "ok"
                        or probe["p_methodology"] is None):
                    continue
                by_condition.setdefault(gen["condition"], []).append(probe["p_methodology"])
            pos = [p for c, ps in sorted(by_condition.items()) if kinds.get(c) == "skill"
                   for p in ps]
            neg = [p for c, ps in sorted(by_condition.items()) if kinds.get(c) == "plain"
                   for p in ps]
            contrasts = []
            for ct in run.contrasts:
                if not (run.allowed(g, ct["treatment"]) and run.allowed(g, ct["control"])):
                    continue
                d = _discrimination(by_condition.get(ct["treatment"], []),
                                    by_condition.get(ct["control"], []))
                if kinds.get(ct["treatment"]) == "skill" and kinds.get(ct["control"]) == "skill":
                    d["accuracy"], d["accuracy_ci95"] = None, None
                contrasts.append(dict({"treatment": ct["treatment"], "control": ct["control"]},
                                      **d))
            out.append({
                "judge": judge, "generator": g["id"],
                "n_probes": sum(1 for p in mine
                                if (mapped.get(p["probe_id"]) or {}).get("generator") == g["id"]),
                "n_ok": sum(len(ps) for ps in by_condition.values()),
                "skill_vs_plain": _discrimination(pos, neg),
                "contrasts": contrasts,
                "note": LEAKAGE_NOTE,
            })
    return out


def _length(run, ok_by_cell, values):
    out = []
    for c, cond in run.conditions.items():
        for g in run.generators:
            if not run.allowed(g, c):
                continue
            gids = [gid for p in run.prompt_ids for gid in ok_by_cell.get((p, c, g["id"]), [])]
            words = [values[gid]["words"] for gid in gids]
            entry = {"condition": c, "generator": g["id"], "n": len(gids),
                     "words": stats.describe(words)}
            for metric in ("eq_total", "adherence_total"):
                ys = [values[gid].get(metric) for gid in gids]
                entry[f"spearman_words_{metric}"] = stats.spearman(words, ys)
            cap = cond.get("length_cap_words")
            within = sum(1 for w in words if w is not None and w * 10 <= cap * 11) if cap else None
            entry.update({"length_cap_words": cap, "within_cap": within,
                          "cap_compliance": within / len(gids) if cap and gids else None})
            out.append(entry)
    return out


def _cost_block(recs):
    known = [r["cost_usd"] for r in recs if r.get("cost_usd") is not None]
    block = {"n_calls": len(recs), "cost_usd": math.fsum(known),
             "n_unknown_cost": len(recs) - len(known),
             "n_unknown_usage": sum(1 for r in recs if r.get("usage") is None)}
    for key in USAGE_KEYS:
        block[key] = sum((r.get("usage") or {}).get(key) or 0 for r in recs)
    return block


def _cost(run):
    return {"generation": _cost_block(run.gens), "judging": _cost_block(run.judgments),
            "probe": _cost_block(run.probes), "superseded": _cost_block(run.superseded),
            "total": _cost_block(run.gens + run.judgments + run.probes + run.superseded)}


def _status_counts(recs, statuses, invalid):
    counts = {s: sum(1 for r in recs if r.get("status") == s) for s in statuses}
    counts["not_a_valid_record"] = len(invalid)
    return counts


def _ids(items):
    return ", ".join(sorted(items))


# --- entry points ---------------------------------------------------------------------------

def _provenance_flags(run):
    """What the summary must disclose about how the plans came to be: manifest notes (e.g. a
    config replaced with --force-config), planned plans with no record, and a condition whose
    plans loaded different versions of the same instruction file."""
    flags = [f"manifest note: {line}" for line in
             (run.manifest.get("notes") or "").splitlines() if line.strip()]
    recorded = {g["gen_id"] for g in run.gens}
    planned = [g["gen_id"] for g in configmod.expand_generations(run.config)]
    missing = sorted(gid for gid in planned if gid not in recorded)
    if missing:
        flags.append(f"{len(missing)} of {len(planned)} planned generation(s) have no record "
                     f"(not run, or removed): {_ids(missing)}")
    started = run.manifest.get("condition_files") or {}
    for condition in sorted(run.config.get("conditions", {})):
        versions = {}
        for gen in run.gens:
            if gen["condition"] == condition and gen["status"] == "ok":
                for item in gen.get("instructions_loaded") or []:
                    versions.setdefault(item["path"], set()).add(item["sha256"])
        for path, shas in sorted(versions.items()):
            at_start = (started.get(condition) or {}).get(path)
            if len(shas) > 1:
                flags.append(f"condition {condition}: its plans loaded {path} in {len(shas)} "
                             "versions (the file changed during the run), so the condition mixes "
                             "instruction versions")
            elif at_start and shas != {at_start}:
                flags.append(f"condition {condition}: its plans loaded a version of {path} other "
                             "than the one recorded when the run started")
    flags += _denied_skill_reads(run)
    flags += _session_settings(run)
    return flags


def _denied_skill_reads(run):
    """Skill plans written without the skill's reference files: every Read the session made
    inside the skill's directory failed (e.g. a permission mode that refused them)."""
    flags = []
    for condition, spec in sorted(run.config.get("conditions", {}).items()):
        if spec.get("kind") != "skill":
            continue
        marker = f"/skills/{spec.get('skill_name')}/"
        per_generator = {}
        for gen in run.gens:
            if gen["condition"] != condition or gen["status"] != "ok":
                continue
            reads = [c for c in gen.get("tool_calls") or [] if c.get("tool") == "Read"
                     and marker in "/" + (c.get("path") or "")  # files, not directories
                     and "." in (c.get("path") or "").rstrip("/").rsplit("/", 1)[-1]]
            counts = per_generator.setdefault(gen["generator"], [0, 0, 0])
            counts[0] += 1
            if reads and not any(c.get("ok") for c in reads):
                counts[1] += 1
                counts[2] += len(reads)
        for generator, (plans, denied, n_reads) in sorted(per_generator.items()):
            if denied:
                flags.append(f"condition {condition}, generator {generator}: {denied} of {plans} "
                             f"plans could not read the skill's files (all {n_reads} reads of "
                             "them failed), so they follow SKILL.md alone, not the skill as "
                             "shipped")
    return flags


def _session_settings(run):
    """The output style and permission mode each claude-cli generation session reported at
    start: user settings still apply under --bare, so they are part of the experiment."""
    styles, modes = {}, {}
    for gen in run.gens:
        if gen.get("provider") != "claude-cli" or gen["status"] != "ok" or not gen.get("raw_file"):
            continue
        init = init_event(run.dir.root / gen["raw_file"])
        style = init.get("output_style") or "unknown"
        mode = init.get("permissionMode") or "unknown"
        styles[style] = styles.get(style, 0) + 1
        modes.setdefault(mode, {}).setdefault(gen["generator"], 0)
        modes[mode][gen["generator"]] += 1
    flags = []
    if styles and set(styles) != {"default"}:
        flags.append("generation sessions ran with output style " + ", ".join(
            f"{style!r} ({n} plans)" for style, n in sorted(styles.items()))
            + " (the operator's Claude Code settings apply under --bare)")
    if len(modes) > 1:
        flags.append("generation sessions ran in different permission modes: " + "; ".join(
            f"{mode} ({', '.join(f'{g} {n}' for g, n in sorted(by_gen.items()))})"
            for mode, by_gen in sorted(modes.items())))
    return flags


def init_event(path):
    """The `system`/`init` event at the start of a stream-json transcript ({} if none)."""
    try:
        with open(path, encoding="utf-8") as handle:
            for _, line in zip(range(20), handle):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("type") == "system" and event.get("subtype") == "init":
                        return event
    except OSError:
        pass
    return {}


def aggregate(run_dir, rubrics_dir=None):
    """The summary of `run_dir` as a dict (schemas.SUMMARY). Deterministic; writes nothing.

    Rubric metadata comes from the run's own copies (<run>/rubrics/, written when it was judged)
    when they exist, so a committed run re-aggregates the same way after eval/rubrics/ changes.
    """
    local = Path(run_dir) / "rubrics"
    if rubrics_dir is None and local.is_dir():
        rubrics_dir = local
    run = _Run(run_dir, rubrics_dir or RUBRICS_DIR)
    registry = _registry(run)
    flags = run.flags
    in_matrix = set(run.cell_keys())

    gen_by_id, ok_by_cell, failed_by_cell, outside = {}, {}, {}, []
    for gen in sorted(run.gens, key=lambda r: r["gen_id"]):
        cell = (gen["prompt_id"], gen["condition"], gen["generator"])
        if cell not in in_matrix:
            outside.append(gen["gen_id"])
            continue
        if gen["status"] == "ok":
            gen_by_id[gen["gen_id"]] = gen
            ok_by_cell.setdefault(cell, []).append(gen["gen_id"])
        else:
            failed_by_cell.setdefault(cell, []).append(gen["gen_id"])
    for cell in ok_by_cell.values():
        cell.sort(key=lambda gid: (gen_by_id[gid]["run_index"], gid))

    failed = sorted(g["gen_id"] for g in run.gens if g["status"] != "ok")
    if failed:
        flags.append(f"excluded {len(failed)} generation(s) with status error: {_ids(failed)}")
    for kind, recs, id_key in (("generation", run.gens, "gen_id"),
                               ("judgment", run.judgments, "judgment_id"),
                               ("probe", run.probes, "probe_id")):
        for r in sorted(recs, key=lambda r: r[id_key]):
            if r.get("model_mismatch"):
                used = ", ".join(r["models_used"]) or "unknown"
                flags.append(f"model mismatch ({kind} {r[id_key]}, excluded): requested "
                             f"{r.get('model_requested')}, used {used}")
    if outside:
        flags.append(f"excluded {len(outside)} generation(s) outside the config's matrix: "
                     f"{_ids(outside)}")
    flags += _provenance_flags(run)

    def rescored(record):
        """True if the record names (blind_sha256) a blind copy other than the key's current
        one: the plan was re-blinded after this judgment or probe, so it scored other text."""
        entry = (run.key or {}).get(record["blind_id"])
        sha = record.get("blind_sha256")
        return entry is not None and isinstance(sha, str) and sha != entry["sha256"]

    # judgments -> blind key -> generations
    coders = {info["id"]: set(run.coders(info["id"])) for info in run.rubrics.values()}
    judged_by_gen, orphans, foreign, outdated = {}, [], [], []
    for status in ("error", "invalid"):
        bad = sorted(j["judgment_id"] for j in run.judgments if j["status"] == status)
        if bad:
            flags.append(f"excluded {len(bad)} judgment(s) with status {status}: {_ids(bad)}")
    for j in sorted(run.judgments, key=lambda r: r["judgment_id"]):
        if j["status"] != "ok":
            continue
        entry = (run.key or {}).get(j["blind_id"])
        if entry is None or entry["gen_id"] not in gen_by_id:
            orphans.append(j["judgment_id"])
            continue
        # Also catches a judge outside its `rubrics` subset and a repeat beyond judge_repeats.
        if (j["judge"], j["repeat"]) not in coders.get(j["rubric"], ()):
            foreign.append(j["judgment_id"])
            continue
        if rescored(j):
            outdated.append(j["judgment_id"])
            continue
        judged_by_gen.setdefault(entry["gen_id"], {}).setdefault(j["rubric"], []).append(j)
    if orphans:
        flags.append(f"excluded {len(orphans)} ok judgment(s) whose blind id does not lead to "
                     f"an ok generation: {_ids(orphans)}")
    if foreign:
        flags.append(f"excluded {len(foreign)} judgment(s) from a rubric, judge or repeat that the "
                     f"config does not ask for: {_ids(foreign)}")
    if outdated:
        flags.append(f"excluded {len(outdated)} ok judgment(s) of an earlier version of their "
                     "blind copy (blind_sha256 differs from the sha256 in blind/key.json; judge "
                     f"them again): {_ids(outdated)}")
    for info in run.rubrics.values():
        recorded = sorted({j["rubric_sha256"] for j in run.judgments if j["rubric"] == info["id"]})
        stale = [sha for sha in recorded if sha != info["sha256"]]
        if stale:
            flags.append(f"rubric {info['id']}: judgments recorded sha256 "
                         f"{', '.join(s[:12] for s in stale)}, but the rubric file is now "
                         f"{info['sha256'][:12]}")

    # Complete-case: a generation's judgments of a rubric feed the headline metrics only if every
    # (judge, repeat) the config asks for scored it ok. A rubric with no usable judgment at all is
    # reported by the "missing judgments" flags instead, so it is not split here.
    complete_by_gen, incomplete = {}, {}  # incomplete: (rubric, condition, generator) -> [...]
    for info in run.rubrics.values():
        rid = info["id"]
        judged_any = any(rid in by_rubric for by_rubric in judged_by_gen.values())
        for gid in sorted(gen_by_id):
            js = judged_by_gen.get(gid, {}).get(rid, [])
            have = {(j["judge"], j["repeat"]) for j in js}
            missing = [coder for coder in run.coders(rid) if coder not in have]
            if not missing:
                complete_by_gen.setdefault(gid, {})[rid] = js
            elif judged_any:
                gen = gen_by_id[gid]
                incomplete.setdefault((rid, gen["condition"], gen["generator"]), []).append(
                    (gid, {judge for judge, _ in missing}))

    lint_by_gen = {r["gen_id"]: r for r in run.lints if r["gen_id"] in gen_by_id}
    values = {gid: _generation_metrics(run, gen, complete_by_gen.get(gid, {}),
                                       judged_by_gen.get(gid, {}), lint_by_gen.get(gid), registry)
              for gid, gen in gen_by_id.items()}

    # what is missing, and who judged their own model's plans
    for info in run.rubrics.values():
        for judge in run.judges:
            if judge.get("rubrics") and info["id"] not in judge["rubrics"]:
                continue  # this judge was never asked to score this rubric
            got = sum(1 for gid in gen_by_id for j in judged_by_gen.get(gid, {}).get(info["id"], [])
                      if j["judge"] == judge["id"])
            expected = len(gen_by_id) * run.repeats
            if got < expected:
                flags.append(f"missing judgments: {info['id']} by {judge['id']}: {got} of "
                             f"{expected} expected (ok generations x repeats) are ok")
    lost, incomplete_judging = {}, []  # lost: (rubric, condition, generator) -> (n_lost, n_ok)
    for info in run.rubrics.values():
        for c in run.conditions:
            for g in run.generators:
                left_out = incomplete.get((info["id"], c, g["id"]))
                if not left_out:
                    continue
                n_ok = sum(len(ok_by_cell.get((p, c, g["id"]), [])) for p in run.prompt_ids)
                lacking = {j["id"]: sum(1 for _, judges in left_out if j["id"] in judges)
                           for j in run.judges}
                lacking = {jid: n for jid, n in lacking.items() if n}
                lost[(info["id"], c, g["id"])] = (len(left_out), n_ok)
                incomplete_judging.append({"rubric": info["id"], "condition": c,
                                           "generator": g["id"], "n_ok": n_ok,
                                           "n_left_out": len(left_out),
                                           "lacking_judge": lacking})
                flags.append(f"incomplete judging: {info['id']} metrics leave out "
                             f"{len(left_out)} of {n_ok} ok {c} generation(s) (generator "
                             f"{g['id']}) that not every configured judge x repeat scored ok ("
                             + ", ".join(f"lacking {jid}: {n}" for jid, n in lacking.items())
                             + f"): {_ids(gid for gid, _ in left_out)}")
    if run.rubrics and gen_by_id and not judged_by_gen:
        flags.append("no ok judgments: every judge-based metric is n/a")
    if len(lint_by_gen) < len(gen_by_id):
        flags.append(f"lint missing for {len(gen_by_id) - len(lint_by_gen)} ok generation(s)")
    probes = [p for p in run.probes if not rescored(p)]
    if run.probe_cfg.get("enabled"):
        for judge in run.probe_cfg.get("judges", []):
            got = sum(1 for p in probes if p["judge"] == judge and p["status"] == "ok")
            if got < len(gen_by_id):
                flags.append(f"missing probes: {judge}: {got} of {len(gen_by_id)} expected are ok")
    for status in ("error", "invalid"):
        bad = sorted(p["probe_id"] for p in run.probes if p["status"] == status)
        if bad:
            flags.append(f"excluded {len(bad)} probe(s) with status {status}: {_ids(bad)}")
    outdated_probes = sorted(p["probe_id"] for p in run.probes
                             if p["status"] == "ok" and rescored(p))
    if outdated_probes:
        flags.append(f"excluded {len(outdated_probes)} ok probe(s) of an earlier version of their "
                     "blind copy (blind_sha256 differs from the sha256 in blind/key.json; probe "
                     f"them again): {_ids(outdated_probes)}")
    same_model = {}  # generator id -> ids of the judges that run on its model
    for g in run.generators:
        for judge in run.judges:
            if g.get("model") and judge.get("model") == g.get("model"):
                same_model.setdefault(g["id"], []).append(judge["id"])
                flags.append(f"judge {judge['id']} uses the generator's model {g['model']} "
                             f"(generator {g['id']}): it judged plans its own model wrote")
    if len(run.prompt_ids) < 5:
        flags.append(f"small n: {len(run.prompt_ids)} prompt(s) (< 5): intervals across prompts "
                     "are wide")
    if run.config["runs_per_cell"] < 3:
        flags.append(f"small n: {run.config['runs_per_cell']} run(s) per cell (< 3)")

    cells = _cells(run, ok_by_cell, failed_by_cell, values, registry)
    summary = {
        "schema": "dfa-eval/summary@1",
        "run_id": run.manifest["run_id"],
        "run": _run_block(run, gen_by_id, judged_by_gen),
        "generated_from": {
            "generations": _status_counts(run.gens, ("ok", "error"), run.invalid["generations"]),
            "judgments": _status_counts(run.judgments, ("ok", "error", "invalid"),
                                        run.invalid["judgments"]),
            "probes": _status_counts(run.probes, ("ok", "error", "invalid"), run.invalid["probes"]),
            "lint": {"records": len(run.lints), "used": len(lint_by_gen),
                     "not_a_valid_record": len(run.invalid["lint"])},
            "blind_key_entries": len(run.key or {}),
            # Usable ok judgments: the per-judge metrics and reliability read all of them, the
            # headline metrics only those of generations that every judge x repeat scored.
            "judgments_used": sum(len(js) for by in judged_by_gen.values() for js in by.values()),
            "incomplete_judging": incomplete_judging,
            "rubrics": {info["id"]: {"sha256": info["sha256"],
                                     "dimensions": len(info["rubric"]["dimensions"]),
                                     "scale_max": info["rubric"]["scale"]["max"]}
                        for role, info in sorted(run.rubrics.items())},
        },
        "metrics": registry,
        "cells": cells,
        "conditions": _conditions(run, cells, ok_by_cell, values, registry),
        "contrasts": _contrasts(run, ok_by_cell, values, registry, same_model, lost),
        "judge_reliability": _reliability(run, judged_by_gen, sorted(gen_by_id)),
        "leakage": _leakage(run, gen_by_id, probes),
        "length": _length(run, ok_by_cell, values),
        "cost": _cost(run),
        "flags": flags,
    }
    return stats.rounded(summary)


def _run_block(run, gen_by_id, judged_by_gen):
    def models_used(recs):
        return sorted({m for r in recs for m in r.get("models_used", [])})

    used = {}
    for by_rubric in judged_by_gen.values():
        for js in by_rubric.values():
            for j in js:
                used[j["judge"]] = used.get(j["judge"], 0) + 1

    generators = []
    for g in run.generators:
        mine = [r for r in run.gens if r["generator"] == g["id"]]
        ok = [r for r in mine if r["gen_id"] in gen_by_id]
        generators.append({"id": g["id"], "provider": g["provider"],
                           "model_requested": g.get("model"), "effort": g.get("effort"),
                           "models_used": models_used(ok), "n_ok": len(ok),
                           "n_records": len(mine)})
    judges = []
    for j in run.judges:
        mine = [r for r in run.judgments if r["judge"] == j["id"]]
        ok = [r for r in mine if r["status"] == "ok"]
        judges.append({"id": j["id"], "provider": j["provider"], "model_requested": j.get("model"),
                       "effort": j.get("effort"), "models_used": models_used(ok),
                       "n_ok": len(ok), "n_used": used.get(j["id"], 0), "n_records": len(mine)})
    started = sorted(r["started_at"] for r in run.gens if r.get("started_at"))
    finished = sorted(r["finished_at"] for r in run.gens if r.get("finished_at"))
    return {
        "config_name": run.config["name"], "seed": run.manifest["seed"],
        "created": run.manifest["created"], "harness_version": run.manifest["harness_version"],
        "skill_version": run.manifest["skill"]["version"],
        "repo_commit": run.manifest["repo_commit"],
        "runs_per_cell": run.config["runs_per_cell"], "judge_repeats": run.repeats,
        "generators": generators, "judges": judges,
        "probe_judges": (list(run.probe_cfg.get("judges", []))
                         if run.probe_cfg.get("enabled") else []),
        "first_generation_started": started[0] if started else None,
        "last_generation_finished": finished[-1] if finished else None,
    }


def write_summary(run_dir):
    """Aggregate, validate against schemas.SUMMARY, and write <run>/summary.json."""
    summary = aggregate(run_dir)
    schema.check(summary, schemas.SUMMARY, "summary")
    records.write_json(records.RunDir(run_dir).summary_path, summary)
    return summary


def check_summary(run_dir):
    """(ok, diff): recompute the summary and compare it with the committed summary.json."""
    path = records.RunDir(run_dir).summary_path
    expected = records.dumps(aggregate(run_dir))
    if not path.exists():
        return False, f"{records.SUMMARY} is missing; run `python eval/run.py aggregate`\n"
    current = path.read_bytes().decode("utf-8").replace("\r\n", "\n")
    if current == expected:
        return True, ""
    return False, "".join(difflib.unified_diff(
        current.splitlines(True), expected.splitlines(True),
        fromfile=f"{records.SUMMARY} (committed)", tofile=f"{records.SUMMARY} (recomputed)"))
