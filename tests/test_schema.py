"""Tests for the JSON-Schema-subset validator, eval/dfa_eval/schema.py.

Every supported keyword, the deliberate strictness (bool is not a number, NaN is not JSON, an
unknown keyword fails loudly), readable error paths, and that every schema in schemas.py, including
judge_response_schema for both committed rubrics, is accepted and satisfiable. Read-only: nothing
is written anywhere. Standard library only:  python -m unittest discover -s tests -v
"""

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))
from dfa_eval import schema, schemas  # noqa: E402

validate = schema.validate
NAN, INF = float("nan"), float("inf")
RUBRICS = ROOT / "eval" / "rubrics"


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class TypeTest(unittest.TestCase):
    CASES = {
        "string": (["", "x", "é"], [1, None, True, [], {}]),
        "integer": ([0, -3, 2 ** 70], [1.0, 1.5, True, False, "1", None]),
        "number": ([0, -2, 1.5, 2 ** 70, 1e300], [True, False, "1", None, [], NAN, INF, -INF]),
        "boolean": ([True, False], [0, 1, "true", None]),
        "object": ([{}, {"a": 1}], [[], "x", None, 0]),
        "array": ([[], [1, "a"], (1, 2)], [{}, "x", None]),
        "null": ([None], [0, "", False, [], {}]),
    }

    def test_every_type_name(self):
        for name, (good, bad) in self.CASES.items():
            for value in good:
                self.assertEqual(validate(value, {"type": name}), [], (name, value))
            for value in bad:
                self.assertTrue(validate(value, {"type": name}), (name, value))

    def test_type_list(self):
        nullable = {"type": ["string", "null"]}
        self.assertEqual(validate("x", nullable), [])
        self.assertEqual(validate(None, nullable), [])
        self.assertEqual(validate(5, nullable), ["$: 5 is not of type string or null"])

    def test_bool_is_not_an_integer_or_a_number(self):
        self.assertEqual(validate(True, {"type": "integer"}), ["$: true is not of type integer"])
        self.assertEqual(validate(False, {"type": "number"}), ["$: false is not of type number"])
        self.assertEqual(validate(1, {"type": "boolean"}), ["$: 1 is not of type boolean"])

    def test_integral_float_is_a_number_but_not_an_integer(self):
        # Documented strictness: `"seed": 0.0` must not pass as an integer seed.
        self.assertEqual(validate(2.0, {"type": "number"}), [])
        self.assertEqual(validate(2.0, {"type": "integer"}), ["$: 2.0 is not of type integer"])

    def test_nan_and_infinity_are_rejected_with_or_without_a_type(self):
        for value in (NAN, INF, -INF):
            for s in ({"type": "number"}, {"type": ["number", "null"]}, {}, {"minimum": 0}):
                errors = validate(value, s)
                self.assertEqual(len(errors), 1, (value, s))
                self.assertIn("is not a valid JSON value", errors[0])
        self.assertEqual(validate(NAN, {}), [
            "$: NaN is not a valid JSON value (NaN and infinity are not JSON numbers)"])

    def test_nan_inside_a_part_no_subschema_describes_is_still_found(self):
        free = {"type": "object", "properties": {"config": {"type": "object"},
                                                 "list": {"type": "array"}}}
        errors = validate({"config": {"a": [1, NAN]}, "list": [{"b": INF}], "x": -INF}, free)
        self.assertEqual([e.split(":")[0] for e in errors], ["$.config.a[1]", "$.list[0].b", "$.x"])

    def test_values_with_no_json_form_are_rejected(self):
        for value in (object(), {1, 2}, b"bytes", Path("x")):
            errors = validate(value, {})
            self.assertEqual(len(errors), 1, value)
            self.assertIn("is not a valid JSON value (no JSON form)", errors[0])
        self.assertTrue(validate([object()], {"type": "array"}))

    def test_tuples_count_as_arrays(self):
        s = {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2}
        self.assertEqual(validate((1, 2), s), [])
        self.assertEqual(validate((1, "2"), s), ['$[1]: "2" is not of type integer'])


