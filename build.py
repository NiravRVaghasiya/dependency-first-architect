#!/usr/bin/env python3
"""Regenerate the Cursor and Codex adapters, and SHA256SUMS, from the canonical SKILL.md.

Single source of truth: SKILL.md. The adapters are DERIVED — never hand-edit them. They carry
SKILL.md without its <!-- claude-code-only --> spans, which tell Claude Code to load reference/
files that the adapters do not ship.
Run:  python build.py                       regenerate the adapters and SHA256SUMS
      python build.py --check               write nothing; exit 1 if an adapter or SHA256SUMS
                                            is stale, or CHANGELOG.md has no entry for VERSION
      python build.py --release-tag vX.Y.Z  everything --check does; also exit 1 unless the tag
                                            is "v" + VERSION and CHANGELOG.md dates that version
Provenance: each adapter's banner names VERSION and the SKILL.md hash it was built from, so an
installed copy can be traced to a release; SHA256SUMS (sha256sum format) covers VERSION,
SKILL.md, reference/*.md and every adapter, so `sha256sum -c SHA256SUMS` verifies a checkout.
No third-party dependencies; standard library only.
"""

import argparse
import datetime
import difflib
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SKILL = ROOT / "SKILL.md"
VERSION = ROOT / "VERSION"
CHANGELOG = ROOT / "CHANGELOG.md"
SUMS = ROOT / "SHA256SUMS"
REFERENCE = ROOT / "reference"
ADAPTERS = ROOT / "adapters"
CURSOR = ADAPTERS / "cursor-dependency-first-architect.mdc"
CODEX = ADAPTERS / "AGENTS.md"
# Lazy Codex install: a short AGENTS.md block that tells Codex to read the whole skill from a file
# only when a request matches, instead of loading all of it into every session.
CODEX_LAZY = ADAPTERS / "codex" / "dependency-first-architect.md"
CODEX_STUB = ADAPTERS / "codex" / "AGENTS-snippet.md"
CODEX_LAZY_INSTALLED = ".codex/dependency-first-architect.md"  # where the README installs it
CODEX_LAZY_REFERENCES = ("reference/plan-template.md", "reference/validation.md",
                         "reference/ai-systems.md")
FULL_ADAPTERS = (CURSOR, CODEX, CODEX_LAZY)  # carry the whole method, so the sentinels apply
REPO_URL = "https://github.com/NiravRVaghasiya/dependency-first-architect"

# Section names from SKILL.md that must survive into both adapters; a missing one means the
# build dropped or mangled part of the method.
SENTINELS = ("Tradeoff gates", "Validation gates", "Methodology exceptions")
SENTINEL = SENTINELS[0]  # the v1 single sentinel, kept for compatibility

# The adapters get copied into other people's repos (Codex appends to their AGENTS.md), so the
# banner must make sense there too, and BEGIN/END let an installed copy be replaced cleanly.
# Installed copies are found by these two lines: never change them.
BEGIN_LINE = "BEGIN dependency-first-architect — GENERATED FILE, DO NOT HAND-EDIT."
END_MARKER = "<!-- END dependency-first-architect -->"

# SKILL.md wraps what only Claude Code can follow (loading reference/ files, which the adapters do
# not ship) in these markers; the adapters get everything except those spans.
CC_OPEN = "<!-- claude-code-only -->"
CC_CLOSE = "<!-- /claude-code-only -->"
CC_SPAN = re.compile(re.escape(CC_OPEN) + ".*?" + re.escape(CC_CLOSE), re.DOTALL)

