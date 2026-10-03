"""JSON schemas for every record the harness reads or writes (validated by `schema.validate`).

These use a small, explicit subset of JSON Schema (see `schema.py` for the supported keywords),
so they can be checked with the standard library and also passed to providers that accept a JSON
Schema for structured output. Record schemas are versioned by their `schema` constant: change a
record's shape, bump its version.
"""

ID = {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]*$"}
SHA256 = {"type": "string", "pattern": r"^[0-9a-f]{64}$"}
DATE = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"}
NUM_OR_NULL = {"type": ["number", "null"]}
INT_OR_NULL = {"type": ["integer", "null"]}
STR_OR_NULL = {"type": ["string", "null"]}
STRINGS = {"type": "array", "items": {"type": "string"}}

PROVIDERS = ["claude-cli", "openai-compatible", "command", "fake"]
# Records may also come from plans generated elsewhere and imported verbatim (e.g. the v1.0.0
# examples); such records carry provider "imported" and never come from a config.
RECORD_PROVIDERS = PROVIDERS + ["imported"]

# --------------------------------------------------------------------------------------------
# Benchmark configuration (eval/*.json)
# --------------------------------------------------------------------------------------------

PROMPT = {
    "type": "object",
    "required": ["id", "title", "kind", "request"],
    "additionalProperties": False,
    "properties": {
        "id": ID,
        "title": {"type": "string", "minLength": 1},
        "kind": {"enum": ["AI", "non-AI"]},
        "request": {"type": "string", "minLength": 1},
        "checklist": STR_OR_NULL,
        "tags": STRINGS,
    },
}

CONDITION = {
    "type": "object",
    "required": ["kind", "description"],
    "additionalProperties": False,
    "properties": {
        "kind": {"enum": ["plain", "skill"]},
        "description": {"type": "string", "minLength": 1},
        "skill_dir": {"type": "string"},
        "skill_name": ID,
        "length_cap_words": {"type": ["integer", "null"], "minimum": 50},
    },
}

MODEL = {
    "type": "object",
    "required": ["id", "provider"],
    "additionalProperties": False,
    "properties": {
        "id": ID,
        "provider": {"enum": PROVIDERS},
        "model": STR_OR_NULL,
        "effort": STR_OR_NULL,
        "conditions": STRINGS,  # generators only: the subset of conditions it generates
        "rubrics": STRINGS,  # judges only: the subset of the config's rubrics it scores
        "options": {"type": "object"},
    },
}

CONFIG = {
    "type": "object",
    "required": ["schema", "name", "seed", "prompts", "conditions", "generators", "runs_per_cell"],
    "additionalProperties": False,
    "properties": {
        "schema": {"const": "dfa-eval/config@1"},
        "name": ID,
        "description": {"type": "string"},
        "seed": {"type": "integer", "minimum": 0},
        "prompts": {"type": "array", "minItems": 1, "items": PROMPT},
        "conditions": {"type": "object", "minProperties": 1, "additionalProperties": CONDITION},
        "generators": {"type": "array", "minItems": 1, "items": MODEL},
        "judges": {"type": "array", "items": MODEL},
        "rubrics": STRINGS,
        "runs_per_cell": {"type": "integer", "minimum": 1},
        "judge_repeats": {"type": "integer", "minimum": 1},
        "contrasts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["treatment", "control"],
                "additionalProperties": False,
                "properties": {"treatment": ID, "control": ID},
            },
        },
        "blinding": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "strip_self_score": {"type": "boolean"},
                "neutralize_terms_for": STRINGS,
            },
        },
        "probe": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"enabled": {"type": "boolean"}, "judges": STRINGS},
        },
        "workspace": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"readme": STR_OR_NULL},
        },
        "limits": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "max_cost_usd": {"type": ["number", "null"], "minimum": 0},
                "jobs": {"type": "integer", "minimum": 1},
                "timeout_s": {"type": "integer", "minimum": 1},
            },
        },
    },
}

# --------------------------------------------------------------------------------------------
# Run records
# --------------------------------------------------------------------------------------------

