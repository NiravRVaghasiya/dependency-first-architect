"""Blinding: the copies of each plan that judges see, and the sealed key back to the plan.

Every ok generation gets two blind copies, written as blind/<blind_id>.md:

  plain        the plan without any self-score section or "/20" total line (a plan grading
               itself must not anchor the judge), with surrounding whitespace trimmed to one
               final newline; for an imported plan (provider "imported", the examples/ format)
               also without a leading provenance header. A plan the harness generated keeps an
               opening blockquote: there it is the model's own text (a summary, assumptions).
               Used for the methodology-adherence rubric, exactly as in v1.0.0.
  neutralized  plain + `neutralize`: the skill's distinctive vocabulary replaced by plain words,
               so a judge scoring engineering quality cannot reward or punish the method's
               jargon. Applied to every condition alike, whoever wrote the plan.

A self-score section starts at a heading such as "## 8. Eval score", "## 8) Self-check against
the rubric", "## Step 8: Evaluation score", "## 8. Score (self-assessed): 19/20", or a bold line
alone such as "**8. Eval score**" (see is_self_score_heading). After stripping, any line that
still states a score out of 20 next to "score" or "total" is residue: the key's
`self_score_removed` is true only when a self-score was removed and no residue is left, and
run_blind warns about every copy with residue.

NEUTRAL_TERMS is the full replacement table (longest match first, whole words, case-insensitive,
first-letter case kept). It deliberately leaves bare R1/R2/R3 alone ("Cloudflare R2"), and the
word "skill". Extend it only with care: every entry changes what judges read.

Blind ids are sha256(salt:gen_id:variant)[:12] with salt = sha256("blind:<seed>:<run_id>")[:16].
They are deterministic, so the key can be re-derived and audited: blinding protects the judges,
who never see ids, file names or conditions, not the maintainer.
"""

import hashlib
import re

from . import records, schema, schemas
from .generate import HarnessError

HEADER_END = "\n\n---\n\n"
SCORE_LINE = re.compile(r"(?i)^\s*\**\s*(total|self-score|score)\b.*\b\d{1,2}\s*/\s*20\b")
HEADING = re.compile(r"^(#{1,6})(?:[ \t]|$)")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
# What may precede a section title: bold markers and a number ("8.", "8)", "8 —", "8:"),
# optionally "Step 8", "Section 8", "Part 8".
TITLE_PREFIX = re.compile(r"[\s*_]*(?:(?:(?:step|section|part)\s+)?\d+[a-z]?\s*(?:[.):]|[—–-])?)?"
                          r"[\s*_]*", re.I)
# Self-score titles as v1.0.0 matched them: "Eval score" or "Self-score", whatever follows.
V1_TITLE = re.compile(r"(?:Eval score|Self[- ]?score)\b", re.I)
# Further self-score names. Being newer, they count only when the title ends there or goes on
# with punctuation or "against ..." ("Self-check against the rubric"), never with a plain word
# ("Self-assessment questionnaire", "Evaluation score thresholds" are not self-scores).
TITLE = re.compile(r"(?:Eval(?:uation)?\s+score|Self[- ]?(?:score|scoring|check|assessment|"
                   r"evaluation|grade|grading))\s*(?:$|[:(\[—–-]|against\b|vs\b|versus\b)", re.I)
# A heading that states its own score out of 20 ("Score (self-assessed): 19/20", "Total: 18/20").
SCORE_TITLE = re.compile(r"(?:(?:final|overall|rubric|total|self[- ]?assessed)\s+)?"
                         r"(?:score|total)\b.*\b\d{1,2}\s*/\s*20\b", re.I)
BOLD_LINE = re.compile(r"^ {0,3}(\*\*|__)(?P<title>[^\s*_].*?)\1\s*:?\s*$")
PSEUDO_LEVEL = 7  # a bold pseudo-heading's section ends at the next heading of any level
OUT_OF_20 = re.compile(r"\b\d{1,2}\s*/\s*20\b")
SCORE_WORD = re.compile(r"\b(?:total|score)\b", re.I)

NEUTRAL_TERMS = (
    ("widest-blast-radius", "widest-impact"),
    ("tradeoff gates", "key decisions"),
    ("trade-off gates", "key decisions"),
    ("tradeoff gate", "key decision"),
    ("trade-off gate", "key decision"),
    ("flip conditions", "revisit triggers"),
    ("flip condition", "revisit trigger"),
    ("walking skeletons", "first end-to-end slices"),
    ("walking skeleton", "first end-to-end slice"),
    ("blast radius", "impact"),
    ("blast-radius", "impact"),
    ("deliberately deferred", "deferred"),
    ("validation gates", "validation checks"),
    ("validation gate", "validation check"),
    ("methodology exceptions", "exceptions"),
    ("methodology exception", "exception"),
    ("dependency map", "dependencies"),
    ("R1 defaults", "minor defaults"),
    ("/dependency-first-architect", "the plan"),
    ("dependency-first-architect", "the plan"),
    ("dependency-first architect", "the plan"),
    ("dependency-first", "the plan"),
    ("R1 easy", "easy to reverse"),
    ("R2 costly", "costly to reverse"),
    ("R3 hard", "hard to reverse"),
    ("as the template says", "as planned"),
    ("per the template", "as planned"),
    ("as the skill requires", "as planned"),
)
# Matched case-sensitively: "BUILD PLAN" is the skill's title convention, "build plan" is English.
CASE_SENSITIVE_TERMS = (("BUILD PLAN", "Plan"),)