# Semantic Versioning 2.0.0 (semver.org), with ASCII digits: `\d` would accept any Unicode digit.
_NUM = r"(?:0|[1-9][0-9]*)"
_PRE = r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
SEMVER = re.compile(
    rf"{_NUM}\.{_NUM}\.{_NUM}(?:-{_PRE}(?:\.{_PRE})*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
CHANGELOG_HEADING = re.compile(r"^##[ \t]+\[([^\]\n]*)\](.*)$", re.MULTILINE)
ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
FRONTMATTER_KEY = re.compile(r"([A-Za-z_][A-Za-z0-9_-]*)[ \t]*:(?:[ \t]+(.*))?")
BLOCK_SCALAR = re.compile(r"[>|][0-9+-]*")
# One-line values that YAML would not read back as the same plain string.
YAML_NEEDS_QUOTES = re.compile(r"^[\s!&*?:,\[\]{}#|>@`'\"%-]|:\s|\s#|:$|\s$|[\x00-\x1f\x7f]")


def read_lf(path):
    """A file's bytes with CRLF as LF: what git stores and checks out here (.gitattributes sets
    eol=lf), so a CRLF working copy hashes and compares the same as a fresh checkout."""
    return Path(path).read_bytes().replace(b"\r\n", b"\n")


def parse_version(text):
    """Return the version in VERSION's text, which must be one line holding a semantic version."""
    value = text.replace("\r\n", "\n")
    if value.endswith("\n"):
        value = value[:-1]
    if not SEMVER.fullmatch(value):
        raise ValueError("VERSION must be one line holding a semantic version such as 2.0.0, "
                         f"not {value!r}")
    return value


def read_version():
    if not VERSION.is_file():
        raise ValueError("VERSION is missing: it holds the skill's semantic version, e.g. 2.0.0")
    return parse_version(read_lf(VERSION).decode("utf-8", errors="replace"))


def parse_skill(text):
    """Split SKILL.md into (name, description, body_markdown).

    Minimal frontmatter parse, enough for a skill header: top-level `key: value` lines whose value
    may continue on indented lines or be a `>-` / `|` block. Values are joined into one line,
    because the Cursor frontmatter and the Codex header each take a single line.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    if lines[0].rstrip() != "---":
        raise ValueError("SKILL.md must start with YAML frontmatter delimited by ---")
    # The first `---` line closes it; `---` inside a value does not.
    close = next((i for i, line in enumerate(lines) if i and line.rstrip() == "---"), None)
    if close is None:
        raise ValueError("SKILL.md frontmatter is not closed by a --- line")

    fields = {}
    key = None
    for line in lines[1:close]:
        if not line.strip():
            continue  # blank lines do not end a block
        if line[0] in " \t":
            if key:
                fields[key].append(line.strip())
            continue
        match = FRONTMATTER_KEY.fullmatch(line.rstrip())
        key = match.group(1) if match else None  # a comment or other line ends the value
        if key:
            value = (match.group(2) or "").strip()
            fields[key] = [value] if value and not BLOCK_SCALAR.fullmatch(value) else []

    name = " ".join(fields.get("name", []))
    description = " ".join(fields.get("description", []))
    if not name:
        raise ValueError("SKILL.md frontmatter missing `name`")
    if not description:
        raise ValueError("SKILL.md frontmatter missing `description`")
    body = "\n".join(lines[close + 1:]).lstrip("\n")
    return name, description, body


def strip_claude_code_only(body):
    """`body` without its claude-code-only spans, markers included.

    A span inside a line goes with the whitespace before it. A span whose markers stand on their
    own lines goes with those lines, and with the blank line after it if the text before it ends
    in one (or is empty), so removing a block never leaves a doubled or leading blank line.
    """
    tokens = re.findall(f"{re.escape(CC_OPEN)}|{re.escape(CC_CLOSE)}", body)
    if tokens != [CC_OPEN, CC_CLOSE] * (len(tokens) // 2):
        raise ValueError(f"SKILL.md has unbalanced or nested {CC_OPEN} markers")
    if "\x00" in body:
        raise ValueError("SKILL.md contains a NUL character")
    out = []
    skip_blank = False
    for line in CC_SPAN.sub("\x00", body).split("\n"):  # NUL marks where a span was
        if line.strip() == "\x00":
            skip_blank = not out or not out[-1].strip()
            continue
        if skip_blank and not line.strip():
            skip_blank = False
            continue
        skip_blank = False
        out.append(re.sub(r"[ \t]*\x00", "", line))
    return "\n".join(out)


def yaml_value(text):
    """`text` as a one-line YAML value: plain when YAML reads it back unchanged, else quoted."""
    if text and not YAML_NEEDS_QUOTES.search(text):
        return text
    return json.dumps(text, ensure_ascii=False)  # a JSON string is a YAML double-quoted scalar


NOT_BUNDLED = "Reference files (reference/*.md) are not bundled in this adapter; see the repository."


def banner_lines(version, skill_sha256, contents=NOT_BUNDLED):
    return [
        BEGIN_LINE,
        f"Version {version} · built from SKILL.md sha256 {skill_sha256[:12]}",
        contents,
        f"Generated by build.py from SKILL.md in {REPO_URL}",
        "To update, re-run the install command from that README (in AGENTS.md: replace BEGIN to END).",
    ]


def build_cursor(name, description, body, lines):
    """Cursor .mdc: YAML frontmatter with alwaysApply:false + an HTML-comment banner."""
    fm = [
        "---",
        f"description: {yaml_value(description)}",
        "globs:",
        "alwaysApply: false",
        "---",
    ]
    head = "\n".join(fm)
    note = "<!--\n" + "\n".join(f" {line}" if line else "" for line in lines) + "\n-->"
    return f"{head}\n\n{note}\n\n{body.rstrip()}\n\n{END_MARKER}\n"


def build_codex(name, description, body, lines):
    """Codex AGENTS.md: plain markdown, banner as an HTML comment, trigger stated inline."""
    note = "<!--\n" + "\n".join(lines) + "\n-->"
    header = f"# {name}\n\n**When to use:** {description}"
    return f"{note}\n\n{header}\n\n{body.rstrip()}\n\n{END_MARKER}\n"


def build_codex_lazy_file(name, description, body, lines, references):
    """The whole skill as one file a Codex agent reads on demand: the SKILL.md body (without its
    claude-code-only spans) plus, as appendices, the reference files Claude Code loads lazily."""
    note = "<!--\n" + "\n".join(lines) + "\n-->"
    header = f"# {name}\n\n**When to use:** {description}"
    parts = [note, header, body.rstrip(),
             "## Appendices\n\nThe files below are the reference material the Claude Code version "
             "of this skill loads on demand. Appendix A is the exact output format; the others are "
             "worked examples and AI-layer detail. They are already here: do not look for them "
             "elsewhere. Their headings are nested two levels down to fit under this one; in a "
             "plan, Appendix A's numbered sections are top-level `##` sections. Where an "
             "appendix disagrees with the method above, the method wins; `SKILL.md` in an "
             "appendix means the method above."]
    for letter, (rel, text) in zip("ABCDEFGH", references):
        parts.append(f"## Appendix {letter}: `{rel}`\n\n{demote_headings(text).strip()}")
    return "\n\n".join(parts) + f"\n\n{END_MARKER}\n"


def demote_headings(text):
    """Push every markdown heading two levels down (# -> ###), so an appendix nests under its
    `## Appendix` heading; headings inside fenced code blocks are left alone."""
    out, fenced = [], False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        out.append("##" + line if not fenced and re.match(r"#{1,4} ", line) else line)
    return "\n".join(out)


def build_codex_stub(name, description, lines):
    """The few always-loaded lines for AGENTS.md that point Codex at the lazily read file."""
    note = "<!--\n" + "\n".join(lines) + "\n-->"
    return (f"{note}\n\n# {name}\n\n**When to use:** {description}\n\n"
            f"When a request matches, first read `{CODEX_LAZY_INSTALLED}` in full, then follow it "
            "exactly. It holds the whole skill, including its output format.\n\n"
            f"{END_MARKER}\n")


def render(version):
    """Generate every adapter in memory from SKILL.md, reference/ and VERSION: {path: content}."""
    skill = read_lf(SKILL)
    try:
        text = skill.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("SKILL.md is not valid UTF-8") from None
    name, description, body = parse_skill(text)
    body = strip_claude_code_only(body)
    digest = hashlib.sha256(skill).hexdigest()
    lines = banner_lines(version, digest)
    lazy_lines = banner_lines(version, digest, "The reference files the Claude Code version loads "
                                               "on demand are appended as appendices.")
    stub_lines = banner_lines(version, digest, f"The skill itself is in {CODEX_LAZY_INSTALLED}, "
                                               "which Codex reads only when a request matches.")
    references = []
    for rel in CODEX_LAZY_REFERENCES:
        try:
            references.append((rel, read_lf(ROOT / rel).decode("utf-8")))
        except FileNotFoundError:
            raise ValueError(f"{rel} is missing; the lazy Codex adapter appends it") from None
    return {
        CURSOR: build_cursor(name, description, body, lines),
        CODEX: build_codex(name, description, body, lines),
        CODEX_LAZY: build_codex_lazy_file(name, description, body, lazy_lines, references),
        CODEX_STUB: build_codex_stub(name, description, stub_lines),
    }


def checksummed(outputs):
    """{POSIX path: bytes} for every file SHA256SUMS covers, with the adapters as generated."""
    files = {"VERSION": read_lf(VERSION), "SKILL.md": read_lf(SKILL)}
    if REFERENCE.is_dir():
        for path in REFERENCE.iterdir():
            # The shell's reference/*.md on every OS: exact suffix, no dotfiles.
            if path.is_file() and path.suffix == ".md" and not path.name.startswith("."):
                files[f"reference/{path.name}"] = read_lf(path)
    for path, text in outputs.items():
        files[path.relative_to(ROOT).as_posix()] = text.encode("utf-8")
    return files


def render_sums(files):
    """sha256sum's own format: `<sha256>  <path>` (two spaces), POSIX paths, sorted by path."""
    lines = []
    for rel in sorted(files):
        if any(ch in rel for ch in "\\\r\n"):
            raise ValueError(f"cannot list {rel!r} in SHA256SUMS: sha256sum would escape it")
        lines.append(f"{hashlib.sha256(files[rel]).hexdigest()}  {rel}\n")
    return "".join(lines)


def parse_sums(text):
    """({path: recorded sha256}, [descriptions of malformed lines]) from SHA256SUMS text."""
    entries, malformed = {}, []
    for number, line in enumerate(text.splitlines(), 1):
        digest, sep, rel = line.partition("  ")
        if sep and rel and re.fullmatch(r"[0-9a-f]{64}", digest):
            entries[rel] = digest
        else:
            malformed.append(f"line {number} ({line!r})")
    return entries, malformed


def find_stale_sums(expected):
    """Compare SHA256SUMS on disk with `expected`; print what is stale; return True if stale."""
    if not SUMS.is_file():
        print("STALE: SHA256SUMS is missing")
        return True
    actual = read_lf(SUMS).decode("utf-8", errors="replace")
    if actual == expected:
        return False
    want, _ = parse_sums(expected)
    have, malformed = parse_sums(actual)
    for line in malformed:
        print(f"STALE: SHA256SUMS {line} is not `<sha256>  <path>`")
    for rel in sorted(set(want) | set(have)):
        if rel not in have:
            print(f"STALE: SHA256SUMS has no entry for {rel}")
        elif rel not in want:
            print(f"STALE: SHA256SUMS lists {rel}, which is not a file build.py checksums")
        elif have[rel] != want[rel]:
            print(f"STALE: SHA256SUMS entry for {rel} is out of date")
    if not malformed and have == want:
        print("STALE: SHA256SUMS has the right hashes but not the generated layout "
              "(order, spacing or line breaks)")
    return True


def is_date(text):
    """True for a real calendar date written YYYY-MM-DD."""
    if not ISO_DATE.fullmatch(text):
        return False
    try:
        datetime.date.fromisoformat(text)
    except ValueError:
        return False
    return True


def changelog_problems(text, version, release_tag=None):
    """What is wrong with CHANGELOG.md's `text` (None: the file is missing) for VERSION, and for
    releasing it as `release_tag` when one is given. Empty list: nothing."""
    problems = []
    labels = []
    if text is None:
        problems.append(f"CHANGELOG.md is missing; it needs a `## [{version}]` entry for VERSION")
    else:
        # `## [2.0.0] — 2026-10-02` or `## [2.0.0] — Unreleased`; any dash as the separator.
        labels = [label.strip().lstrip("—–-").strip()
                  for found, label in CHANGELOG_HEADING.findall(text.replace("\r\n", "\n"))
                  if found.strip() == version]
        if not labels:
            problems.append(f"CHANGELOG.md has no `## [{version}]` heading for VERSION {version}; "
                            "add an entry that says what changed")
        elif len(labels) > 1:
            problems.append(f"CHANGELOG.md has {len(labels)} `## [{version}]` headings; keep one")
    if release_tag is not None:
        if release_tag != f"v{version}":
            problems.append(f"release tag {release_tag!r} does not match VERSION {version} "
                            f"(expected 'v{version}')")
        if len(labels) == 1 and not is_date(labels[0]):
            problems.append(f"the CHANGELOG.md entry for {version} has no release date "
                            f"(found {labels[0]!r}); write `## [{version}] — YYYY-MM-DD` "
                            "before tagging")
    return problems


def read_changelog():
    return read_lf(CHANGELOG).decode("utf-8", errors="replace") if CHANGELOG.is_file() else None


def find_drift(outputs):
    """Diff each generated adapter against the file on disk; return the paths that differ."""
    drifted = []
    for path, expected in outputs.items():
        rel = path.relative_to(ROOT).as_posix()
        if not path.is_file():
            print(f"DRIFT: {rel} is missing")
            drifted.append(path)
            continue
        # CRLF is read as LF, so a Windows checkout of identical content is not drift; any
        # real content change is.
        actual = read_lf(path).decode("utf-8", errors="replace")
        if actual != expected:
            sys.stdout.writelines(difflib.unified_diff(
                actual.splitlines(keepends=True),
                expected.splitlines(keepends=True),
                fromfile=f"{rel} (committed)",
                tofile=f"{rel} (generated from SKILL.md)",
            ))
            drifted.append(path)
    return drifted


def print_sentinels_ok():
    for sentinel in SENTINELS:
        print(f"Sentinel '{sentinel}' present in SKILL.md and every full adapter: OK")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Regenerate the adapters and SHA256SUMS from SKILL.md.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="write nothing; exit 1 if either adapter or SHA256SUMS differs from what SKILL.md "
             "generates, or CHANGELOG.md has no entry for VERSION",
    )
    parser.add_argument(
        "--release-tag",
        metavar="TAG",
        help="write nothing; everything --check does, and exit 1 unless TAG is 'v' + VERSION and "
             "CHANGELOG.md gives that version a release date (CI runs this on tag pushes)",
    )
    args = parser.parse_args(argv)
    check = args.check or args.release_tag is not None

    # Diffs contain non-ASCII (—, →); emit UTF-8 rather than e.g. cp1252 on Windows pipes.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    try:
        version = read_version()
        outputs = render(version)
        sums = render_sums(checksummed(outputs))
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # Sanity: the shared sentinels from SKILL.md must survive into every full adapter.
    missing = [(s, path) for s in SENTINELS for path, out in outputs.items()
               if path in FULL_ADAPTERS and s not in out]
    for sentinel, path in missing:
        print(f"ERROR: sentinel '{sentinel}' missing from {path.name}", file=sys.stderr)
    if missing:
        return 1

    if check:
        drifted = find_drift(outputs)
        stale_sums = find_stale_sums(sums)
        problems = changelog_problems(read_changelog(), version, args.release_tag)
        if drifted:
            print(
                f"\nERROR: {len(drifted)} adapter(s) out of sync with SKILL.md. "
                "Run `python build.py` and commit the result.",
                file=sys.stderr,
            )
        if stale_sums:
            print("ERROR: SHA256SUMS is out of sync with the files it lists. "
                  "Run `python build.py` and commit the result.", file=sys.stderr)
        for problem in problems:
            print(f"ERROR: {problem}", file=sys.stderr)
        if drifted or stale_sums or problems:
            return 1
        for path in outputs:
            print(f"In sync: {path.relative_to(ROOT).as_posix()}")
        print("In sync: SHA256SUMS")
        print(f"CHANGELOG.md has an entry for {version}: OK")
        if args.release_tag is not None:
            print(f"Release tag {args.release_tag} matches VERSION and a dated CHANGELOG entry: OK")
        print_sentinels_ok()
        return 0

    for path, out in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write bytes so the output is LF on every OS (text mode emits CRLF on Windows).
        path.write_bytes(out.encode("utf-8"))
        print(f"Generated {path.relative_to(ROOT).as_posix()}")
    SUMS.write_bytes(sums.encode("utf-8"))
    print(f"Generated {SUMS.name}")
    for problem in changelog_problems(read_changelog(), version):
        print(f"WARNING: {problem} (`--check` fails until then)", file=sys.stderr)
    print_sentinels_ok()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
