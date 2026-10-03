"""Deterministic format checks for a plan against plan-template v2 (reference/plan-template.md).

Lint measures FORMAT adherence: the template's ten sections in order, its tables with the
required columns, labeled budgets and thresholds, V-IDs that resolve, exceptions with every field,
and no printed self-score. That is methodology adherence by construction, not plan quality: the
checks are the template's own rules, so a plan written with the skill is expected to pass them,
and a plan written without it (or with the v1 template) is expected to fail most of them whatever
its engineering merit. Quality is what the judged engineering-quality rubric is for.

    lint_plan(text, kind=None) -> dict     one plan (schemas.LINT minus gen_id); kind "AI",
                                           "non-AI" or None (None skips ai_layer.consistent)
    run_lint(run_dir) -> list[dict]        writes lint/<gen_id>.json for every ok generation,
                                           kind taken from the prompt config in the manifest
    check_lint(run_dir) -> (ok, diff)      recomputes every lint record and byte-compares

Parsing is forgiving about markdown style and strict about content. Headings are ATX headings
outside fenced code. Sections are found by heading keywords (case-insensitive, numbering
optional); a section runs to the next heading of the same or a higher level, or to the next
section heading. Tables are GFM pipe tables with or without outer pipes and with any alignment
row; `|` inside a code span or escaped as `\\|` stays in its cell; bold, italic, code, links and
<br> are stripped before a cell is read; a table counts anywhere inside its section. A cell is
empty if it reads "", "…", "...", "-", "—" or "–" after stripping. Each content check finds its
own section, so a plan with sections out of order fails `sections.present` only.

A check whose structure is missing (no section, no table, no column) fails and says so: format
adherence gets no credit by default, so a v1 or baseline plan cannot pass a check vacuously.
"""

import difflib
import re

from . import records, schema, schemas

TEMPLATE = "plan-template-v2"

CHECK_IDS = (
    "sections.present",
    "self_score.absent",
    "budgets.labeled",
    "budgets.unknown_has_no_number",
    "dependencies.typed",
    "gates.columns",
    "gates.no_r1_rows",
    "gates.r3_backed",
    "validation.complete",
    "validation.v0",
    "validation.no_dangling",
    "validation.labeled",
    "crosscutting.complete",
    "exceptions.complete",
    "deferred.present",
    "ai_layer.consistent",
)

# (key used in details, heading keyword) in template order.
SECTIONS = (
    ("classification", re.compile(r"classification")),
    ("dependency map", re.compile(r"dependenc(?:y|ies)[\s-]+map")),
    ("tradeoff gates", re.compile(r"trade[\s-]?offs?[\s-]+gates?")),
    ("walking skeleton", re.compile(r"walking[\s-]+skeleton")),
    ("phases", re.compile(r"\bphases\b")),
    ("validation gates", re.compile(r"validation[\s-]+gates?")),
    ("cross-cutting", re.compile(r"cross[\s-]?cutting")),
    ("ai layer", re.compile(r"\bai[\s-]+layer\b")),
    ("exceptions", re.compile(r"\bexceptions?\b")),
    ("deferred", re.compile(r"\bdeferr?ed\b")),
)
SECTION_NUMBER = {key: n for n, (key, _) in enumerate(SECTIONS, 1)}

FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
HEADING = re.compile(r"^ {0,3}(#{1,6})(?=[ \t]|$)(.*)$")
DELIMITER_CELL = re.compile(r"^:?-+:?$")
BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S")
# The v1 self-score heading (examples/scoring/run_eval.py SELF_SCORE), and a line that states a
# /20 as the plan's own score: it starts, after list, quote, table and emphasis markup and an
# optional item number, with Total, Score, Eval score or a self-grading word ("**Total: 19/20**",
# "Self-check total: 19/20", "| **Total** | 19/20 |"), or it is a heading that mentions a score.
# Merely containing N/20 and "score" ("at least 18/20 golden questions scored correct") is a
# threshold, not a self-score.
SELF_SCORE = re.compile(r"^#{1,6}\s*(?:\d+\.\s*)?(?:Eval score|Self[- ]?score)\b", re.I)
OUT_OF_20 = re.compile(r"\b\d{1,2}\s*/\s*20\b")
LEADING_MARKUP = re.compile(r"^(?:[\s>|*_~#+-]|\d{1,2}[.)](?=\s))*")
SCORE_START = re.compile(r"^(?:total|score|eval(?:uation)?\s+score"
                         r"|self[- ]?(?:scor|check|assess|grad|rat|evaluat)[a-z]*)(?![a-z0-9])",
                         re.I)