def _key(term):
    return re.sub(r"\s+", " ", term).lower()


def _pattern():
    terms = [(t, False) for t, _ in NEUTRAL_TERMS] + [(t, True) for t, _ in CASE_SENSITIVE_TERMS]
    terms.sort(key=lambda item: (-len(item[0]), item[0]))
    parts = []
    for term, exact in terms:
        body = r"\s+".join(re.escape(word) for word in term.split(" "))
        parts.append(f"(?-i:{body})" if exact else body)
    return re.compile(r"(?<![\w-])(?:" + "|".join(parts) + r")(?![\w-])", re.I)


_TERMS = _pattern()
_LOOKUP = {_key(t): r for t, r in NEUTRAL_TERMS + CASE_SENSITIVE_TERMS}


def _replacement(match):
    found = match.group(0)
    new = _LOOKUP[_key(found)]
    # Keep a word's first-letter case ("Walking skeleton" -> "First end-to-end slice"); ids like
    # R1 and the slash command have no case to keep.
    if len(found) > 1 and found[0].isalpha() and found[1].isalpha():
        new = (new[0].upper() if found[0].isupper() else new[0].lower()) + new[1:]
    return new


def neutralize(text):
    """(text, n_replacements): NEUTRAL_TERMS applied once, left to right, longest match first."""
    return _TERMS.subn(_replacement, text)


def strip_provenance(text):
    """Drop a leading `> ...` provenance block ended by a `---` line (the examples/ format)."""
    if not text.startswith("> "):
        return text
    head, sep, rest = text.partition(HEADER_END)
    if sep and all(line.startswith(">") for line in head.split("\n")):
        return rest
    return text


def _title_rest(title):
    """A section title without its bold markers and numbering ("8.", "Step 8:")."""
    return title[TITLE_PREFIX.match(title).end():]


def is_self_score_heading(line):
    """True if markdown heading `line` starts a self-score section: its title (after any bold
    markers and numbering) is a self-score name, or states a score or total out of 20."""
    heading = HEADING.match(line)
    if not heading:
        return False
    rest = _title_rest(line[heading.end():].strip().rstrip("#").strip())
    return bool(V1_TITLE.match(rest) or TITLE.match(rest) or SCORE_TITLE.match(rest))


def is_self_score_pseudo_heading(line):
    """True if `line` is a bold line alone that names a self-score ("**8. Eval score**"), the
    way a plan without markdown headings titles a section."""
    bold = BOLD_LINE.match(line)
    return bool(bold and TITLE.match(_title_rest(bold.group("title"))))


def self_score_residue(text):
    """The lines of `text` that still state a score out of 20 next to "score" or "total"."""
    return [line for line in text.split("\n") if OUT_OF_20.search(line) and SCORE_WORD.search(line)]


def strip_self_score(text):
    """(text, removed): remove self-score sections and standalone "/20" total lines.

    A self-score section runs from a heading for which is_self_score_heading holds to the next
    heading of the same or a higher level, or to the end; from a bold pseudo-heading
    (is_self_score_pseudo_heading) to the next heading of any level, or to the end. Headings
    inside fenced code blocks are not headings.
    """
    out, removed = [], False
    skip = None  # level of the section being removed
    fence = None  # (marker char, length) of the open code fence
    for line in text.split("\n"):
        level = None
        marker = FENCE.match(line)
        if fence is None:
            if marker:
                fence = (marker.group(1)[0], len(marker.group(1)))
            else:
                heading = HEADING.match(line)
                level = len(heading.group(1)) if heading else None
        elif (marker and marker.group(1)[0] == fence[0] and len(marker.group(1)) >= fence[1]
              and not line.strip().strip(fence[0])):
            fence = None
            marker = True
        if skip is not None:
            if level is not None and level <= skip:
                skip = None
            else:
                continue
        if level is not None and is_self_score_heading(line):
            skip, removed = level, True
            continue
        if fence is None and not marker and is_self_score_pseudo_heading(line):
            skip, removed = PSEUDO_LEVEL, True
            continue
        if fence is None and not marker and SCORE_LINE.match(line):
            removed = True
            continue
        out.append(line)
    result = "\n".join(out)
    if skip is not None:  # the removed section ran to the end of the plan
        result = result.rstrip() + "\n" if result.strip() else ""
    return result, removed


def blind_salt(seed, run_id):
    return hashlib.sha256(f"blind:{seed}:{run_id}".encode("utf-8")).hexdigest()[:16]