USAGE = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "properties": {
        "input_tokens": INT_OR_NULL,
        "output_tokens": INT_OR_NULL,
        "thinking_tokens": INT_OR_NULL,
        "cache_read_input_tokens": INT_OR_NULL,
        "cache_creation_input_tokens": INT_OR_NULL,
    },
}

CALL_FIELDS = {
    "provider": {"enum": RECORD_PROVIDERS},
    "model_requested": STR_OR_NULL,
    "models_used": STRINGS,
    "model_mismatch": {"type": "boolean"},
    "usage": USAGE,
    "cost_usd": {"type": ["number", "null"], "minimum": 0},
    "latency_s": {"type": ["number", "null"], "minimum": 0},
    "raw_file": STR_OR_NULL,
}

MANIFEST = {
    "type": "object",
    "required": ["schema", "run_id", "created", "harness_version", "repo_commit", "skill",
                 "config", "config_sha256", "seed"],
    "additionalProperties": False,
    "properties": {
        "schema": {"const": "dfa-eval/manifest@1"},
        "run_id": ID,
        "created": DATE,
        "harness_version": {"type": "string"},
        "repo_commit": {"type": "string"},
        "skill": {
            "type": "object",
            "required": ["version", "files"],
            "additionalProperties": False,
            "properties": {
                "version": {"type": "string"},
                "files": {"type": "object", "additionalProperties": SHA256},
            },
        },
        "condition_files": {
            "type": "object",
            "additionalProperties": {"type": "object", "additionalProperties": SHA256},
        },
        "config_file": STR_OR_NULL,
        "config_sha256": SHA256,
        "config": {"type": "object"},
        "seed": {"type": "integer", "minimum": 0},
        "platform": {"type": "object"},
        "tools": {"type": "object"},
        "notes": {"type": "string"},
    },
}

GENERATION = {
    "type": "object",
    "required": ["schema", "gen_id", "prompt_id", "condition", "generator", "run_index", "seed",
                 "seed_honored", "request", "status", "error", "output_file", "output_sha256",
                 "metrics", "tool_calls", *CALL_FIELDS],
    "additionalProperties": False,
    "properties": {
        "schema": {"const": "dfa-eval/generation@1"},
        "gen_id": ID,
        "prompt_id": ID,
        "condition": ID,
        "generator": ID,
        "run_index": {"type": "integer", "minimum": 1},
        "seed": INT_OR_NULL,
        "seed_honored": {"type": "boolean"},
        "request": {"type": "string"},
        "status": {"enum": ["ok", "error"]},
        "error": STR_OR_NULL,
        "output_file": STR_OR_NULL,
        "output_sha256": {"type": ["string", "null"], "pattern": r"^[0-9a-f]{64}$"},
        "metrics": {
            "type": ["object", "null"],
            "required": ["words", "chars", "visible_tokens_est"],
            "additionalProperties": False,
            "properties": {
                "words": {"type": "integer", "minimum": 0},
                "chars": {"type": "integer", "minimum": 0},
                "visible_tokens_est": {"type": "integer", "minimum": 0},
            },
        },
        "turns": INT_OR_NULL,
        "tool_calls": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["tool", "path", "ok"],
                "additionalProperties": False,
                "properties": {"tool": {"type": "string"}, "path": {"type": "string"},
                               "ok": {"type": "boolean"}},
            },
        },
        "skills_available": {"type": ["array", "null"], "items": {"type": "string"}},
        "instructions_loaded": {
            "type": ["array", "null"],
            "items": {
                "type": "object",
                "required": ["path", "sha256", "words"],
                "additionalProperties": False,
                "properties": {"path": {"type": "string"}, "sha256": SHA256,
                               "words": {"type": "integer", "minimum": 0}},
            },
        },
        "started_at": STR_OR_NULL,
        "finished_at": STR_OR_NULL,
        **CALL_FIELDS,
    },
}

