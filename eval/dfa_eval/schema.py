"""Validate JSON data against the small subset of JSON Schema that `schemas.py` uses.

Every record the harness writes or reads, and every structured reply a judge model returns, is
checked here before it is trusted. The standard library has no validator, hence this one.

Supported keywords, exactly, with JSON Schema semantics: each one applies only when the instance
has the matching type (`pattern` ignores null, `minimum` ignores strings, and so on).

    type              a name or a list of names: string integer number boolean object array null
    enum, const       JSON equality: 1 equals 1.0, but true does not equal 1
    properties, required, additionalProperties (true, false or a schema), minProperties
    items             one schema applied to every element
    minItems, maxItems
    minimum, maximum  inclusive
    minLength         counted in code points
    pattern           re.search, so unanchored unless the pattern anchors itself

Deliberately strict, because these schemas guard committed records and configs:
- Any other keyword, or a malformed keyword value, raises ValueError wherever it sits in the
  schema, even in a branch the instance never reaches, so a typo fails loudly instead of quietly
  accepting everything. So does a `required` name that `additionalProperties: false` forbids.
- bool is not an integer or a number. An integer is an integral JSON literal (a Python int):
  2.0 is a number but not an integer, so `"seed": 0.0` cannot slip into a config and quietly
  change every derived seed.
- NaN and +/-infinity are invalid wherever they appear (they are not JSON), as is any other value
  with no JSON form. Tuples count as arrays, because json.dumps writes them as arrays.
"""

import difflib
import json
import math
import re

TYPES = ("string", "integer", "number", "boolean", "object", "array", "null")
KEYWORDS = frozenset({
    "type", "enum", "const", "properties", "required", "additionalProperties", "minProperties",
    "items", "minItems", "maxItems", "minimum", "maximum", "minLength", "pattern",
})
_COUNTS = ("minProperties", "minItems", "maxItems", "minLength")
_BOUNDS = ("minimum", "maximum")
_PLAIN_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")  # shown as .key; anything else as ["key"]


def _kind(value):
    """JSON type of a Python value ("integer" for ints), or None if it has no JSON form."""
    if value is None:
        return "null"
    if isinstance(value, bool):  # before int: bool is an int subclass
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number" if math.isfinite(value) else None
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        return "array"
    if isinstance(value, dict):
        return "object"
    return None


def _has_type(value, name):
    kind = _kind(value)
    return kind == name or (name == "number" and kind == "integer")


def _equal(a, b):
    """JSON equality: numbers by value, everything else only within the same JSON type."""
    ka, kb = _kind(a), _kind(b)
    if ka in ("integer", "number") and kb in ("integer", "number"):
        return a == b
    if ka != kb or ka is None:
        return False
    if ka == "array":
        return len(a) == len(b) and all(_equal(x, y) for x, y in zip(a, b))
    if ka == "object":
        return a.keys() == b.keys() and all(_equal(a[k], b[k]) for k in a)
    return a == b


def _show(value, limit=80):
    """Short, ASCII-only rendering of a value for an error message."""
    try:
        text = json.dumps(value)
    except (TypeError, ValueError):
        text = ascii(value)
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _child(path, key):
    if isinstance(key, str) and _PLAIN_KEY.match(key):
        return f"{path}.{key}"
    return f"{path}[{json.dumps(str(key))}]"


def _is_count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def check_schema(schema, path="schema"):
    """Raise ValueError if `schema` is not a well-formed schema in the supported subset.

    Walks every branch, so a typo is caught even where no instance would ever reach it.
    """
    if not isinstance(schema, dict):
        raise ValueError(f"{path}: a schema must be an object, got {_show(schema)}")
    for key in schema:
        if key not in KEYWORDS:
            close = difflib.get_close_matches(str(key), sorted(KEYWORDS), n=1)
            hint = f" (did you mean {close[0]!r}?)" if close else ""
            raise ValueError(f"{path}: unknown schema keyword {key!r}{hint}; supported: "
                             f"{', '.join(sorted(KEYWORDS))}")
    if "type" in schema:
        names = schema["type"]
        names = [names] if isinstance(names, str) else names
        if (not isinstance(names, (list, tuple)) or not names
                or not all(isinstance(n, str) and n in TYPES for n in names)
                or len(set(names)) != len(names)):
            raise ValueError(f"{path}.type: {_show(schema['type'])} is not a type name or a list "
                             f"of distinct type names ({', '.join(TYPES)})")
    if "enum" in schema and not isinstance(schema["enum"], (list, tuple)):
        raise ValueError(f"{path}.enum: must be an array, got {_show(schema['enum'])}")
    for key in _COUNTS:
        if key in schema and not _is_count(schema[key]):
            raise ValueError(f"{path}.{key}: must be a non-negative integer, "
                             f"got {_show(schema[key])}")
    for key in _BOUNDS:
        value = schema.get(key)
        if key in schema and (_kind(value) not in ("integer", "number")):
            raise ValueError(f"{path}.{key}: must be a finite number, got {_show(value)}")
    if "pattern" in schema:
        if not isinstance(schema["pattern"], str):
            raise ValueError(f"{path}.pattern: must be a string, got {_show(schema['pattern'])}")
        try:
            re.compile(schema["pattern"])
        except re.error as exc:
            raise ValueError(f"{path}.pattern: invalid regular expression: {exc}") from None
    required = schema.get("required", [])
    if not isinstance(required, (list, tuple)) or not all(isinstance(r, str) for r in required):
        raise ValueError(f"{path}.required: must be an array of strings, got {_show(required)}")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise ValueError(f"{path}.properties: must be an object, got {_show(properties)}")
    for name, sub in properties.items():
        if not isinstance(name, str):
            raise ValueError(f"{path}.properties: property names must be strings, "
                             f"got {_show(name)}")
        check_schema(sub, _child(f"{path}.properties", name))
    extra = schema.get("additionalProperties", True)
    if isinstance(extra, dict):
        check_schema(extra, f"{path}.additionalProperties")
    elif not isinstance(extra, bool):
        raise ValueError(f"{path}.additionalProperties: must be a boolean or a schema, "
                         f"got {_show(extra)}")
    if extra is False:
        for name in required:
            if name not in properties:
                raise ValueError(f"{path}.required: {name!r} is not in properties and "
                                 "additionalProperties is false, so no object can be valid")
    if "items" in schema:
        check_schema(schema["items"], f"{path}.items")