def blind_id(salt, gen_id, variant):
    return hashlib.sha256(f"{salt}:{gen_id}:{variant}".encode("utf-8")).hexdigest()[:12]


def blind_variants(text, strip_score=True, strip_header=False):
    """[(variant, text, replacements, self_score_removed)] for one plan, plain then neutralized.

    `strip_header` drops a leading provenance blockquote (strip_provenance); only imported plans
    carry one. self_score_removed: a self-score was removed and no residue is left in the copy.
    """
    plain = strip_provenance(text) if strip_header else text
    removed = False
    if strip_score:
        plain, removed = strip_self_score(plain)
    plain = plain.strip() + "\n"
    neutral, count = neutralize(plain)
    return [("plain", plain, 0, removed and not self_score_residue(plain)),
            ("neutralized", neutral, count, removed and not self_score_residue(neutral))]


def run_blind(run_dir, log=None):
    """Write both blind copies of every ok generation and blind/key.json; return the key.

    Deterministic and idempotent: unchanged files are not rewritten, and blind copies of
    generations that are no longer ok are removed. Warns about copies that still state a score
    out of 20, and about changed copies whose earlier text was already judged.
    """
    log = log or (lambda message: None)
    run = records.RunDir(run_dir)
    manifest = run.manifest()
    config = manifest["config"]
    blinding = config.get("blinding") or {}
    strip_score = blinding.get("strip_self_score", True)
    salt = blind_salt(manifest["seed"], manifest["run_id"])
    entries, residue, changed = [], [], {}
    for record in run.generations():
        if record["status"] != "ok":
            continue
        path = run.root / record["output_file"]
        data = path.read_bytes()
        if records.sha256_bytes(data) != record["output_sha256"]:
            raise HarnessError(f"{record['output_file']} does not match the sha256 recorded in "
                             f"generations/{record['gen_id']}.json; refusing to blind an edited "
                             "plan")
        variants = blind_variants(data.decode("utf-8"), strip_score,
                                  strip_header=record.get("provider") == "imported")
        for variant, body, count, removed in variants:
            bid = blind_id(salt, record["gen_id"], variant)
            sha = records.sha256_text(body)
            if _write_if_changed(run.blind_text(bid), body):
                changed[bid] = sha
            if strip_score and self_score_residue(body):
                residue.append(f"{record['gen_id']} ({variant})")
            entries.append({"blind_id": bid, "gen_id": record["gen_id"], "variant": variant,
                            "sha256": sha, "replacements": count, "self_score_removed": removed})
    entries.sort(key=lambda e: e["blind_id"])
    key = {"schema": "dfa-eval/blind-key@1", "salt": salt,
           "neutralize_terms_for": list(blinding.get("neutralize_terms_for", [])),
           "entries": entries}
    schema.check(key, schemas.BLIND_KEY, "blind key")
    run.blind_key_path.parent.mkdir(parents=True, exist_ok=True)
    _write_if_changed(run.blind_key_path, records.dumps(key))
    keep = {f"{e['blind_id']}.md" for e in entries}
    for stale in sorted(run.blind_key_path.parent.glob("*.md")):
        if stale.name not in keep:
            stale.unlink()
    log(f"blind: {len(entries) // 2} plans, {len(entries)} blind copies, "
        f"{sum(e['self_score_removed'] for e in entries) // 2} self-scores removed")
    if residue:
        log(f"WARNING: {len(residue)} blind copies still state a score out of 20 after "
            f"self-score stripping (self_score_removed is false for them); read them before "
            f"judging: {', '.join(residue)}")
    _warn_about_judged_changes(run, changed, log)
    return key


def _warn_about_judged_changes(run, changed, log):
    """Say what happens to judgments and probes of blind copies whose text just changed."""
    if not changed:
        return
    redo, unknown = 0, []
    for rec in run.judgments() + run.probes():
        if rec.get("blind_id") not in changed:
            continue
        if "blind_sha256" not in rec:
            unknown.append(rec.get("judgment_id") or rec.get("probe_id"))
        elif rec["blind_sha256"] != changed[rec["blind_id"]]:
            redo += 1
    if redo:
        log(f"blind: {redo} judgment(s)/probe(s) scored an earlier text of a changed blind copy; "
            "`judge` and `probe` will redo them")
    if unknown:
        shown = sorted(unknown)
        listed = ", ".join(shown[:10]) + (f", and {len(shown) - 10} more" if len(shown) > 10 else "")
        log(f"WARNING: {len(unknown)} judgment(s)/probe(s) of changed blind copies were recorded "
            "without blind_sha256, so `judge` cannot tell that they scored the earlier text; "
            f"start a new run, or move them out of this one to have them redone: {listed}")


def _write_if_changed(path, text):
    """Write `text` unless `path` already holds it; True if an existing file's text changed."""
    data = text.replace("\r\n", "\n").encode("utf-8")
    existed = path.is_file()
    if existed and path.read_bytes() == data:
        return False
    records.write_text(path, text)
    return existed