SCORE_MENTION = re.compile(r"\bscore\b", re.I)
V_ID = re.compile(r"\bV\d+\b")
# Phase, gate and section references are not budget numbers: the template asks an UNKNOWN
# target to name the phase that needs it ("supplied before Phase 4", "V3 load test").
REFERENCE = re.compile(r"\bphases?\s*\d+(?:\s*(?:[-–—,/]|and|to|or)\s*\d+)*(?:\s+wave\s+\d+)?\b"
                       r"|\b[VEP]\d+[a-z]?\b|§\s*\d+", re.I)
LABEL_START = re.compile(r"^(?:requirement|baseline|assumption|unknown)\b", re.I)
LABEL_ANY = re.compile(r"\b(?:requirement|baseline|assumption|unknown)\b", re.I)
SIGN_OFF = re.compile(r"sign-off|signs off|sign off|approval", re.I)
BASIS = re.compile(r"requirement|basis|headroom|limit", re.I)
GATED_KIND = re.compile(
    r"\b(?:decision|validation|risk\s*(?:/|-|&|and)?\s*security|organi[sz]ational|economic)\b",
    re.I)
EMPTY = frozenset({"", "…", "...", "-", "—", "–"})
NONE_BODY = re.compile(r"^none\b", re.I)  # deferred: "says None"
NONE_ONLY = re.compile(r"^none\.?$", re.I)  # exceptions: "is None", possibly with a period

GATE_COLUMNS = (("decision", "Decision"), ("reversibility", "Reversibility"),
                ("default", "Default"), ("assumption", "Assumption"),
                ("validated", "Validated by"), ("flip", "Flip condition"))
VALIDATION_COLUMNS = (("id", "ID"), ("hypothesis", "Hypothesis"), ("method", "Method"),
                      ("acceptance", "Acceptance threshold"), ("evidence", "Evidence"),
                      ("unlocks", "Unlocks"), ("phase", "Phase"))
CROSSCUTTING_COLUMNS = (("phase", "Phase"), ("security", "Security"),
                        ("observability", "Observability"),
                        ("reproducibility", "Reproducibility"), ("resilience", "Resilience"))
EXCEPTION_COLUMNS = (("id", "ID"), ("rule", "Rule bypassed"), ("why", "Why"),
                     ("replacement", "Replacement validation"), ("evidence", "Evidence"),
                     ("resumes", "Resumes"))


# --- markdown ------------------------------------------------------------------------------

def plain(cell):
    """Cell or heading text with markdown markup removed and whitespace collapsed."""
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", cell)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\[[^\]]*\]", r"\1", text)
    text = re.sub(r"<((?:https?|mailto):[^>\s]+)>", r"\1", text)
    text = re.sub(r"</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?/?>", " ", text)
    text = text.replace("`", "")
    text = re.sub(r"\\(.)", r"\1", text)
    text = text.replace("*", "").replace("~~", "")
    text = re.sub(r"(?<![A-Za-z0-9])_+|_+(?![A-Za-z0-9])", "", text)
    return " ".join(text.split())


def is_empty(cell):
    return plain(cell) in EMPTY


def _closing_ticks(line, start, count):
    """Index of the next run of exactly `count` backticks at or after `start`, or -1."""
    i = start
    while True:
        i = line.find("`", i)
        if i < 0:
            return -1
        j = i
        while j < len(line) and line[j] == "`":
            j += 1
        if j - i == count:
            return i
        i = j