class EnumConstTest(unittest.TestCase):
    def test_enum(self):
        s = {"enum": ["AI", "non-AI"]}
        self.assertEqual(validate("AI", s), [])
        self.assertEqual(validate("ai", s), ['$: "ai" is not one of ["AI", "non-AI"]'])
        self.assertEqual(validate(None, s), ['$: null is not one of ["AI", "non-AI"]'])

    def test_enum_and_const_do_not_confuse_bool_with_int(self):
        self.assertTrue(validate(True, {"enum": [0, 1]}))
        self.assertTrue(validate(False, {"const": 0}))
        self.assertTrue(validate(1, {"enum": [True, False]}))
        self.assertEqual(validate(True, {"enum": [True]}), [])

    def test_numbers_compare_by_value_as_in_json(self):
        self.assertEqual(validate(1.0, {"enum": [1, 2]}), [])
        self.assertEqual(validate(2, {"const": 2.0}), [])

    def test_const_compares_structures(self):
        s = {"const": {"a": [1, "x"], "b": None}}
        self.assertEqual(validate({"a": [1, "x"], "b": None}, s), [])
        self.assertEqual(validate({"a": (1, "x"), "b": None}, s), [])
        for bad in ({"a": [1, "x"]}, {"a": [1, "x", 2], "b": None},
                    {"a": [True, "x"], "b": None}, {"a": [1, "x"], "b": 0}):
            self.assertTrue(validate(bad, s), bad)

    def test_const_schema_version_message(self):
        self.assertEqual(validate("dfa-eval/config@2", {"const": "dfa-eval/config@1"}),
                         ['$: expected constant "dfa-eval/config@1", got "dfa-eval/config@2"'])

    def test_empty_enum_accepts_nothing(self):
        self.assertTrue(validate("x", {"enum": []}))


class ObjectTest(unittest.TestCase):
    def test_properties_and_required(self):
        s = {"type": "object", "required": ["id", "n"],
             "properties": {"id": {"type": "string"}, "n": {"type": "integer"}}}
        self.assertEqual(validate({"id": "a", "n": 1}, s), [])
        self.assertEqual(validate({"id": "a"}, s), ['$: missing required property "n"'])
        self.assertEqual(validate({}, s), ['$: missing required property "id"',
                                           '$: missing required property "n"'])
        self.assertEqual(validate({"id": 3, "n": 1}, s), ["$.id: 3 is not of type string"])

    def test_required_property_that_is_null_is_present(self):
        s = {"type": "object", "required": ["error"],
             "properties": {"error": {"type": ["string", "null"]}}}
        self.assertEqual(validate({"error": None}, s), [])

    def test_additional_properties_false(self):
        s = {"type": "object", "properties": {"a": {}}, "additionalProperties": False}
        self.assertEqual(validate({"a": 1}, s), [])
        self.assertEqual(validate({"a": 1, "b": 2}, s),
                         ['$: additional property "b" is not allowed'])

    def test_additional_properties_true_or_absent_allows_extras(self):
        for s in ({"type": "object", "properties": {"a": {}}},
                  {"type": "object", "properties": {"a": {}}, "additionalProperties": True}):
            self.assertEqual(validate({"a": 1, "b": 2}, s), [])

    def test_additional_properties_as_a_schema(self):
        files = {"type": "object",
                 "additionalProperties": {"type": "string", "pattern": "^[0-9a-f]{64}$"}}
        self.assertEqual(validate({"SKILL.md": "0" * 64}, files), [])
        self.assertEqual(validate({"SKILL.md": "abc"}, files),
                         ['$["SKILL.md"]: "abc" does not match pattern /^[0-9a-f]{64}$/'])
        conditions = {"type": "object", "properties": {"known": {"type": "integer"}},
                      "additionalProperties": schemas.CONDITION}
        errors = validate({"known": 1, "dfa-capped": {"kind": "skil"}}, conditions)
        self.assertEqual(errors, ['$.dfa-capped: missing required property "description"',
                                  '$.dfa-capped.kind: "skil" is not one of ["plain", "skill"]'])

    def test_min_properties(self):
        s = {"type": "object", "minProperties": 1}
        self.assertEqual(validate({"a": 1}, s), [])
        self.assertEqual(validate({}, s), ["$: 0 properties, fewer than minProperties 1"])

    def test_object_keywords_ignore_other_types(self):
        s = {"required": ["a"], "properties": {"a": {"type": "string"}}, "minProperties": 3,
             "additionalProperties": False}
        for value in ([], "a", 5, None, True):
            self.assertEqual(validate(value, s), [], value)


