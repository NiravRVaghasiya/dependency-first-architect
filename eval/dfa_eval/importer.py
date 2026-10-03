"""Import the ten v1.0.0 example plans (examples/) as a run, so the published plans can be judged
again with the v2 harness: the independent engineering-quality rubric, other judge models, and
the leakage probe.

    python eval/run.py import-v1 --run eval/results/<run-id>

Nothing is generated. Each plan's text is taken from examples/<prompt>/<arm>-skill.md (below its
provenance header) and must match the SHA-256 recorded in examples/scoring/generation.json when
it was generated, so the imported text is exactly what the model wrote and what v1's judges
scored. Records carry provider "imported"; tokens, cost, and latency were not recorded in v1 and
stay null. After importing, run the usual stages: blind, judge, probe, lint, aggregate, report.
"""

import json
from pathlib import Path

from . import generate, records, schema, schemas
from . import config as configmod

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = Path("examples")
HEADER_END = "\n\n---\n\n"
GENERATOR = {"id": "opus-5.5-max", "provider": "claude-cli", "model": "claude-opus-5-5",
             "effort": "max"}
V1_DIR = "eval/conditions/dfa-v1.0.0"
ARMS = {"without": "baseline", "with": "dfa-v1"}


def import_config(repo_root, judges, seed):
    """The config recorded in the manifest. It describes how the plans were made; it is never
    used to generate anything."""
    bench = json.loads((repo_root / "eval" / "benchmark.json").read_text(encoding="utf-8"))
    prompts = [p for p in bench["prompts"] if p["id"] in ("P1", "P2", "P3", "P4", "P5")]
    return {
        "schema": "dfa-eval/config@1",
        "name": "v1-examples-rejudge",
        "description": "The ten v1.0.0 example plans (examples/, one run per arm, generated "
                       "2026-10-02 with claude-opus-5-5 at effort max in isolated claude --bare "
                       "sessions), imported verbatim and judged again with the v2 harness.",
        "seed": seed,
        "prompts": prompts,
        "conditions": {
            "baseline": {"kind": "plain",
                         "description": "v1.0.0 'without the skill' arm: the request alone."},
            "dfa-v1": {"kind": "skill",
                       "description": "v1.0.0 'with the skill' arm: Dependency-First Architect "
                                      "at commit 3ba6b70, invoked as /dependency-first-architect.",
                       "skill_dir": V1_DIR, "skill_name": "dependency-first-architect"},
        },
        "generators": [GENERATOR],
        "judges": judges,
        "rubrics": ["methodology-adherence-v1", "engineering-quality-v1"],
        "runs_per_cell": 1,
        "judge_repeats": 1,
        "contrasts": [{"treatment": "dfa-v1", "control": "baseline"}],
        "blinding": {"strip_self_score": True, "neutralize_terms_for": ["engineering-quality-v1"]},
        "probe": {"enabled": True, "judges": [judges[0]["id"]]},
        "workspace": {"readme": None},
        "limits": {"max_cost_usd": 40, "jobs": 8, "timeout_s": 3600},
    }


def v1_skill_snapshot(repo_root):
    folder = repo_root / V1_DIR
    files = {}
    for line in (folder / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, _, rel = line.partition("  ")
        files[rel] = digest
    return {"version": "1.0.0 (commit 3ba6b70)", "files": files}


def import_v1_examples(run_dir, judges, seed=20261006, repo_root=None, log=None):
    """Create `run_dir` with a manifest and one generation record per v1 example plan."""
    log = log or generate.default_log
    repo_root = Path(repo_root) if repo_root else ROOT
    run = records.RunDir(run_dir)
    if run.root.exists() and any(run.root.iterdir()):
        raise generate.HarnessError(f"{run.root} is not empty; import into a new run directory")
    config = configmod.check_config(import_config(repo_root, judges, seed), repo_root)
    generation = json.loads((repo_root / EXAMPLES / "scoring" / "generation.json")
                            .read_text(encoding="utf-8"))
    prompts = configmod.by_id(config["prompts"])
    skill = v1_skill_snapshot(repo_root)
    manifest = generate.new_manifest(run_dir, config, None, repo_root, {})
    manifest["skill"] = skill
    manifest["condition_files"] = {"dfa-v1": {f"{V1_DIR}/{rel}": sha
                                              for rel, sha in skill["files"].items()}}
    manifest["tools"] = {"claude": generation.get("claude_cli", "unknown")}
    manifest["notes"] = ("Imported, not generated: the plans in examples/ as committed, verified "
                         "against the SHA-256 recorded in examples/scoring/generation.json at "
                         f"generation time ({generation['date']}, model {generation['model']}, "
                         f"effort {generation['effort']}, skill commit {generation['skill']}). "
                         "Tokens, cost and latency were not recorded in v1.")
    schema.check(manifest, schemas.MANIFEST, "manifest")
    run.root.mkdir(parents=True, exist_ok=True)
    records.write_json(run.manifest_path, manifest)
    imported = []
    for entry in generation["runs"]:
        pid, condition = entry["prompt"], ARMS[entry["arm"]]
        folder = next((repo_root / EXAMPLES).glob(f"{pid.lower()}-*"))
        text = (folder / f"{entry['arm']}-skill.md").read_bytes().decode("utf-8")
        body = text.replace("\r\n", "\n").split(HEADER_END, 1)[1]
        if records.sha256_text(body) != entry["sha256"]:
            raise generate.HarnessError(f"{folder.name}/{entry['arm']}-skill.md differs from the "
                                        "model output recorded in generation.json")
        gid = records.gen_id(pid, condition, GENERATOR["id"], 1)
        request = prompts[pid]["request"]
        if condition == "dfa-v1":
            request = f"/dependency-first-architect {request}"
        records.write_text(run.generation_text(gid), body)
        record = {
            "schema": "dfa-eval/generation@1", "gen_id": gid, "prompt_id": pid,
            "condition": condition, "generator": GENERATOR["id"], "run_index": 1, "seed": None,
            "seed_honored": False, "request": request, "status": "ok", "error": None,
            "output_file": f"{records.GENERATIONS}/{gid}.md",
            "output_sha256": records.sha256_text(body), "metrics": generate.text_metrics(body),
            "turns": entry.get("turns"),
            "tool_calls": [{"tool": c["tool"], "path": c["path"], "ok": bool(c["ok"])}
                           for c in entry.get("tool_calls", [])],
            "skills_available": None, "instructions_loaded": None, "started_at": None,
            "finished_at": None, "provider": "imported", "model_requested": GENERATOR["model"],
            "models_used": [], "model_mismatch": False, "usage": None, "cost_usd": None,
            "latency_s": None, "raw_file": None,
        }
        schema.check(record, schemas.GENERATION, f"generation record {gid}")
        records.write_json(run.generation_json(gid), record)
        imported.append(gid)
    log(f"imported {len(imported)} v1 plans into {run.root}")
    return imported