def split_row(line):
    """Cells of a pipe-table row, or None if the line has no cell separator.

    A `|` inside a code span or escaped as `\\|` is cell content, not a separator. Outer pipes
    are optional.
    """
    s = line.strip()
    cells, buf, seps, trailing = [], [], 0, False
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch == "\\" and i + 1 < n:
            buf.append(s[i:i + 2])
            i += 2
            continue
        if ch == "`":
            j = i
            while j < n and s[j] == "`":
                j += 1
            close = _closing_ticks(s, j, j - i)
            end = j if close < 0 else close + (j - i)
            buf.append(s[i:end])
            i = end
            continue
        if ch == "|":
            cells.append("".join(buf))
            buf, seps = [], seps + 1
            i += 1
            trailing = i == n
            continue
        buf.append(ch)
        i += 1
    cells.append("".join(buf))
    if not seps:
        return None
    if s.startswith("|"):
        cells = cells[1:]
    if trailing:
        cells = cells[:-1]
    return [c.strip() for c in cells]


class Heading:
    def __init__(self, index, level, text):
        self.index, self.level, self.text = index, level, text
        self.norm = plain(text).lower()


class Table:
    def __init__(self, line, header, rows):
        self.line = line
        self.header = header
        self.norm = [plain(h).lower() for h in header]
        self.rows = rows  # every row padded or cut to the header's width

    def col(self, prefix):
        """Index of the first column whose header starts with `prefix` (lowercase), or None."""
        for k, name in enumerate(self.norm):
            if name.startswith(prefix):
                return k
        return None

    def missing(self, columns):
        return [label for prefix, label in columns if self.col(prefix) is None]


class Section:
    def __init__(self, doc, key, heading, end):
        self.doc, self.key, self.heading = doc, key, heading
        self.start, self.end = heading.index + 1, end

    @property
    def name(self):
        return f"section {SECTION_NUMBER[self.key]} ({self.key})"

    def body_lines(self):
        return [self.doc.lines[i] for i in range(self.start, self.end) if not self.doc.code[i]]

    def body(self):
        return plain(" ".join(self.body_lines()))

    def tables(self):
        return self.doc.tables(self.start, self.end)

    def table_with(self, *prefixes):
        """First table in the section that has every column in `prefixes`, or None."""
        for table in self.tables():
            if all(table.col(p) is not None for p in prefixes):
                return table
        return None


class Document:
    def __init__(self, text):
        self.text = text.replace("\r\n", "\n").replace("\r", "\n")
        self.lines = self.text.split("\n")
        self.code = self._code_mask()
        self.headings = []
        for i, line in enumerate(self.lines):
            match = None if self.code[i] else HEADING.match(line)
            if match:
                title = re.sub(r"(?:^|[ \t]+)#+$", "", match.group(2).strip()).strip()
                self.headings.append(Heading(i, len(match.group(1)), title))
        self._locate_sections()

    def _code_mask(self):
        mask, fence = [], None
        for line in self.lines:
            match = FENCE.match(line)
            if fence is None:
                opens = match and not (match.group(1)[0] == "`" and "`" in match.group(2))
                if opens:
                    fence = match.group(1)
                mask.append(bool(opens))
            else:
                mask.append(True)
                if (match and match.group(1)[0] == fence[0]
                        and len(match.group(1)) >= len(fence) and not match.group(2).strip()):
                    fence = None
        return mask

    def _locate_sections(self):
        # The in-order chain: each section's heading is the first match after the previous one.
        self.chain, position = {}, -1
        for key, pattern in SECTIONS:
            for heading in self.headings:
                if heading.index > position and pattern.search(heading.norm):
                    self.chain[key] = heading
                    position = heading.index
                    break
        # Content checks find each section on its own, so an order error costs one check.
        found = dict(self.chain)
        for key, pattern in SECTIONS:
            if key not in found:
                hit = next((h for h in self.headings if pattern.search(h.norm)), None)
                if hit:
                    found[key] = hit
        starts = {h.index for h in found.values()}
        self.sections = {}
        for key, heading in found.items():
            end = len(self.lines)
            for other in self.headings:
                if other.index > heading.index and (other.level <= heading.level
                                                    or other.index in starts):
                    end = other.index
                    break
            self.sections[key] = Section(self, key, heading, end)

    def section(self, key):
        return self.sections.get(key)

    def tables(self, start, end):
        out, i = [], start
        while i < end - 1:
            if self.code[i] or self.code[i + 1]:
                i += 1
                continue
            header = split_row(self.lines[i])
            delimiter = split_row(self.lines[i + 1])
            if (header and delimiter and not HEADING.match(self.lines[i])
                    and all(DELIMITER_CELL.match(c) for c in delimiter)):
                width, rows, j = len(header), [], i + 2
                while j < end and not self.code[j] and self.lines[j].strip():
                    if HEADING.match(self.lines[j]):
                        break
                    cells = split_row(self.lines[j])
                    if cells is None:
                        break
                    rows.append((cells + [""] * width)[:width])
                    j += 1
                out.append(Table(i, header, rows))
                i = j
            else:
                i += 1
        return out


