"""Write schema-valid run directories for the analysis tests (lint, aggregate, report, matrix).

A test describes a run compactly (which generations exist, what each judge scored, what each
probe said) and `make_run` writes every record a real run leaves behind: manifest.json,
generations/*.json + *.md, raw/*.txt, blind/key.json + blind copies, judgments/, probes/ and
lint/, in the shapes of eval/dfa_eval/schemas.py. Every record is validated before it is written,
so a fixture can never drift from the contract. Tests call this with a tempfile.mkdtemp() root;
nothing here writes into the repository.

Judge scores are given per coder: {"j1": 30, "j2#2": [2, 2, ...], "j3": "invalid"}, where "j2#2"
is judge j2's second repeat, an int is a total spread over the rubric's dimensions by `spread`,
a list is the per-dimension scores, "invalid"/"error" is a judgment with that status, and a dict
may add "checklist" (statuses 0-2), "traps" (booleans) and "cost". `supersede` adds a record
that a retry replaced, under <run>/superseded/.
"""

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "eval"))
from dfa_eval import records, schema, schemas  # noqa: E402

ADHERENCE = "methodology-adherence-v1"
EQ = "engineering-quality-v1"
RUBRICS = {rid: json.loads((ROOT / "eval" / "rubrics" / f"{rid}.json").read_text(encoding="utf-8"))
           for rid in (ADHERENCE, EQ)}
RUBRIC_SHA = {rid: records.sha256_file(ROOT / "eval" / "rubrics" / f"{rid}.json")
              for rid in (ADHERENCE, EQ)}
VARIANT = {ADHERENCE: "plain", EQ: "neutralized"}

PROMPTS = {
    "P1": ("RAG support chatbot", "AI",
           "Plan a customer-support RAG chatbot over our help-center docs."),
    "P2": ("Multi-tenant SaaS billing", "non-AI", "Architect a multi-tenant SaaS billing system."),
    "P3": ("CI/CD for 50 microservices", "non-AI",
           "Sequence building a CI/CD platform for 50 microservices."),
    "P4": ("GitHub issue agent", "AI",
           "Plan an autonomous agent that triages and resolves GitHub issues."),
    "P5": ("Collaborative doc editor", "non-AI",
           "Order the build of a real-time collaborative document editor."),
    "P6": ("Hospital scheduling cloud migration", "non-AI",
           "Plan migrating our hospital's on-premises patient scheduling system to the cloud "
           "without downtime."),
    "P7": ("Photo renaming CLI", "non-AI",
           "Plan a command-line tool that renames a folder of photos by their EXIF capture date."),
    "P8": ("Card fraud detection", "AI",
           "Plan a real-time fraud-detection pipeline for card transactions."),
}

CONDITIONS = {
    "baseline": {"kind": "plain", "description": "The request alone, no planning instructions."},
    "dfa": {"kind": "skill", "description": "The Dependency-First Architect skill.",
            "skill_dir": ".", "skill_name": "dependency-first-architect"},
    "generic-control": {"kind": "skill",
                        "description": "Active control: an independently written, comparably "
                                       "long generic planning skill.",
                        "skill_dir": "eval/conditions/generic-architect",
                        "skill_name": "architecture-planner"},
    "baseline-capped": {"kind": "plain", "description": "The request alone, with a length cap.",
                        "length_cap_words": 100},
    "dfa-capped": {"kind": "skill", "description": "The skill, with a length cap.",
                   "skill_dir": ".", "skill_name": "dependency-first-architect",
                   "length_cap_words": 100},
}


def spread(total, n, top):
    """n integer scores in [0, top] that sum to `total`, larger scores first."""
    if not 0 <= total <= n * top:
        raise ValueError(f"total {total} is outside 0..{n * top}")
    q, r = divmod(total, n)
    return [q + 1] * r + [q] * (n - r)


def eq_core(scores):
    """EQ total without the dimensions flagged overlaps_methodology (independent of aggregate)."""
    overlap = {d["id"] for d in RUBRICS[EQ]["dimensions"] if d.get("overlaps_methodology")}
    return sum(s for dim, s in enumerate(scores, 1) if dim not in overlap)