class ArrayTest(unittest.TestCase):
    def test_items_and_the_spec_example_message(self):
        item = {"dim": 1, "score": 2, "evidence": "e", "rationale": "r"}
        score_item = json.loads(json.dumps(schemas.SCORE_ITEM))
        score_item["properties"]["score"]["maximum"] = 4
        s = {"type": "object", "properties": {"scores": {"type": "array", "items": score_item}}}
        self.assertEqual(validate({"scores": [item, item, dict(item, score=5)]}, s),
                         ["$.scores[2].score: 5 is greater than maximum 4"])

    def test_min_and_max_items(self):
        s = {"type": "array", "minItems": 2, "maxItems": 3}
        self.assertEqual(validate([1, 2], s), [])
        self.assertEqual(validate([1, 2, 3], s), [])
        self.assertEqual(validate([1], s), ["$: 1 items, fewer than minItems 2"])
        self.assertEqual(validate([1, 2, 3, 4], s), ["$: 4 items, more than maxItems 3"])

    def test_every_bad_item_is_reported(self):
        errors = validate([1, "a", 2, None], {"type": "array", "items": {"type": "integer"}})
        self.assertEqual(errors, ['$[1]: "a" is not of type integer',
                                  "$[3]: null is not of type integer"])

    def test_array_keywords_ignore_other_types(self):
        s = {"items": {"type": "integer"}, "minItems": 5, "maxItems": 0}
        for value in ("abc", {"a": "b"}, 3, None):
            self.assertEqual(validate(value, s), [], value)


class NumberAndStringTest(unittest.TestCase):
    def test_minimum_and_maximum_are_inclusive(self):
        s = {"type": "number", "minimum": 0, "maximum": 1}
        for good in (0, 0.0, 0.5, 1, 1.0):
            self.assertEqual(validate(good, s), [], good)
        self.assertEqual(validate(-1, s), ["$: -1 is less than minimum 0"])
        self.assertEqual(validate(1.25, s), ["$: 1.25 is greater than maximum 1"])

    def test_big_integers_compare_exactly(self):
        s = {"type": "integer", "maximum": 2 ** 63}
        self.assertEqual(validate(2 ** 63, s), [])
        self.assertEqual(len(validate(2 ** 63 + 1, s)), 1)

    def test_numeric_bounds_ignore_non_numbers(self):
        s = {"minimum": 3, "maximum": 4}
        for value in ("a", True, None, [], {}):
            self.assertEqual(validate(value, s), [], value)

    def test_min_length_counts_code_points(self):
        s = {"type": "string", "minLength": 1}
        self.assertEqual(validate("é", s), [])
        self.assertEqual(validate("\U0001F600", s), [])
        self.assertEqual(validate("", s), ["$: length 0 is less than minLength 1"])
        self.assertEqual(validate(5, {"minLength": 10}), [])

    def test_pattern_uses_re_search(self):
        self.assertEqual(validate("abc", {"pattern": "b"}), [])
        self.assertEqual(validate("abc", {"pattern": "^b"}),
                         ['$: "abc" does not match pattern /^b/'])
        self.assertEqual(validate("2026-10-02", schemas.DATE), [])
        self.assertTrue(validate("2026-10-2", schemas.DATE))

    def test_a_closing_dollar_is_the_end_of_the_string(self):
        # As in JSON Schema (ECMA-262); Python's `$` would also accept a final newline, and ids
        # become file names.
        self.assertEqual(validate("P1.dfa.g.r01", schemas.ID), [])
        self.assertTrue(validate("P1.dfa.g.r01\n", schemas.ID))
        self.assertEqual(validate("cost$", {"pattern": r"cost\$"}), [])

    def test_pattern_applies_to_strings_only_so_nullable_hashes_work(self):
        nullable_hash = schemas.GENERATION["properties"]["output_sha256"]
        self.assertEqual(validate(None, nullable_hash), [])
        self.assertEqual(validate("ab" * 32, nullable_hash), [])
        self.assertEqual(validate("xyz", nullable_hash),
                         ['$: "xyz" does not match pattern /^[0-9a-f]{64}$/'])
        self.assertEqual(validate(5, nullable_hash), ["$: 5 is not of type string or null"])
        self.assertEqual(validate(5, {"pattern": "^x$"}), [])