# --- checks --------------------------------------------------------------------------------

def _short(text, limit=40):
    text = plain(text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _names(items, limit=6):
    items = list(items)
    shown = ", ".join(items[:limit])
    return shown + (f" and {len(items) - limit} more" if len(items) > limit else "")


def _v_order(vid):
    return int(vid[1:]), vid


def _row_label(table, row):
    return _short(row[0]) or f"row {table.rows.index(row) + 1}"


def _missing_section(key):
    return False, f"section {SECTION_NUMBER[key]} ({key}) not found"


def check_sections(doc):
    missing = [key for key, pattern in SECTIONS
               if not any(pattern.search(h.norm) for h in doc.headings)]
    out_of_order = [key for key, _ in SECTIONS if key not in doc.chain and key not in missing]
    if not missing and not out_of_order:
        return True, "all 10 sections found in template order"
    parts = []
    if missing:
        parts.append("missing: " + ", ".join(missing))
    if out_of_order:
        parts.append("out of order: " + ", ".join(out_of_order))
    return False, "; ".join(parts)


def states_self_score(line):
    """True if a line (outside code) states a /20 self-score; see SCORE_START."""
    if not OUT_OF_20.search(line):
        return False
    heading = HEADING.match(line)
    if heading and SCORE_MENTION.search(heading.group(2)):
        return True
    return bool(SCORE_START.match(LEADING_MARKUP.sub("", line)))


def check_self_score(doc):
    for i, line in enumerate(doc.lines):
        if not doc.code[i] and SELF_SCORE.match(line.strip()):
            return False, f"self-score heading on line {i + 1}: {_short(line, 50)}"
    for i, line in enumerate(doc.lines):
        if not doc.code[i] and states_self_score(line):
            return False, f"line {i + 1} states a /20 total: {_short(line, 50)}"
    return True, "no self-score heading and no /20 total"


def _budget_table(doc):
    section = doc.section("classification")
    if section is None:
        return None, _missing_section("classification")
    table = section.table_with("budget", "label")
    if table is None:
        return None, (False, f"no table with Budget and Label columns in {section.name}")
    return table, None


def check_budgets_labeled(doc):
    table, failure = _budget_table(doc)
    if failure:
        return failure
    if not table.rows:
        return False, "budget table has no rows"
    label = table.col("label")
    bad = [_row_label(table, row) for row in table.rows if not LABEL_START.match(plain(row[label]))]
    if bad:
        return False, ("Label does not start with REQUIREMENT, BASELINE, ASSUMPTION or UNKNOWN: "
                       + _names(bad))
    return True, f"{len(table.rows)} budget rows, every Label starts with a label"


def check_unknown_has_no_number(doc):
    table, failure = _budget_table(doc)
    if failure:
        return failure
    target = table.col("target")
    if target is None:
        return False, "budget table has no Target column"
    label = table.col("label")
    unknown = [row for row in table.rows if plain(row[label]).lower().startswith("unknown")]
    bad = [_row_label(table, row) for row in unknown
           if re.search(r"\d", REFERENCE.sub("", plain(row[target])))]
    if bad:
        return False, "UNKNOWN rows with a number in Target: " + _names(bad)
    return True, f"{len(unknown)} UNKNOWN rows, none with a number in Target"


def check_dependencies(doc):
    section = doc.section("dependency map")
    if section is None:
        return _missing_section("dependency map")
    table = section.table_with("kind")
    if table is None:
        return False, f"no table with a Kind column in {section.name}"
    kind = table.col("kind")
    kinds = [plain(row[kind]) for row in table.rows]
    typed = [k for k in kinds if GATED_KIND.search(k)]
    if not typed:
        found = _names(sorted({k.lower() for k in kinds if k})) or "no rows"
        return False, ("no row of kind decision, validation, risk/security, organizational or "
                       f"economic (found: {found})")
    return True, (f"{len(table.rows)} rows, {len(typed)} of a decision/validation/risk/org/"
                  "economic kind")


def _gates_table(doc):
    section = doc.section("tradeoff gates")
    if section is None:
        return None, _missing_section("tradeoff gates")
    tables = section.tables()
    if not tables:
        return None, (False, f"no table in {section.name}")
    return section.table_with("decision") or tables[0], None


def check_gate_columns(doc):
    table, failure = _gates_table(doc)
    if failure:
        return failure
    missing = table.missing(GATE_COLUMNS)
    if missing:
        return False, "gates table lacks columns: " + ", ".join(missing)
    return True, "gates table has Decision, Reversibility, Default, Assumption, Validated by, Flip"


def _gate_rows(table):
    """Rows that are real gates: a Decision or Default cell starting with N/A is skipped."""
    skip = [table.col("decision"), table.col("default")]
    return [row for row in table.rows
            if not any(k is not None and plain(row[k]).lower().startswith("n/a") for k in skip)]


def check_no_r1_rows(doc):
    table, failure = _gates_table(doc)
    if failure:
        return failure
    rev = table.col("reversibility")
    if rev is None:
        return False, "gates table has no Reversibility column"
    if not table.rows:
        return False, "gates table has no rows"
    rows = _gate_rows(table)
    bad = []
    for row in rows:
        cell = plain(row[rev])
        if not re.search(r"\bR[23]\b", cell, re.I) or re.search(r"\bR1\b", cell, re.I):
            bad.append(_row_label(table, row))
    if bad:
        return False, "rows whose Reversibility is R1 or not R2/R3: " + _names(bad)
    return True, f"{len(rows)} gate rows, all R2 or R3"


def _validation_table(doc):
    section = doc.section("validation gates")
    if section is None:
        return None, _missing_section("validation gates")
    table = section.table_with("id")
    if table is None:
        return None, (False, f"no table with an ID column in {section.name}")
    return table, None


def _validation_ids(doc):
    table, _ = _validation_table(doc)
    if table is None:
        return set()
    return {v for row in table.rows for v in V_ID.findall(plain(row[table.col("id")]))}


def check_r3_backed(doc):
    table, failure = _gates_table(doc)
    if failure:
        return failure
    rev, validated = table.col("reversibility"), table.col("validated")
    if rev is None or validated is None:
        return False, "gates table lacks a Reversibility or Validated by column"
    known = _validation_ids(doc)
    r3 = [row for row in _gate_rows(table) if re.search(r"\bR3\b", plain(row[rev]), re.I)]
    bad = []
    for row in r3:
        cell = plain(row[validated])
        if not (set(V_ID.findall(cell)) & known or BASIS.search(cell)):
            bad.append(_row_label(table, row))
    if bad:
        return False, "R3 rows citing neither a V-ID in section 6 nor a basis: " + _names(bad)
    if not r3:
        return True, "no R3 rows"
    return True, f"{len(r3)} R3 rows, each cites a section-6 V-ID or a basis"


def check_validation_complete(doc):
    table, failure = _validation_table(doc)
    if failure:
        return failure
    missing = table.missing(VALIDATION_COLUMNS)
    if missing:
        return False, "validation table lacks columns: " + ", ".join(missing)
    if not table.rows:
        return False, "validation table has no rows"
    empty = [f"{_row_label(table, row)} {label}" for row in table.rows
             for prefix, label in VALIDATION_COLUMNS if is_empty(row[table.col(prefix)])]
    if empty:
        return False, "empty cells: " + _names(empty)
    return True, f"{len(table.rows)} gates, all 7 fields filled"


def check_v0(doc):
    table, failure = _validation_table(doc)
    if failure:
        return failure
    ids = [plain(row[table.col("id")]) for row in table.rows]
    if any(re.match(r"V0(?!\d)", i) for i in ids):
        return True, "V0 row present"
    return False, "no V0 row (IDs: " + (_names(ids) or "none") + ")"


def check_no_dangling(doc):
    cited = sorted(set(V_ID.findall(doc.text)), key=_v_order)
    table, failure = _validation_table(doc)
    if failure:
        return False, failure[1] + (f"; cited: {_names(cited)}" if cited else "")
    dangling = [v for v in cited if v not in _validation_ids(doc)]
    if dangling:
        return False, "V-IDs cited without a section-6 row: " + _names(dangling)
    return True, f"all {len(cited)} cited V-IDs have a section-6 row"


def check_validation_labeled(doc):
    table, failure = _validation_table(doc)
    if failure:
        return failure
    threshold = table.col("acceptance")
    if threshold is None:
        return False, "validation table has no Acceptance threshold column"
    rows = [row for row in table.rows if not re.match(r"V0(?!\d)", plain(row[table.col("id")]))]
    bad = [_row_label(table, row) for row in rows
           if not (LABEL_ANY.search(plain(row[threshold]))
                   or SIGN_OFF.search(plain(row[threshold])))]
    if bad:
        return False, "thresholds without a label or sign-off: " + _names(bad)
    return True, f"{len(rows)} thresholds (V0 excepted), each labeled or a sign-off"


def _is_phase_zero(cell):
    text = plain(cell).lower()
    return bool(re.match(r"(?:phase\s*|p)?0(?!\d)", text) or re.search(r"\bphase\s*0(?!\d)", text)
                or "skeleton" in text)


def check_crosscutting(doc):
    section = doc.section("cross-cutting")
    if section is None:
        return _missing_section("cross-cutting")
    table = section.table_with("phase")
    if table is None:
        return False, f"no table with a Phase column in {section.name}"
    missing = table.missing(CROSSCUTTING_COLUMNS)
    if missing:
        return False, "cross-cutting table lacks columns: " + ", ".join(missing)
    phase = table.col("phase")
    if not any(_is_phase_zero(row[phase]) for row in table.rows):
        return False, "no row for phase 0"
    empty = [f"phase {_short(row[phase], 12)} {label}" for row in table.rows
             for prefix, label in CROSSCUTTING_COLUMNS if is_empty(row[table.col(prefix)])]
    if empty:
        return False, "empty cells: " + _names(empty)
    return True, f"{len(table.rows)} phases including phase 0, no empty cell"


def check_exceptions(doc):
    section = doc.section("exceptions")
    if section is None:
        return _missing_section("exceptions")
    table = next((t for t in section.tables()
                  if t.col("rule") is not None or t.col("resumes") is not None), None)
    if table is None:
        # Exactly "None" (any case, optional period), as the body or at the end of the heading.
        body = section.body()
        if NONE_ONLY.match(body) or (not body and re.search(r"\bnone\.?$", section.heading.norm)):
            return True, "None."
        return False, f"{section.name} is neither 'None.' nor an exceptions table"
    missing = table.missing(EXCEPTION_COLUMNS)
    if missing:
        return False, "exceptions table lacks columns: " + ", ".join(missing)
    if not table.rows:
        return False, "exceptions table has no rows"
    problems = [f"{_row_label(table, row)} empty {label}" for row in table.rows
                for prefix, label in EXCEPTION_COLUMNS if is_empty(row[table.col(prefix)])]
    resumes = table.col("resumes")
    problems += [f"{_row_label(table, row)} resumes 'later'" for row in table.rows
                 if plain(row[resumes]).lower().strip(" .!") == "later"]
    if problems:
        return False, "; ".join(problems[:6]) + (" …" if len(problems) > 6 else "")
    return True, f"{len(table.rows)} exceptions, every field filled"


def check_deferred(doc):
    section = doc.section("deferred")
    if section is None:
        return _missing_section("deferred")
    bullets = [line for line in section.body_lines() if BULLET.match(line)]
    if bullets:
        return True, f"{len(bullets)} deferred items"
    if NONE_BODY.match(section.body()):
        return True, "None."
    return False, f"{section.name} has no bullet and does not say None"


def check_ai_layer(doc, kind):
    section = doc.section("ai layer")
    if section is None:
        return _missing_section("ai layer")
    text = (section.heading.norm + " " + section.body()).lower()
    if kind == "AI":
        # The template names sublayer 1 "Prompt-injection / guardrail defense", so either word
        # counts (a fraud model has guardrails, not prompts).
        missing = [name for name, words in (("injection or guardrail", ("injection", "guardrail")),
                                            ("cost or budget", ("cost", "budget")),
                                            ("human or approval", ("human", "approval")))
                   if not any(w in text for w in words)]
        if missing:
            return False, "AI system; section 8 does not mention: " + ", ".join(missing)
        return True, ("AI system; section 8 covers injection or guardrails, cost/budget and "
                      "human approval")
    if "n/a" in text:
        return True, "non-AI system; section 8 says N/A"
    return False, "non-AI system; section 8 does not say N/A"


CHECKS = (
    ("sections.present", check_sections),
    ("self_score.absent", check_self_score),
    ("budgets.labeled", check_budgets_labeled),
    ("budgets.unknown_has_no_number", check_unknown_has_no_number),
    ("dependencies.typed", check_dependencies),
    ("gates.columns", check_gate_columns),
    ("gates.no_r1_rows", check_no_r1_rows),
    ("gates.r3_backed", check_r3_backed),
    ("validation.complete", check_validation_complete),
    ("validation.v0", check_v0),
    ("validation.no_dangling", check_no_dangling),
    ("validation.labeled", check_validation_labeled),
    ("crosscutting.complete", check_crosscutting),
    ("exceptions.complete", check_exceptions),
    ("deferred.present", check_deferred),
)


# --- entry points --------------------------------------------------------------------------

def lint_plan(text, kind=None):
    """Lint one plan. Returns schemas.LINT minus gen_id: 15 checks, or 16 when `kind` is given."""
    if kind not in (None, "AI", "non-AI"):
        raise ValueError(f"kind must be 'AI', 'non-AI' or None, got {kind!r}")
    doc = Document(text)
    results = []
    for check_id, check in CHECKS:
        ok, detail = check(doc)
        results.append({"id": check_id, "ok": ok, "detail": detail})
    if kind is not None:
        ok, detail = check_ai_layer(doc, kind)
        results.append({"id": "ai_layer.consistent", "ok": ok, "detail": detail})
    passed = sum(1 for r in results if r["ok"])
    return {"schema": "dfa-eval/lint@1", "template": TEMPLATE, "checks": results,
            "passed": passed, "total": len(results), "score": passed / len(results)}


def _lint_records(run_dir):
    """(path, record) for every ok generation, computed from its plan text."""
    run = records.RunDir(run_dir)
    manifest = run.manifest()
    kinds = {p["id"]: p.get("kind") for p in manifest.get("config", {}).get("prompts", [])}
    out = []
    for gen in run.generations():
        if gen.get("status") != "ok":
            continue
        gid = gen["gen_id"]
        source = (run.root / gen["output_file"]) if gen.get("output_file") else (
            run.generation_text(gid))
        result = lint_plan(records.read_text(source), kinds.get(gen["prompt_id"]))
        record = {"schema": result["schema"], "gen_id": gid}
        record.update((k, v) for k, v in result.items() if k != "schema")
        schema.check(record, schemas.LINT, f"lint record {gid}")
        out.append((run.lint_path(gid), record))
    # str order, not Path order: Path comparison is case-insensitive on Windows.
    return sorted(out, key=lambda item: item[1]["gen_id"])


def run_lint(run_dir):
    """Write lint/<gen_id>.json for every ok generation; return the records, sorted by gen_id."""
    out = _lint_records(run_dir)
    for path, record in out:
        records.write_json(path, record)
    return [record for _, record in out]


def check_lint(run_dir):
    """(ok, diff): recompute every lint record and compare with the committed files."""
    diffs = []
    for path, record in _lint_records(run_dir):
        expected = records.dumps(record)
        name = f"lint/{path.name}"
        if not path.exists():
            diffs.append(f"{name} is missing\n")
            continue
        current = path.read_bytes().decode("utf-8").replace("\r\n", "\n")
        if current != expected:
            diffs.append("".join(difflib.unified_diff(
                current.splitlines(True), expected.splitlines(True),
                fromfile=f"{name} (committed)", tofile=f"{name} (recomputed)")))
    return not diffs, "".join(diffs)