BLIND_KEY = {
    "type": "object",
    "required": ["schema", "salt", "neutralize_terms_for", "entries"],
    "additionalProperties": False,
    "properties": {
        "schema": {"const": "dfa-eval/blind-key@1"},
        "salt": {"type": "string", "minLength": 1},
        "neutralize_terms_for": STRINGS,
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["blind_id", "gen_id", "variant", "sha256", "replacements",
                             "self_score_removed"],
                "additionalProperties": False,
                "properties": {
                    "blind_id": ID,
                    "gen_id": ID,
                    "variant": {"enum": ["plain", "neutralized"]},
                    "sha256": SHA256,
                    "replacements": {"type": "integer", "minimum": 0},
                    "self_score_removed": {"type": "boolean"},
                },
            },
        },
    },
}

SCORE_ITEM = {
    "type": "object",
    "required": ["dim", "score", "evidence", "rationale"],
    "additionalProperties": False,
    "properties": {
        "dim": {"type": "integer", "minimum": 1},
        "score": {"type": "integer", "minimum": 0},
        "evidence": {"type": "string"},
        "rationale": {"type": "string"},
    },
}

JUDGMENT = {
    "type": "object",
    "required": ["schema", "judgment_id", "blind_id", "rubric", "rubric_sha256", "judge", "repeat",
                 "order_index", "status", "error", "validation_errors", "scores", "checklist",
                 "traps", "note", *CALL_FIELDS],
    "additionalProperties": False,
    "properties": {
        "schema": {"const": "dfa-eval/judgment@1"},
        "judgment_id": ID,
        "blind_id": ID,
        # sha256 of the blind copy the judge scored (its blind/key.json entry at judge time).
        # Optional: records written before it existed lack it and stay valid.
        "blind_sha256": SHA256,
        "rubric": ID,
        "rubric_sha256": SHA256,
        "judge": ID,
        "repeat": {"type": "integer", "minimum": 1},
        "order_index": {"type": "integer", "minimum": 0},
        "status": {"enum": ["ok", "error", "invalid"]},
        "error": STR_OR_NULL,
        "validation_errors": STRINGS,
        "scores": {"type": "array", "items": SCORE_ITEM},
        "checklist": {
            "type": ["array", "null"],
            "items": {
                "type": "object",
                "required": ["id", "status", "evidence"],
                "additionalProperties": False,
                "properties": {"id": {"type": "string"},
                               "status": {"type": "integer", "minimum": 0, "maximum": 2},
                               "evidence": {"type": "string"}},
            },
        },
        "traps": {
            "type": ["array", "null"],
            "items": {
                "type": "object",
                "required": ["id", "present", "evidence"],
                "additionalProperties": False,
                "properties": {"id": {"type": "string"}, "present": {"type": "boolean"},
                               "evidence": {"type": "string"}},
            },
        },
        "note": STR_OR_NULL,
        **CALL_FIELDS,
    },
}

PROBE = {
    "type": "object",
    "required": ["schema", "probe_id", "blind_id", "judge", "order_index", "status", "error",
                 "validation_errors", "p_methodology", "cues", *CALL_FIELDS],
    "additionalProperties": False,
    "properties": {
        "schema": {"const": "dfa-eval/probe@1"},
        "probe_id": ID,
        "blind_id": ID,
        "blind_sha256": SHA256,  # optional, as in JUDGMENT
        "judge": ID,
        "order_index": {"type": "integer", "minimum": 0},
        "status": {"enum": ["ok", "error", "invalid"]},
        "error": STR_OR_NULL,
        "validation_errors": STRINGS,
        "p_methodology": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "cues": STRINGS,
        **CALL_FIELDS,
    },
}

LINT = {
    "type": "object",
    "required": ["schema", "gen_id", "template", "checks", "passed", "total", "score"],
    "additionalProperties": False,
    "properties": {
        "schema": {"const": "dfa-eval/lint@1"},
        "gen_id": ID,
        "template": {"const": "plan-template-v2"},
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "ok", "detail"],
                "additionalProperties": False,
                "properties": {"id": {"type": "string"}, "ok": {"type": "boolean"},
                               "detail": {"type": "string"}},
            },
        },
        "passed": {"type": "integer", "minimum": 0},
        "total": {"type": "integer", "minimum": 0},
        "score": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
    },
}