class MalformedSchemaTest(unittest.TestCase):
    def assertSchemaError(self, s, *fragments):
        with self.assertRaises(ValueError) as caught:
            validate("anything", s)
        for fragment in fragments:
            self.assertIn(fragment, str(caught.exception))

    def test_unknown_keyword_raises_and_suggests_the_fix(self):
        self.assertSchemaError({"type": "string", "minLenght": 1},
                               "minLenght", "did you mean 'minLength'")

    def test_unknown_keyword_in_an_unreached_branch_still_raises(self):
        nested = [
            {"type": "object", "properties": {"never": {"type": "string", "patern": "x"}}},
            {"type": "array", "items": {"maxitems": 1}},
            {"type": "object", "additionalProperties": {"typo": 1}},
        ]
        for s in nested:
            with self.assertRaises(ValueError):
                validate({}, s)
            with self.assertRaises(ValueError):
                validate([], s)
        with self.assertRaises(ValueError) as caught:
            validate({}, nested[0])
        self.assertIn("schema.properties.never", str(caught.exception))

    def test_annotation_keywords_are_not_in_the_subset(self):
        self.assertSchemaError({"type": "string", "description": "x"}, "'description'")

    def test_malformed_keyword_values_raise(self):
        bad = [
            ({"type": "interger"}, "schema.type"),
            ({"type": []}, "schema.type"),
            ({"type": ["string", "string"]}, "schema.type"),
            ({"type": [{}]}, "schema.type"),
            ({"enum": "AI"}, "schema.enum"),
            ({"required": "id"}, "schema.required"),
            ({"required": [1]}, "schema.required"),
            ({"properties": []}, "schema.properties"),
            ({"additionalProperties": "no"}, "schema.additionalProperties"),
            ({"items": [{"type": "string"}]}, "schema.items"),
            ({"minItems": -1}, "schema.minItems"),
            ({"minLength": 1.5}, "schema.minLength"),
            ({"maxItems": True}, "schema.maxItems"),
            ({"minimum": "0"}, "schema.minimum"),
            ({"maximum": NAN}, "schema.maximum"),
            ({"minimum": True}, "schema.minimum"),
            ({"pattern": "("}, "schema.pattern"),
            ({"pattern": 5}, "schema.pattern"),
        ]
        for s, where in bad:
            with self.assertRaises(ValueError, msg=s) as caught:
                validate("x", s)
            self.assertIn(where, str(caught.exception), s)

    def test_a_schema_must_be_an_object(self):
        for s in (True, None, [], "string"):
            with self.assertRaises(ValueError):
                validate("x", s)

    def test_required_name_forbidden_by_additional_properties_false(self):
        self.assertSchemaError({"type": "object", "required": ["gen_ids"],
                                "additionalProperties": False, "properties": {"gen_id": {}}},
                               "'gen_ids'", "no object can be valid")

    def test_check_schema_is_public(self):
        schema.check_schema({"type": "string"})
        with self.assertRaises(ValueError):
            schema.check_schema({"type": "string", "max": 3})