def _pattern_matches(pattern, value):
    """re.search, except that a closing `$` means the end of the string, as in JSON Schema's
    ECMA-262 regexes (Python's `$` also matches before a final newline)."""
    match = re.search(pattern, value)
    return bool(match) and not (pattern.endswith("$") and not pattern.endswith("\\$")
                                and match.end() != len(value))


def validate(instance, schema, path="$"):
    """Return the list of human-readable errors for `instance`; an empty list means valid.

    Errors look like `$.scores[2].score: 5 is greater than maximum 4` and come in a stable order
    (the schema's required list, then the instance's own key order). Raises ValueError if the
    schema itself is malformed or uses an unsupported keyword.
    """
    check_schema(schema)
    errors = []
    _validate(instance, schema, path, errors)
    return errors


def check(instance, schema, what="instance"):
    """Return `instance` if it is valid, else raise ValueError listing every error."""
    errors = validate(instance, schema)
    if errors:
        plural = "" if len(errors) == 1 else "s"
        raise ValueError(f"invalid {what} ({len(errors)} error{plural}):\n"
                         + "\n".join(f"  {error}" for error in errors))
    return instance


def _not_json(value, path, errors):
    why = "NaN and infinity are not JSON numbers" if isinstance(value, float) else "no JSON form"
    errors.append(f"{path}: {_show(value)} is not a valid JSON value ({why})")


def _scan(value, path, errors):
    """Report non-JSON values inside a part of the instance that no subschema describes."""
    kind = _kind(value)
    if kind is None:
        _not_json(value, path, errors)
    elif kind == "object":
        for key, item in value.items():
            _scan(item, _child(path, key), errors)
    elif kind == "array":
        for index, item in enumerate(value):
            _scan(item, f"{path}[{index}]", errors)


def _validate(value, schema, path, errors):
    kind = _kind(value)
    if kind is None:
        _not_json(value, path, errors)
        return
    if "type" in schema:
        names = schema["type"]
        names = [names] if isinstance(names, str) else names
        if not any(_has_type(value, name) for name in names):
            errors.append(f"{path}: {_show(value)} is not of type {' or '.join(names)}")
            return
    if "enum" in schema and not any(_equal(value, option) for option in schema["enum"]):
        errors.append(f"{path}: {_show(value)} is not one of {_show(list(schema['enum']), 200)}")
    if "const" in schema and not _equal(value, schema["const"]):
        errors.append(f"{path}: expected constant {_show(schema['const'])}, got {_show(value)}")

    if kind == "object":
        properties = schema.get("properties", {})
        for name in schema.get("required", ()):
            if name not in value:
                errors.append(f"{path}: missing required property {_show(name)}")
        if "minProperties" in schema and len(value) < schema["minProperties"]:
            errors.append(f"{path}: {len(value)} properties, fewer than minProperties "
                          f"{schema['minProperties']}")
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if isinstance(key, str) and key in properties:
                _validate(item, properties[key], _child(path, key), errors)
            elif extra is False:
                errors.append(f"{path}: additional property {_show(key)} is not allowed")
            elif isinstance(extra, dict):
                _validate(item, extra, _child(path, key), errors)
            else:
                _scan(item, _child(path, key), errors)
    elif kind == "array":
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: {len(value)} items, fewer than minItems {schema['minItems']}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: {len(value)} items, more than maxItems {schema['maxItems']}")
        for index, item in enumerate(value):
            if "items" in schema:
                _validate(item, schema["items"], f"{path}[{index}]", errors)
            else:
                _scan(item, f"{path}[{index}]", errors)
    elif kind in ("integer", "number"):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {_show(value)} is less than minimum {_show(schema['minimum'])}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {_show(value)} is greater than maximum "
                          f"{_show(schema['maximum'])}")
    elif kind == "string":
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: length {len(value)} is less than minLength "
                          f"{schema['minLength']}")
        if "pattern" in schema and not _pattern_matches(schema["pattern"], value):
            errors.append(f"{path}: {_show(value)} does not match pattern /{schema['pattern']}/")