STAT = {
    "type": "object",
    "required": ["n", "mean", "median", "sd", "min", "max", "ci95"],
    "additionalProperties": False,
    "properties": {
        "n": {"type": "integer", "minimum": 0},
        "mean": NUM_OR_NULL,
        "median": NUM_OR_NULL,
        "sd": NUM_OR_NULL,
        "min": NUM_OR_NULL,
        "max": NUM_OR_NULL,
        "ci95": {"type": ["array", "null"], "items": {"type": "number"}, "minItems": 2,
                 "maxItems": 2},
    },
}

SUMMARY = {
    "type": "object",
    "required": ["schema", "run_id", "generated_from", "metrics", "cells", "conditions",
                 "contrasts", "judge_reliability", "leakage", "length", "flags"],
    "additionalProperties": True,
    "properties": {
        "schema": {"const": "dfa-eval/summary@1"},
        "run_id": ID,
        "generated_from": {"type": "object"},
        "metrics": {"type": "object"},
        "cells": {"type": "array", "items": {"type": "object"}},
        "conditions": {"type": "array", "items": {"type": "object"}},
        "contrasts": {"type": "array", "items": {"type": "object"}},
        "judge_reliability": {"type": "array", "items": {"type": "object"}},
        "leakage": {"type": "array", "items": {"type": "object"}},
        "length": {"type": "array", "items": {"type": "object"}},
        "cost": {"type": "object"},
        "flags": STRINGS,
    },
}

# --------------------------------------------------------------------------------------------
# Outcome benchmark records
# --------------------------------------------------------------------------------------------

TEST_RESULTS = {
    "type": "object",
    "required": ["status", "by_category", "tests"],
    "additionalProperties": False,
    "properties": {
        "status": {"enum": ["ok", "timeout", "error", "refused"]},
        "error": STR_OR_NULL,
        "by_category": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "required": ["passed", "total"],
                "additionalProperties": False,
                "properties": {"passed": {"type": "integer", "minimum": 0},
                               "total": {"type": "integer", "minimum": 0}},
            },
        },
        "tests": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "category", "outcome"],
                "additionalProperties": False,
                "properties": {"id": {"type": "string"}, "category": {"type": "string"},
                               "outcome": {"enum": ["pass", "fail", "error", "skip"]},
                               "message": STR_OR_NULL},
            },
        },
    },
}

OUTCOME_ROUND = {
    "type": "object",
    "required": ["round", "status", "error", "implementer", "files", "tests", "diff_from_previous"],
    "additionalProperties": False,
    "properties": {
        "round": {"type": "integer", "minimum": 1},
        "status": {"enum": ["ok", "error", "skipped"]},
        "error": STR_OR_NULL,
        "implementer": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"turns": INT_OR_NULL, **CALL_FIELDS},
        },
        "code_sha256": {"type": ["string", "null"], "pattern": r"^[0-9a-f]{64}$"},
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["path", "sha256", "lines"],
                "additionalProperties": False,
                "properties": {"path": {"type": "string"}, "sha256": SHA256,
                               "lines": {"type": "integer", "minimum": 0}},
            },
        },
        "tests": {"type": ["object", "null"]},
        "diff_from_previous": {
            "type": ["object", "null"],
            "required": ["files_changed", "lines_added", "lines_removed"],
            "additionalProperties": False,
            "properties": {
                "files_changed": {"type": "integer", "minimum": 0},
                "lines_added": {"type": "integer", "minimum": 0},
                "lines_removed": {"type": "integer", "minimum": 0},
            },
        },
    },
}