class ErrorFormatAndCheckTest(unittest.TestCase):
    def test_errors_are_collected_in_a_stable_order(self):
        bad = {"title": "", "kind": "AI?", "request": 7, "tags": ["ok", 3], "extra": 1}
        first = validate(bad, schemas.PROMPT)
        self.assertEqual(first, validate(bad, schemas.PROMPT))
        self.assertEqual(first, [
            '$: missing required property "id"',
            "$.title: length 0 is less than minLength 1",
            '$.kind: "AI?" is not one of ["AI", "non-AI"]',
            "$.request: 7 is not of type string",
            "$.tags[1]: 3 is not of type string",
            '$: additional property "extra" is not allowed',
        ])

    def test_path_argument_prefixes_errors(self):
        self.assertEqual(validate(1, {"type": "string"}, path="$.prompts[3]"),
                         ["$.prompts[3]: 1 is not of type string"])

    def test_awkward_keys_are_quoted_in_paths(self):
        s = {"type": "object", "additionalProperties": {"type": "integer"}}
        errors = validate({"a b": "x", "1st": "x", 'q"uote': "x", "ok_key-2": "x"}, s)
        self.assertEqual([e.split(": ")[0] for e in errors],
                         ['$["a b"]', '$["1st"]', '$["q\\"uote"]', "$.ok_key-2"])

    def test_messages_are_short_and_ascii(self):
        (error,) = validate("→" * 5000, {"type": "string", "pattern": "^x"})
        self.assertLess(len(error), 200)
        self.assertTrue(error.isascii(), error)
        self.assertIn("\\u2192", error)
        (error,) = validate(Path("→"), {})
        self.assertTrue(error.isascii(), error)

    def test_check_returns_valid_instance_and_raises_with_every_error(self):
        record = {"id": "P1", "title": "t", "kind": "AI", "request": "r"}
        self.assertIs(schema.check(record, schemas.PROMPT, "prompt"), record)
        with self.assertRaises(ValueError) as caught:
            schema.check({"id": "bad id", "kind": "x"}, schemas.PROMPT, "prompt P9")
        message = str(caught.exception)
        self.assertTrue(message.startswith("invalid prompt P9 (4 errors):\n  $: "), message)
        self.assertEqual(len(message.splitlines()), 5)
        for fragment in ('missing required property "title"',
                         'missing required property "request"',
                         '$.id: "bad id" does not match pattern', '$.kind: "x" is not one of'):
            self.assertIn(fragment, message)


# Strings that satisfy the patterns used in schemas.py (ID, SHA256, DATE).
PATTERN_SAMPLES = ("a", "0" * 64, "2026-10-02")


def minimal_instance(s):
    """Smallest instance the schema describes: proves the schema is satisfiable."""
    if "const" in s:
        return s["const"]
    if "enum" in s:
        return s["enum"][0]
    types = s.get("type", "null")
    first = types if isinstance(types, str) else types[0]
    if first == "null":
        return None
    if first == "boolean":
        return False
    if first in ("integer", "number"):
        return s.get("minimum", 0)
    if first == "string":
        if "pattern" in s:
            matches = [x for x in PATTERN_SAMPLES if re.search(s["pattern"], x)]
            if not matches:
                raise AssertionError(f"add a sample string for pattern {s['pattern']!r}")
            return matches[0]
        return "x" * s.get("minLength", 0)
    if first == "array":
        return [minimal_instance(s.get("items", {})) for _ in range(s.get("minItems", 0))]
    props = s.get("properties", {})
    extra = s.get("additionalProperties", True)
    extra = extra if isinstance(extra, dict) else {}
    out = {name: minimal_instance(props.get(name, extra)) for name in s.get("required", [])}
    while len(out) < s.get("minProperties", 0):
        out[f"k{len(out)}"] = minimal_instance(extra)
    return out


def module_schemas():
    """Every schema defined at module level in schemas.py, by name."""
    found = {}
    for name, value in vars(schemas).items():
        if not name.isupper() or not isinstance(value, dict):
            continue
        if name == "CALL_FIELDS":  # a mapping of property name -> schema, spliced into records
            found.update({f"CALL_FIELDS.{key}": sub for key, sub in value.items()})
        else:
            found[name] = value
    return found