def gen(prompt, condition, run=1, generator="g1", status="ok", words=120, text=None, cost=0.01,
        latency=2.0, output_tokens=None, adherence=None, eq=None, probes=None, lint=(4, 16),
        model_mismatch=False, started_at=None):
    """One generation spec (see the module docstring for the judge-score notation)."""
    return dict(prompt=prompt, condition=condition, run=run, generator=generator, status=status,
                words=words, text=text, cost=cost, latency=latency, output_tokens=output_tokens,
                adherence=adherence or {}, eq=eq or {}, probes=probes or {}, lint=lint,
                model_mismatch=model_mismatch, started_at=started_at)


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _checked(data, record_schema, what):
    return schema.check(data, record_schema, what)


def _call_fields(provider, model, cost, latency, raw_file, output_tokens=200, mismatch=False):
    used = ["some-other-model"] if mismatch else ([model] if model else [])
    return {
        "provider": provider, "model_requested": model, "models_used": used,
        "model_mismatch": mismatch,
        "usage": {"input_tokens": 1000, "output_tokens": output_tokens, "thinking_tokens": None,
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        "cost_usd": cost, "latency_s": latency, "raw_file": raw_file,
    }


def make_run(root, gens, run_id="fixture-run", prompts=("P1", "P2"), conditions=("baseline", "dfa"),
             generators=None, judges=None, rubrics=(ADHERENCE, EQ), runs_per_cell=2,
             judge_repeats=1, contrasts=(("dfa", "baseline"),), probe_judges=None, seed=11,
             name="fixture-bench", skill_version="2.0.0", created="2026-10-02",
             condition_overrides=None, write_lint=True):
    """Write a run directory under `root` and return its path."""
    generators = generators or [{"id": "g1", "provider": "fake", "model": "gen-model"}]
    judges = judges if judges is not None else [{"id": "j1", "provider": "fake",
                                                 "model": "judge-model"}]
    conds = {}
    for c in conditions:
        conds[c] = dict(CONDITIONS[c])
        conds[c].update((condition_overrides or {}).get(c, {}))
    config = {
        "schema": "dfa-eval/config@1", "name": name, "description": "Fixture benchmark.",
        "seed": seed,
        "prompts": [{"id": p, "title": PROMPTS[p][0], "kind": PROMPTS[p][1],
                     "request": PROMPTS[p][2], "checklist": None, "tags": []} for p in prompts],
        "conditions": conds, "generators": generators, "judges": judges,
        "rubrics": list(rubrics), "runs_per_cell": runs_per_cell, "judge_repeats": judge_repeats,
        "contrasts": [{"treatment": t, "control": c} for t, c in contrasts],
        "blinding": {"strip_self_score": True, "neutralize_terms_for": [EQ]},
        "probe": {"enabled": bool(probe_judges), "judges": list(probe_judges or [])},
    }
    _checked(config, schemas.CONFIG, "fixture config")
    run = records.RunDir(Path(root) / run_id)
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    manifest = {
        "schema": "dfa-eval/manifest@1", "run_id": run_id, "created": created,
        "harness_version": "1.0.0", "repo_commit": "abc1234", "skill": {
            "version": skill_version, "files": {"SKILL.md": "0" * 64}},
        "condition_files": {}, "config_file": "eval/benchmark.json",
        "config_sha256": _sha(canonical), "config": config, "seed": seed,
        "platform": {"python": "3.x"}, "tools": {}, "notes": "test fixture",
    }
    records.write_json(run.manifest_path, _checked(manifest, schemas.MANIFEST, "manifest"))

    model_of = {g["id"]: g for g in generators}
    judge_of = {j["id"]: j for j in judges}
    salt = _sha(f"blind:{seed}:{run_id}")[:16]
    key_entries, order = [], [0]

    def next_order():
        order[0] += 1
        return order[0] - 1

    for spec in gens:
        gmodel = model_of[spec["generator"]]
        gid = records.gen_id(spec["prompt"], spec["condition"], spec["generator"], spec["run"])
        ok = spec["status"] == "ok"
        text = spec["text"] if spec["text"] is not None else " ".join(["plan"] * spec["words"])
        text = text if text.endswith("\n") else text + "\n"
        words = len(text.split()) if spec["text"] is not None else spec["words"]
        record = {
            "schema": "dfa-eval/generation@1", "gen_id": gid, "prompt_id": spec["prompt"],
            "condition": spec["condition"], "generator": spec["generator"],
            "run_index": spec["run"], "seed": None, "seed_honored": False,
            "request": PROMPTS[spec["prompt"]][2], "status": "ok" if ok else "error",
            "error": None if ok else "fixture: provider failed",
            "output_file": f"generations/{gid}.md" if ok else None,
            "output_sha256": _sha(text) if ok else None,
            "metrics": ({"words": words, "chars": len(text), "visible_tokens_est": -(-len(text) // 4)}
                        if ok else None),
            "tool_calls": [], "turns": 1, "skills_available": None, "instructions_loaded": None,
            "started_at": spec["started_at"] or "2026-10-02T10:00:00Z",
            "finished_at": spec["started_at"] or "2026-10-02T10:00:00Z",
        }
        record.update(_call_fields(gmodel["provider"], gmodel.get("model"), spec["cost"],
                                   spec["latency"], f"raw/gen-{gid}.txt",
                                   spec["output_tokens"] if spec["output_tokens"] is not None
                                   else words * 2, spec["model_mismatch"]))
        records.write_json(run.generation_json(gid),
                           _checked(record, schemas.GENERATION, f"generation {gid}"))
        records.write_text(run.raw_path(f"gen-{gid}"), "fixture raw output\n")
        if not ok:
            continue
        records.write_text(run.generation_text(gid), text)
        blind = {}
        for variant in ("plain", "neutralized"):
            bid = _sha(f"{salt}:{gid}:{variant}")[:12]
            blind[variant] = bid
            records.write_text(run.blind_text(bid), text)
            key_entries.append({"blind_id": bid, "gen_id": gid, "variant": variant,
                                "sha256": _sha(text), "replacements": 0,
                                "self_score_removed": False})
        if write_lint and spec["lint"] is not None:
            passed, total = spec["lint"]
            lint = {"schema": "dfa-eval/lint@1", "gen_id": gid, "template": "plan-template-v2",
                    "checks": [{"id": f"fixture.check{i + 1}", "ok": i < passed,
                                "detail": "fixture"} for i in range(total)],
                    "passed": passed, "total": total, "score": passed / total}
            records.write_json(run.lint_path(gid), _checked(lint, schemas.LINT, f"lint {gid}"))
        for rubric, scores_by_coder in ((ADHERENCE, spec["adherence"]), (EQ, spec["eq"])):
            for coder in sorted(scores_by_coder):
                judge, _, repeat = coder.partition("#")
                _write_judgment(run, spec, rubric, judge_of[judge], int(repeat or 1),
                                blind[VARIANT[rubric]], scores_by_coder[coder], next_order())
        for judge in sorted(spec["probes"]):
            _write_probe(run, judge_of[judge], blind["neutralized"], spec["probes"][judge],
                         next_order())

    if key_entries:
        key = {"schema": "dfa-eval/blind-key@1", "salt": salt, "neutralize_terms_for": [EQ],
               "entries": sorted(key_entries, key=lambda e: e["blind_id"])}
        records.write_json(run.blind_key_path, _checked(key, schemas.BLIND_KEY, "blind key"))
    return run.root


def _write_judgment(run, spec, rubric_id, judge, repeat, blind_id, value, order_index):
    rubric = RUBRICS[rubric_id]
    n, top = len(rubric["dimensions"]), rubric["scale"]["max"]
    detail = value if isinstance(value, dict) else {"total": value}
    status, scores = "ok", detail.get("scores", detail.get("total"))
    if scores in ("invalid", "error"):
        status, scores = scores, None
    elif isinstance(scores, int):
        scores = spread(scores, n, top)
    jid = records.judgment_id(blind_id, rubric_id, judge["id"], repeat)
    checklist = traps = None
    if status == "ok" and rubric.get("uses_checklists"):
        statuses = detail.get("checklist", [1, 1])
        checklist = [{"id": f"{spec['prompt']}-{k:02d}", "status": s, "evidence": "fixture"}
                     for k, s in enumerate(statuses, 1)]
        traps = [{"id": f"{spec['prompt']}-T{k}", "present": bool(p), "evidence": "fixture"}
                 for k, p in enumerate(detail.get("traps", [False]), 1)]
    record = {
        "schema": "dfa-eval/judgment@1", "judgment_id": jid, "blind_id": blind_id,
        "rubric": rubric_id, "rubric_sha256": RUBRIC_SHA[rubric_id], "judge": judge["id"],
        "repeat": repeat, "order_index": order_index, "status": status,
        "error": "fixture: provider failed" if status == "error" else None,
        "validation_errors": ["$.dimensions: 9 items, fewer than minItems 10"]
        if status == "invalid" else [],
        "scores": [{"dim": d, "score": s, "evidence": "fixture", "rationale": "fixture"}
                   for d, s in enumerate(scores or [], 1)] if status == "ok" else [],
        "checklist": checklist, "traps": traps, "note": "fixture" if status == "ok" else None,
    }
    record.update(_call_fields(judge["provider"], judge.get("model"), detail.get("cost", 0.002),
                               4.0, f"raw/judge-{jid}.txt"))
    records.write_json(run.judgment_path(jid), _checked(record, schemas.JUDGMENT, f"judgment {jid}"))


def _write_probe(run, judge, blind_id, value, order_index):
    detail = value if isinstance(value, dict) else {"p": value}
    status = detail["p"] if detail["p"] in ("invalid", "error") else "ok"
    pid = records.probe_id(blind_id, judge["id"])
    record = {
        "schema": "dfa-eval/probe@1", "probe_id": pid, "blind_id": blind_id, "judge": judge["id"],
        "order_index": order_index, "status": status,
        "error": "fixture: provider failed" if status == "error" else None,
        "validation_errors": ["$.p_methodology: missing"] if status == "invalid" else [],
        "p_methodology": detail["p"] if status == "ok" else None,
        "cues": ["fixture cue"] if status == "ok" else [],
    }
    record.update(_call_fields(judge["provider"], judge.get("model"), detail.get("cost", 0.001),
                               1.0, f"raw/probe-{pid}.txt"))
    records.write_json(run.probe_path(pid), _checked(record, schemas.PROBE, f"probe {pid}"))


SUPERSEDED_SCHEMAS = {"generations": schemas.GENERATION, "judgments": schemas.JUDGMENT,
                      "probes": schemas.PROBE}


def supersede(run_dir, record_path, n=1, **changes):
    """A call that a retry replaced: a copy of one of the run's generation, judgment or probe
    records, with `changes`, written (schema-checked) as <run>/superseded/<folder>/<id>.<n>.json.
    Returns the path."""
    record_path = Path(record_path)
    folder = record_path.parent.name
    record = dict(records.read_json(record_path), **changes)
    path = Path(run_dir) / "superseded" / folder / f"{record_path.stem}.{n}.json"
    records.write_json(path, _checked(record, SUPERSEDED_SCHEMAS[folder], f"superseded {path.name}"))
    return path


def validate_run_dir(run_dir):
    """Every JSON record in a run directory checked against its schema; returns the errors."""
    run_dir = Path(run_dir)
    kinds = {"generations": schemas.GENERATION, "judgments": schemas.JUDGMENT,
             "probes": schemas.PROBE, "lint": schemas.LINT}
    errors = schema.validate(records.read_json(run_dir / "manifest.json"), schemas.MANIFEST)
    for folder, record_schema in kinds.items():
        for path in sorted((run_dir / folder).glob("*.json"), key=lambda p: p.name):
            errors += [f"{folder}/{path.name}: {e}"
                       for e in schema.validate(records.read_json(path), record_schema)]
    key = run_dir / "blind" / "key.json"
    if key.exists():
        errors += schema.validate(records.read_json(key), schemas.BLIND_KEY)
    return errors