OUTCOMES_CONFIG = {
    "type": "object",
    "required": ["schema", "name", "seed", "task", "planner", "implementer", "conditions",
                 "arms", "runs_per_arm"],
    "additionalProperties": False,
    "properties": {
        "schema": {"const": "dfa-eval/outcomes-config@1"},
        "name": ID,
        "description": {"type": "string"},
        "seed": {"type": "integer", "minimum": 0},
        "task": {"type": "string", "minLength": 1},
        "planner": MODEL,
        "implementer": MODEL,
        "conditions": {"type": "object", "minProperties": 1, "additionalProperties": CONDITION},
        "arms": {"type": "object", "minProperties": 1,
                 "additionalProperties": {"type": ["string", "null"]}},
        "runs_per_arm": {"type": "integer", "minimum": 1},
        "workspace": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"readme": STR_OR_NULL},
        },
        "limits": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "max_cost_usd": {"type": ["number", "null"], "minimum": 0},
                "jobs": {"type": "integer", "minimum": 1},
                "timeout_s": {"type": "integer", "minimum": 1},
                "implement_timeout_s": {"type": "integer", "minimum": 1},
                "test_timeout_s": {"type": "integer", "minimum": 1},
            },
        },
    },
}

OUTCOME_ATTEMPT = {
    "type": "object",
    "required": ["schema", "attempt_id", "task", "arm", "run_index", "plan_gen_id", "plan_sha256",
                 "rounds"],
    "additionalProperties": False,
    "properties": {
        "schema": {"const": "dfa-eval/outcome-attempt@1"},
        "attempt_id": ID,
        "task": ID,
        "arm": ID,
        "run_index": {"type": "integer", "minimum": 1},
        "plan_gen_id": {"type": ["string", "null"]},
        "plan_sha256": {"type": ["string", "null"], "pattern": r"^[0-9a-f]{64}$"},
        "rounds": {"type": "array", "items": OUTCOME_ROUND},
    },
}

# --------------------------------------------------------------------------------------------
# Structured responses requested from judges (sent to providers)
# --------------------------------------------------------------------------------------------


def judge_response_schema(rubric, checklist=None):
    """The JSON Schema a judge must return for `rubric` (and the prompt's checklist, if used)."""
    n = len(rubric["dimensions"])
    scale = rubric["scale"]
    item = {
        "type": "object",
        "required": ["dim", "score", "evidence", "rationale"],
        "additionalProperties": False,
        "properties": {
            "dim": {"type": "integer", "minimum": 1, "maximum": n},
            "score": {"type": "integer", "minimum": scale["min"], "maximum": scale["max"]},
            "evidence": {"type": "string"},
            "rationale": {"type": "string"},
        },
    }
    out = {
        "type": "object",
        "required": ["dimensions", "note"],
        "additionalProperties": False,
        "properties": {
            "dimensions": {"type": "array", "minItems": n, "maxItems": n, "items": item},
            "note": {"type": "string"},
        },
    }
    if rubric.get("uses_checklists") and checklist:
        ids = [i["id"] for i in checklist["items"]]
        traps = [t["id"] for t in checklist["traps"]]
        out["required"] += ["checklist", "traps"]
        out["properties"]["checklist"] = {
            "type": "array", "minItems": len(ids), "maxItems": len(ids),
            "items": {
                "type": "object",
                "required": ["id", "status", "evidence"],
                "additionalProperties": False,
                "properties": {"id": {"enum": ids},
                               "status": {"type": "integer", "minimum": 0, "maximum": 2},
                               "evidence": {"type": "string"}},
            },
        }
        out["properties"]["traps"] = {
            "type": "array", "minItems": len(traps), "maxItems": len(traps),
            "items": {
                "type": "object",
                "required": ["id", "present", "evidence"],
                "additionalProperties": False,
                "properties": {"id": {"enum": traps}, "present": {"type": "boolean"},
                               "evidence": {"type": "string"}},
            },
        }
    return out


PROBE_RESPONSE = {
    "type": "object",
    "required": ["p_methodology", "cues"],
    "additionalProperties": False,
    "properties": {
        "p_methodology": {"type": "number", "minimum": 0, "maximum": 1},
        "cues": {"type": "array", "maxItems": 10, "items": {"type": "string"}},
    },
}