class SchemasModuleTest(unittest.TestCase):
    def test_every_module_schema_is_accepted_and_satisfiable(self):
        found = module_schemas()
        for expected in ("CONFIG", "MANIFEST", "GENERATION", "BLIND_KEY", "JUDGMENT", "PROBE",
                         "LINT", "STAT", "SUMMARY", "TEST_RESULTS", "OUTCOME_ROUND",
                         "OUTCOME_ATTEMPT", "PROBE_RESPONSE", "CALL_FIELDS.usage"):
            self.assertIn(expected, found)
        for name, s in found.items():
            with self.subTest(schema=name):
                schema.check_schema(s, name)
                instance = minimal_instance(s)
                self.assertEqual(validate(instance, s), [])
                self.assertEqual(validate(json.loads(json.dumps(instance)), s), [])

    def test_a_realistic_config_and_its_common_mistakes(self):
        config = {
            "schema": "dfa-eval/config@1", "name": "core-v2", "seed": 0,
            "prompts": [{
                "id": "P1", "title": "RAG support chatbot", "kind": "AI",
                "request": "Plan a customer-support RAG chatbot over our help-center docs.",
                "checklist": "eval/rubrics/checklists/P1.json", "tags": [],
            }],
            "conditions": {
                "baseline": {"kind": "plain", "description": "The request alone."},
                "dfa-capped": {"kind": "skill", "description": "Skill, capped.", "skill_dir": ".",
                               "skill_name": "dependency-first-architect",
                               "length_cap_words": 1500},
            },
            "generators": [{"id": "opus-5.5", "provider": "claude-cli",
                            "model": "claude-opus-5-5", "effort": "high"}],
            "runs_per_cell": 5,
            "limits": {"max_cost_usd": None, "jobs": 8, "timeout_s": 3600},
        }
        self.assertEqual(validate(config, schemas.CONFIG), [])
        broken = json.loads(json.dumps(config))
        broken["seed"] = 0.0
        broken["generators"][0]["provider"] = "claude"
        broken["conditions"]["dfa-capped"]["length_cap_words"] = 10
        broken["judge"] = []
        self.assertEqual(validate(broken, schemas.CONFIG), [
            "$.seed: 0.0 is not of type integer",
            "$.conditions.dfa-capped.length_cap_words: 10 is less than minimum 50",
            '$.generators[0].provider: "claude" is not one of ["claude-cli", '
            '"openai-compatible", "command", "fake"]',
            '$: additional property "judge" is not allowed',
        ])

    def test_a_realistic_generation_record(self):
        gid = "P1.dfa.opus-5.5.r01"
        record = {
            "schema": "dfa-eval/generation@1", "gen_id": gid, "prompt_id": "P1",
            "condition": "dfa", "generator": "opus-5.5", "run_index": 1, "seed": 123,
            "seed_honored": False, "request": "/dependency-first-architect Plan a chatbot.",
            "status": "ok", "error": None, "output_file": f"generations/{gid}.md",
            "output_sha256": "ab" * 32,
            "metrics": {"words": 900, "chars": 6000, "visible_tokens_est": 1500}, "turns": 3,
            "tool_calls": [{"tool": "Read", "path": "skills/x/SKILL.md", "ok": True}],
            "skills_available": ["dependency-first-architect"],
            "instructions_loaded": [{"path": "skills/x/SKILL.md", "sha256": "cd" * 32,
                                     "words": 2000}],
            "started_at": "2026-10-02T12:00:00Z", "finished_at": "2026-10-02T12:03:00Z",
            "provider": "claude-cli", "model_requested": "claude-opus-5-5",
            "models_used": ["claude-opus-5-5"], "model_mismatch": False,
            "usage": {"input_tokens": 10, "output_tokens": 2000}, "cost_usd": 0.42,
            "latency_s": 180.5, "raw_file": f"raw/gen-{gid}.txt",
        }
        self.assertEqual(validate(record, schemas.GENERATION), [])
        failed = dict(record, status="error", error="timeout", output_file=None,
                      output_sha256=None, metrics=None, usage=None, cost_usd=None, latency_s=None)
        self.assertEqual(validate(failed, schemas.GENERATION), [])
        wrong = dict(record, run_index=True, cost_usd=NAN, usage={"output_tokens": 1.5})
        self.assertEqual(validate(wrong, schemas.GENERATION), [
            "$.run_index: true is not of type integer",
            "$.usage.output_tokens: 1.5 is not of type integer or null",
            "$.cost_usd: NaN is not a valid JSON value (NaN and infinity are not JSON numbers)",
        ])


class JudgeResponseSchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.adherence = load(RUBRICS / "methodology-adherence-v1.json")
        cls.quality = load(RUBRICS / "engineering-quality-v1.json")
        cls.p1 = load(RUBRICS / "checklists" / "P1.json")

    @staticmethod
    def response(rubric, checklist=None, score=None):
        top = rubric["scale"]["max"] if score is None else score
        out = {"dimensions": [{"dim": d["id"], "score": top, "evidence": "quote",
                               "rationale": "why"} for d in rubric["dimensions"]],
               "note": ""}
        if checklist is not None:
            out["checklist"] = [{"id": i["id"], "status": 2, "evidence": "q"}
                                for i in checklist["items"]]
            out["traps"] = [{"id": t["id"], "present": False, "evidence": ""}
                            for t in checklist["traps"]]
        return out

    def round_trip(self, rubric, checklist):
        s = schemas.judge_response_schema(rubric, checklist)
        schema.check_schema(s)
        self.assertEqual(json.loads(json.dumps(s)), s)  # sent to providers as JSON text
        return s

    def test_methodology_adherence_rubric(self):
        s = self.round_trip(self.adherence, self.p1)  # uses_checklists is false: checklist ignored
        self.assertNotIn("checklist", s["properties"])
        good = self.response(self.adherence)
        self.assertEqual(len(good["dimensions"]), 10)
        self.assertEqual(validate(good, s), [])
        self.assertEqual(validate(json.loads(json.dumps(good)), s), [])
        bad = self.response(self.adherence)
        bad["dimensions"][0]["score"] = 3
        self.assertEqual(validate(bad, s), ["$.dimensions[0].score: 3 is greater than maximum 2"])
        short = self.response(self.adherence)
        del short["dimensions"][-1]
        self.assertEqual(validate(short, s), ["$.dimensions: 9 items, fewer than minItems 10"])
        extra = self.response(self.adherence, self.p1)
        self.assertEqual(validate(extra, s), ['$: additional property "checklist" is not allowed',
                                              '$: additional property "traps" is not allowed'])

    def test_engineering_quality_rubric_with_p1_checklist(self):
        s = self.round_trip(self.quality, self.p1)
        good = self.response(self.quality, self.p1)
        self.assertEqual((len(good["dimensions"]), len(good["checklist"]), len(good["traps"])),
                         (11, 11, 5))
        self.assertEqual(validate(good, s), [])
        self.assertEqual(validate(json.loads(json.dumps(good)), s), [])
        self.assertEqual(validate(self.response(self.quality, self.p1, score=0), s), [])

        bad = self.response(self.quality, self.p1)
        bad["dimensions"][2]["score"] = 5
        bad["dimensions"][3]["dim"] = 12
        bad["dimensions"][4]["score"] = True
        bad["checklist"][0]["id"] = "P2-01"
        bad["checklist"][1]["status"] = 3
        bad["traps"][0]["present"] = 1
        del bad["note"]
        p1_ids = ", ".join(f'"P1-{i:02d}"' for i in range(1, 12))
        self.assertEqual(validate(bad, s), [
            '$: missing required property "note"',
            "$.dimensions[2].score: 5 is greater than maximum 4",
            "$.dimensions[3].dim: 12 is greater than maximum 11",
            "$.dimensions[4].score: true is not of type integer",
            f'$.checklist[0].id: "P2-01" is not one of [{p1_ids}]',
            "$.checklist[1].status: 3 is greater than maximum 2",
            "$.traps[0].present: 1 is not of type boolean",
        ])
        missing = self.response(self.quality, self.p1)
        del missing["traps"]
        self.assertEqual(validate(missing, s), ['$: missing required property "traps"'])

    def test_engineering_quality_rubric_without_a_checklist(self):
        s = self.round_trip(self.quality, None)
        self.assertEqual(s["required"], ["dimensions", "note"])
        self.assertEqual(validate(self.response(self.quality), s), [])

    def test_every_committed_checklist_builds_a_valid_schema(self):
        paths = sorted((RUBRICS / "checklists").glob("P*.json"))
        self.assertEqual(len(paths), 8)
        for path in paths:
            with self.subTest(checklist=path.name):
                checklist = load(path)
                s = self.round_trip(self.quality, checklist)
                self.assertEqual(validate(self.response(self.quality, checklist), s), [])


if __name__ == "__main__":
    unittest.main()
