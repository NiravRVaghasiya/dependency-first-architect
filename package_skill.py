#!/usr/bin/env python3
"""Build the installable skill package, and the release assets for each agent, into dist/.

Maintainer tool: it is not part of the package it builds. Standard library only; it runs no
shell, makes no network call and executes no generated code.
Run:  python package_skill.py             build into dist/, verify it, list what it holds
      python package_skill.py --out DIR   build into DIR instead (it must be new, empty, or an
                                          earlier output of this script)
Output:
  dependency-first-architect/   the skill package, the folder an agent installs: exactly the
                                files in PACKAGE_FILES, byte for byte as committed (LF), with
                                SKILL.md at its root
  release/                      what a release publishes: the package as a zip (that folder at
                                the zip's root), the Cursor and Codex adapters build.py generates
                                (separate assets, never in the package), and SHA256SUMS for every
                                asset and every file in the package
Everything is built and checked in memory before the output directory is touched, and checked
again on disk afterwards; any problem exits 1 and says which file. Unchanged sources rebuild to
byte-identical output on every OS. Nothing is ever deleted except the output directory itself,
and only when it is empty or carries the marker an earlier build left in it.
"""

import argparse
import hashlib
import io
import os
import posixpath
import re
import shutil
import stat
import sys
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath

sys.dont_write_bytecode = True  # a build writes dist/ and nothing else, not even __pycache__/
import build  # noqa: E402

ROOT = Path(__file__).resolve().parent
PACKAGE_NAME = "dependency-first-architect"
# The package, as an explicit allowlist: what an agent needs to plan, and nothing else. SKILL.md
# must name exactly the reference/ files listed here. reference/evals.md stays out: it is
# evaluation material for maintainers, and SKILL.md never loads it.
PACKAGE_FILES = (
    "SKILL.md",
    "LICENSE",  # MIT: copies must carry the notice
    "reference/plan-template.md",
    "reference/validation.md",
    "reference/ai-systems.md",
    "reference/layer-map.md",
)
MAX_FILES = 49  # the submission limit is fewer than 50 files, nested and hidden ones included
DEFAULT_OUT = ROOT / "dist"
RELEASE = "release"
# An earlier build left this file, so a rebuild may delete the directory holding it.
MARKER = ".package_skill-output"
MARKER_TEXT = b"Made by package_skill.py. The next build deletes this directory and rebuilds it.\n"
# Files an operating system drops into a folder it shows; a rebuild deletes them with the rest.
OS_METADATA = (".DS_Store", "Thumbs.db", "desktop.ini")
# The adapters as release assets, under flat names (a release has no folders).
ADAPTER_ASSETS = {
    build.CURSOR: "cursor-dependency-first-architect.mdc",
    build.CODEX: "codex-AGENTS.md",
    build.CODEX_LAZY: "codex-lazy-dependency-first-architect.md",
    build.CODEX_STUB: "codex-lazy-AGENTS-snippet.md",
}
# The adapters' banner names the repository it was generated from; no other URL may ship.
ADAPTER_URLS = (build.REPO_URL,)
ZIP_DATE = (1980, 1, 1, 0, 0, 0)  # the earliest a zip can hold: the bytes never record a build time

FRONTMATTER_KEYS = ("name", "description", "tags")
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")  # lowercase letters, digits, single inner hyphens
MAX_NAME = 64
MAX_DESCRIPTION = 1024
MAX_TAG = 32
MAX_TAGS = 10
RESERVED_WORDS = ("anthropic", "claude")  # some skill hosts reject a name holding these

# Headings that carry the method (SKILL.md is canonical): a package without one of them is not
# this skill, whatever else is right with it.
METHOD_HEADINGS = (
    "## Core principles",
    "## Dependencies are not only code",
    "## Validation gates",
    "## Brownfield and migrations",
    "## Methodology exceptions",
    "## Procedure",
    "### Step 1 — Classification and constraints",
    "### Step 2 — Dependency map",
    "### Step 3 — Tradeoff gates",
    "### Step 4 — Walking skeleton (Phase 0)",
    "### Step 5 — Phases",
    "### Step 6 — Validation gates",
    "### Step 7 — Cross-cutting concerns",
    "### Step 8 — AI layer (AI/agentic systems only)",
    "### Step 9 — Exceptions and deferred work",
    "### Step 10 — Self-check, then emit",
)

REFERENCE_PATH = re.compile(r"\breference/[A-Za-z0-9_./-]{1,200}\.[A-Za-z0-9]+\b")
# Link destinations, whatever the link text holds: inline links (`](target)`), link definitions
# (also inside block quotes and lists), autolinks (`<scheme:...>`) and HTML href/src. Every scan
# is bounded or anchored, so no rule backtracks badly on a long line.
MARKDOWN_LINK = re.compile(r"\]\(\s*<?([^)\s>]+)")
LINK_DEFINITION = re.compile(r"^[ \t>*+0-9.)-]{0,64}\[(?:[^\]\\\n]|\\.){1,999}\]:\s*<?([^\s>]+)",
                             re.MULTILINE)
AUTOLINK = re.compile(r"<([A-Za-z][A-Za-z0-9+.-]{1,31}:[^\s<>]*|[^\s<>@]{1,64}@[^\s<>@]{1,255})>")
HTML_LINK = re.compile(r"<[a-z][^>]{0,500}?\s(?:href|src)\s*=\s*[\"']?([^\"'\s>]+)",
                       re.IGNORECASE)
# A fence: an opener of three or more backticks (none after them on its line) or tildes, and
# a closer of the same character, at least as long, with nothing else on its line.
CODE_FENCE = re.compile(r"^ {0,3}(?:(`{3,})(?!`)(?![^\n]*`)|(~{3,})(?!~)).*?"
                        r"^ {0,3}(?(1)\1`*|\2~*)[ \t]*$", re.MULTILINE | re.DOTALL)
SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
URL = re.compile(r"[a-z][a-z0-9+.-]{0,31}://[^\s)>\]`'\"]*|\bwww\.[a-z0-9-]+\.[a-z]{2,}\b|"
                 r"\b(?:mailto|vbscript):|\bfile:/|\bdata:[a-z]+/", re.IGNORECASE)
# The only HTML comments a shipped file may hold, hidden in rendered views but read by agents:
# the markers build.py uses to leave Claude Code-only text out of the adapters. An unclosed
# comment runs to the end of the file, so it is matched to the end; <!x> and <?x> are comments
# to an HTML parser too.
ALLOWED_COMMENTS = (build.CC_OPEN, build.CC_CLOSE)
HTML_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)|<![A-Za-z\[]|<\?", re.DOTALL)
# Every character a shipped file may hold: printable ASCII, tab, line feed, and the typography
# the method uses (section sign, plus-minus, middle dot, multiplication sign, en and em dashes,
# ellipsis, right arrow, not-equal, greater-or-equal). Anything else, from a no-break space to
# the invisible Unicode tag characters that can smuggle instructions past a reviewer, fails the
# build. Add a character here only on purpose.
TEXT_CHARSET = frozenset("\t\n" + "".join(map(chr, range(0x20, 0x7f)))
                         + "\u00a7\u00b1\u00b7\u00d7\u2013\u2014\u2026\u2192\u2260\u2265")
# What the package may not contain, by the categories skill marketplaces scan for. The package
# is instructions only: no URL, command, code, variable or credential belongs in it, so each rule
# is a plain pattern and any match fails the build. Wording that merely resembles one (the word
# "eval" for an evaluation) fails too: reword it rather than weaken the rule. Every repetition is
# bounded, so a long line costs linear time.
CONTENT_RULES = (
    ("network", URL, "a URL"),
    ("network", re.compile(r"\b(?:curl|wget|Invoke-WebRequest|Invoke-RestMethod|"
                           r"Start-BitsTransfer|certutil)\b", re.IGNORECASE),
     "a download command"),
    ("network", re.compile(r"\b(?:iwr|irm)\s|\bgit\s+clone\b"), "a download command"),
    # A pipe into a shell or interpreter, as a command ends ("| sh", "| sudo bash -s",
    # "| /bin/bash", "| env bash"), so a table cell such as "| Bash |" is not one.
    ("dangerous-code", re.compile(r"\|\s*(?:sudo\s+(?:-\S{1,64}\s+){0,16})?(?:env\s+)?"
                                  r"(?:/[\w./-]{0,64}/)?(?:(?:ba|z|k|da|fi)?sh|python3?|node|perl|"
                                  r"ruby|pwsh|powershell|cmd)(?:\.exe)?(?=\s*(?:$|[;&)]|-))"),
     "a pipe into a shell"),
    ("dangerous-code", re.compile(r"\b(?:ba|z|k|da|fi)?sh\s+-c\b|\bcmd(?:\.exe)?\s+/[ck]\b|"
                                  r"\b(?:python3?|py|node|deno|perl|ruby|php)(?:\.exe)?\s+-[cem]\b|"
                                  r"\b(?:pwsh|powershell)(?:\.exe)?\s+-(?:c|e|ec|enc|command|"
                                  r"encodedcommand)\b|\bInvoke-Expression\b|\|\s*iex\b|\biex\s*\(",
                                  re.IGNORECASE), "a shell or interpreter command"),
    ("dangerous-code", re.compile(r"\b(?:eval|exec|__import__|subprocess|child_process)\b|"
                                  r"\bos\.(?:system|popen)\b", re.IGNORECASE),
     "dynamic code execution"),
    ("dangerous-code", re.compile(r"\bexec(?:Sync|File|FileSync|v|vp|l|lp|ve|le)\b|"
                                  r"\bnew\s+Function\s*\("), "dynamic code execution"),
    ("dangerous-code", re.compile(r"\brm\s+-[A-Za-z]{0,8}[rRf]|"
                                  r"\bRemove-Item\b[^\n]{0,500}?-Recurse|"
                                  r"\b(?:del|erase|rd|rmdir)\s+/[sq]\b|\bmkfs\b|\bdd\s+if=",
                                  re.IGNORECASE), "a destructive command"),
    # Claude Code runs !`command` and ```! blocks in a skill's text when it loads the skill.
    ("dangerous-code", re.compile(r"(?:^|\s)!`|`{3}\s*!"), "a Claude Code shell command"),
    ("dangerous-code", re.compile(r"<\s*script\b|\bjavascript:|<[a-z][^>]{0,500}?\son[a-z]+\s*=",
                                  re.IGNORECASE), "script markup"),
    ("environment", re.compile(r"\$\{?[A-Z_][A-Z0-9_]+\}?|\$\{[A-Za-z_]\w*\}|"
                               r"(?i:\$env:)[A-Za-z_]\w*|\$\{env:[^}\n]{1,64}\}|"
                               r"%[A-Za-z_][A-Za-z0-9_]*%|\bos\.environ\b|\b(?:os\.)?[Gg]etenv\b|"
                               r"\bprocess\.env\b|\bGetEnvironmentVariable\b|"
                               r"\b[A-Z][A-Z0-9_]*_(?:API_KEY|TOKEN|SECRET|PASSWORD)\b"),
     "an environment variable"),
    ("secret", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "an AWS access key id"),
    ("secret", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})"),
     "a GitHub token"),
    ("secret", re.compile(r"\bglpat-[0-9A-Za-z_-]{20,}"), "a GitLab token"),
    ("secret", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}|"
                          r"\b[rs]k_(?:live|test)_[0-9A-Za-z]{16,}"), "an API secret key"),
    ("secret", re.compile(r"\b(?:hf_[A-Za-z0-9]{30,}|npm_[A-Za-z0-9]{36})\b"),
     "a model-hub or package-registry token"),
    ("secret", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "a Slack token"),
    ("secret", re.compile(r"\bAIza[0-9A-Za-z_-]{35}"), "a Google API key"),
    ("secret", re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
     "a JSON Web Token"),
    ("secret", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"), "a private key"),
    ("secret", re.compile(r"(?i:\bBearer)\s+[A-Za-z0-9._~+/=-]{16,}"), "a bearer token"),
    # A key-like name given a value with a digit in it: `client_secret = 's3cr3t...'` and
    # `"password": "..."` fail, prose such as "Secret: rotated every quarter" does not. The name
    # may start up to 64 characters before the keyword, and the value is scanned up to 256.
    ("secret", re.compile(r"[\w.-]{0,64}?(?:api[_-]?key|access[_-]?key|secret|password|passwd|"
                          r"token|credential)s?['\"]?[ \t]*[:=][ \t]*['\"]?"
                          r"(?![0-9]{1,8}[A-Za-z]?-[A-Za-z])(?=[^\s'\"]{0,255}[0-9])"
                          r"[^\s'\"]{12,}", re.IGNORECASE),
     "a credential assignment"),
    # Text an agent reads but a reviewer cannot see: format and bidirectional controls,
    # zero-width characters, variation selectors, fillers, Unicode tags, private use; and a
    # Markdown link definition, which renders as nothing ("[//]: # (...)" is a common comment).
    ("hidden-text", re.compile("[\u00ad\u034f\u061c\u115f-\u1160\u17b4-\u17b5\u180b-\u180f"
                               "\u200b-\u200f\u2028-\u202e\u2060-\u206f\u3164\ue000-\uf8ff"
                               "\ufe00-\ufe0f\ufeff\uffa0\ufff9-\ufffb\U000e0000-\U000e0fff"
                               "\U000f0000-\U0010ffff]"),
     "an invisible, bidirectional or private-use character"),
    ("hidden-text", re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]"), "a control character"),
    ("hidden-text", re.compile(r"^[ \t>*+0-9.)-]{0,64}\[(?:[^\]\\\n]|\\.){1,999}\]:|"
                               r"^(?:[^\[\]\\\n]|\\.){0,999}\]:"),
     "a Markdown link definition, which rendered views hide"),
)
# Plain scalars that YAML 1.1 (PyYAML) reads as a boolean, null, number or date, not as text.
YAML_TYPED = re.compile(r"(?i)y|yes|n|no|true|false|on|off|null|~|"
                        r"[-+]?(?:0b[01_]+|0x[0-9a-f_]+|0[0-7_]*|[1-9][0-9_]*(?::[0-5]?[0-9])*)|"
                        r"[-+]?(?:[0-9][0-9_]*)?\.[0-9_]*(?:e[-+][0-9]+)?|[-+]?\.(?:inf|nan)|"
                        r"[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}(?:[t ].*)?")
# Reparse points that are links: symbolic links, and the junctions Path.is_symlink() misses.
LINK_REPARSE_TAGS = {getattr(stat, name) for name in ("IO_REPARSE_TAG_SYMLINK",
                                                      "IO_REPARSE_TAG_MOUNT_POINT")
                     if hasattr(stat, name)}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def read_frontmatter(text):
    """({key: value}, [problems]) for SKILL.md's frontmatter, read strictly.

    One `key: value` per line; each key in FRONTMATTER_KEYS exactly once; `name` and
    `description` plain one-line scalars, `tags` a one-line flow sequence (`[a, b]`), returned as
    a list. Block scalars, quoting, continuation lines and comments are refused: they are where
    YAML parsers, and the line-based readers some skill hosts use, start to disagree, and every
    reader should get the same values from this file.
    """
    lines = text.split("\n")
    if lines[0] != "---":
        return {}, ["SKILL.md: the first line must be `---` (frontmatter starts the file)"]
    if "---" not in lines[1:]:
        return {}, ["SKILL.md: the frontmatter is not closed by a `---` line"]
    close = lines.index("---", 1)
    fields, problems = {}, []
    for number, line in enumerate(lines[1:close], 2):
        match = re.fullmatch(r"([a-z][a-z0-9_-]*): (\S(?:.*\S)?)", line)
        if not match:
            problems.append(f"SKILL.md line {number}: expected `key: value` on one line, "
                            f"not {line!r}")
            continue
        key, value = match.groups()
        if key not in FRONTMATTER_KEYS:
            problems.append(f"SKILL.md line {number}: unknown frontmatter key {key!r} (the "
                            f"package allows {', '.join(FRONTMATTER_KEYS)})")
        elif key in fields:
            problems.append(f"SKILL.md line {number}: {key!r} appears twice")
        else:
            fields[key] = value
    for key in FRONTMATTER_KEYS:
        if key not in fields:
            problems.append(f"SKILL.md: the frontmatter has no `{key}`")

    name = fields.get("name")
    if name is not None:
        if not SLUG.fullmatch(name) or len(name) > MAX_NAME:
            problems.append(f"SKILL.md: name {name!r} must be lowercase letters, digits and "
                            f"single hyphens, at most {MAX_NAME} characters")
        if name != PACKAGE_NAME:
            problems.append(f"SKILL.md: name {name!r} must be {PACKAGE_NAME!r}, the package "
                            "folder's name")
        problems += [f"SKILL.md: name {name!r} contains the reserved word {word!r}"
                     for word in RESERVED_WORDS if word in name]
    description = fields.get("description")
    if description is not None:
        if build.yaml_value(description) != description:
            problems.append("SKILL.md: description must be a plain YAML scalar (no leading "
                            "indicator, ': ', ' #', or quotes), so it reads the same everywhere")
        if len(description) > MAX_DESCRIPTION:
            problems.append(f"SKILL.md: description is {len(description)} characters; the "
                            f"limit is {MAX_DESCRIPTION}")
        if "<" in description or ">" in description:
            problems.append("SKILL.md: description must not contain < or > (hosts reject "
                            "markup in it)")
        if YAML_TYPED.fullmatch(description):
            problems.append(f"SKILL.md: YAML reads the description {description!r} as a "
                            "boolean, null or number, not text")
    tags = fields.get("tags")
    if tags is not None:
        match = re.fullmatch(r"\[([^\[\]{}]*)\]", tags)
        # `[a, b]`: one ASCII space after each comma and nowhere else, so no reader trims a
        # character (a tab, a no-break space) that another keeps or refuses.
        raw = match.group(1).split(",") if match else []
        items = [item[1:] if index and item.startswith(" ") else item
                 for index, item in enumerate(raw)]
        bad = [item for item in items if not SLUG.fullmatch(item) or len(item) > MAX_TAG
               or YAML_TYPED.fullmatch(item)]
        bad += [item for item in raw[1:] if not item.startswith(" ")]  # `[a,b]`: one space
        if not match or not items or bad:
            problems.append(f"SKILL.md: tags must be a one-line list such as [planning, "
                            f"architecture] of lowercase hyphenated words (at most {MAX_TAG} "
                            f"characters each), not {tags!r}")
        elif len(set(items)) != len(items) or len(items) > MAX_TAGS:
            problems.append(f"SKILL.md: tags must be unique, at most {MAX_TAGS}")
        fields["tags"] = items
    return fields, problems


def link_problems(files):
    """Every path a package file names must be a file in the package, no link may leave the
    package, and SKILL.md must name exactly the reference files the package holds."""
    problems = []
    for rel, data in sorted(files.items()):
        if not rel.endswith(".md"):
            continue
        text = data.decode("utf-8")
        for found in sorted(set(REFERENCE_PATH.findall(text))):
            if found not in files:
                problems.append(f"{rel}: names {found}, which is not in the package")
        prose = CODE_FENCE.sub("", text)  # a link shown as code is not a link
        targets = (MARKDOWN_LINK.findall(prose) + LINK_DEFINITION.findall(prose)
                   + AUTOLINK.findall(prose) + HTML_LINK.findall(prose))
        for target in targets:
            if SCHEME.match(target):  # a URL, mailto:, a drive letter, an editor deep link…
                problems.append(f"{rel}: links to {target}; the package links only to its own "
                                "files")
                continue
            path = target.split("#", 1)[0]
            if not path:
                continue  # an anchor in the same file
            # POSIX semantics on every OS: os.path on Windows would turn / into \.
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(rel), path))
            if resolved.startswith(("/", "../")) or resolved == ".." or resolved not in files:
                problems.append(f"{rel}: links to {target}, which is not in the package")
    named = set(REFERENCE_PATH.findall(files.get("SKILL.md", b"").decode("utf-8")))
    shipped = {rel for rel in files if rel.startswith("reference/")}
    for rel in sorted(shipped - named):
        problems.append(f"{rel} is in PACKAGE_FILES, but SKILL.md never names it: drop it from "
                        "the package, or say in SKILL.md when to load it")
    return problems


def content_problems(rel, data, allowed_urls=(), comments=ALLOWED_COMMENTS):
    """What the package policy refuses in one file: binary or undecodable bytes, a script, a
    character outside TEXT_CHARSET, an HTML comment other than `comments` (None: any), and every
    CONTENT_RULES match (URLs in `allowed_urls` excepted)."""
    name = PurePosixPath(rel).name
    if not (name.endswith((".md", ".mdc")) or name == "LICENSE"):
        return [f"{rel}: only Markdown files and LICENSE may ship"]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return [f"{rel}: not UTF-8 text ({exc})"]
    problems = []
    if text.startswith("#!"):
        problems.append(f"{rel}: starts with #!, like a script")
    for number, line in enumerate(text.split("\n"), 1):
        for category, pattern, what in CONTENT_RULES:
            for match in pattern.finditer(line):
                if category == "network" and match.group(0) in allowed_urls:
                    continue
                problems.append(f"{rel} line {number}: {category}: {what} "
                                f"({match.group(0)!r})")
        for char in sorted(set(line) - TEXT_CHARSET):
            problems.append(f"{rel} line {number}: hidden-text: U+{ord(char):04X} "
                            f"{unicodedata.name(char, 'unnamed')} is outside the package's "
                            "characters (TEXT_CHARSET)")
    if comments is not None:
        for match in HTML_COMMENT.finditer(text):
            if match.group(0) not in comments:
                number = text.count("\n", 0, match.start()) + 1
                problems.append(f"{rel} line {number}: hidden-text: an HTML comment, which "
                                f"rendered views hide ({match.group(0)[:60]!r})")
    return problems


def zip_bytes(files):
    """The package as a zip whose only top-level entry is the package folder
    (dependency-first-architect/SKILL.md, …): stored, not compressed, sorted, with a fixed
    timestamp and fixed permissions, so the same files give the same bytes on every OS and
    Python version (deflate output differs between zlib builds)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for rel in sorted(files):
            info = zipfile.ZipInfo(f"{PACKAGE_NAME}/{rel}", date_time=ZIP_DATE)
            info.create_system = 3  # Unix, on every OS: the mode below is then what unzip applies
            info.external_attr = (stat.S_IFREG | 0o644) << 16  # a regular file, never executable
            archive.writestr(info, files[rel])
    return buffer.getvalue()


def zip_problems(data, files):
    """Read a built zip back: exactly the package files under the package folder, each regular,
    not executable, safely named and byte-identical to the package."""
    problems = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if archive.testzip() is not None:
            return ["the zip has a corrupt entry"]
        infos = archive.infolist()
        names = [info.filename for info in infos]
        expected = sorted(f"{PACKAGE_NAME}/{rel}" for rel in files)
        if names != expected:
            problems.append(f"the zip holds {names}, not {expected}")
        if len(infos) > MAX_FILES:
            problems.append(f"the zip holds {len(infos)} files; the limit is {MAX_FILES}")
        for info in infos:
            mode = info.external_attr >> 16
            parts = PurePosixPath(info.filename).parts
            if (info.filename.startswith("/") or "\\" in info.filename or ".." in parts
                    or ":" in info.filename):
                problems.append(f"zip entry {info.filename!r}: unsafe name")
            if info.is_dir() or not stat.S_ISREG(mode) or mode & 0o111:
                problems.append(f"zip entry {info.filename!r}: not a plain, non-executable file")
            rel = info.filename[len(PACKAGE_NAME) + 1:]
            if rel in files and archive.read(info) != files[rel]:
                problems.append(f"zip entry {info.filename!r}: differs from the package file")
    return problems


def is_link(path):
    """True for a symbolic link, and on Windows for a junction (Path.is_symlink() says False)."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(info.st_mode) or getattr(info, "st_reparse_tag", 0) in LINK_REPARSE_TAGS


def source_files(root):
    """({rel: LF bytes} for PACKAGE_FILES under `root`, [problems]). A file must be a regular
    file inside `root`, reached without a symbolic link or junction."""
    root = Path(os.path.abspath(root))
    real_root = root.resolve()
    files, problems = {}, []
    for rel in PACKAGE_FILES:
        path = root / rel
        steps = [PurePosixPath(rel), *PurePosixPath(rel).parents][:-1]  # the file, its folders
        links = [step for step in steps if is_link(root / step)]
        if links:
            problems.append(f"{rel}: reached through a symbolic link or junction "
                            f"({root / links[-1]})")
        elif not path.is_file():
            problems.append(f"{rel}: missing (it is in PACKAGE_FILES)")
        elif real_root not in path.resolve().parents:
            problems.append(f"{rel}: resolves outside the repository")
        else:
            files[rel] = build.read_lf(path)
    return files, problems


def plan(root=ROOT):
    """(outputs, problems): every file a build writes, {POSIX path under the output dir: bytes},
    made from the sources under `root` and checked in memory. Nothing is written."""
    root = Path(os.path.abspath(root))
    files, problems = source_files(root)
    try:
        version = build.parse_version(build.read_lf(root / "VERSION").decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return {}, problems + [f"VERSION: {exc}"]
    if problems:
        return {}, problems

    for rel, data in sorted(files.items()):
        problems += content_problems(rel, data)
    if problems:
        return {}, problems  # the rest reads the files as text
    skill = files["SKILL.md"].decode("utf-8")
    fields, frontmatter = read_frontmatter(skill)
    problems += frontmatter
    try:
        name, description, body = build.parse_skill(skill)
    except ValueError as exc:
        problems.append(f"SKILL.md: {exc}")
    else:
        if (name, description) != (fields.get("name"), fields.get("description")):
            problems.append("SKILL.md: build.py reads a different name or description than "
                            "the package check does")
        problems += [f"SKILL.md: the method heading {heading!r} is missing"
                     for heading in METHOD_HEADINGS
                     if not re.search(rf"^{re.escape(heading)}", body, re.MULTILINE)]
    problems += link_problems(files)
    if len(files) > MAX_FILES:
        problems.append(f"the package holds {len(files)} files; the limit is {MAX_FILES}")

    assets = {f"{PACKAGE_NAME}-v{version}.zip": zip_bytes(files)}
    problems += zip_problems(assets[f"{PACKAGE_NAME}-v{version}.zip"], files)
    try:
        adapters = build.render(version, root)
    except (OSError, ValueError) as exc:
        return {}, problems + [f"adapters: {exc}"]
    for path, text in adapters.items():
        rel = path.relative_to(build.ROOT).as_posix()
        committed = root / rel
        data = text.encode("utf-8")
        if not committed.is_file() or build.read_lf(committed) != data:
            problems.append(f"{rel} is stale or missing: run `python build.py` and commit it")
        # The adapters carry build.py's banner comment and END marker, and its repository URL.
        problems += content_problems(f"{RELEASE}/{ADAPTER_ASSETS[path]}", data, ADAPTER_URLS,
                                     comments=None)
        assets[ADAPTER_ASSETS[path]] = data
    if problems:
        return {}, problems

    summed = dict(assets)
    summed.update((f"{PACKAGE_NAME}/{rel}", data) for rel, data in files.items())
    assets["SHA256SUMS"] = build.render_sums(summed).encode("utf-8")
    outputs = {f"{PACKAGE_NAME}/{rel}": data for rel, data in files.items()}
    outputs.update((f"{RELEASE}/{name}", data) for name, data in assets.items())
    return outputs, []


def prepare_out(out, root=ROOT):
    """Make `out` an output directory holding only MARKER, and return it. An existing directory
    is emptied only if it is empty or carries MARKER, and holds no link; the repository, its
    parents and the home directory are refused outright."""
    out = Path(os.path.abspath(out))
    if is_link(out):
        raise ValueError(f"refusing to build into {out}: it is a symbolic link or junction")
    real = out.resolve()
    for keep in (Path(root).resolve(), Path.home().resolve()):
        if real == keep or real in keep.parents:
            raise ValueError(f"refusing to build into {out}: it is or contains {keep}")
    if not out.exists():
        out.mkdir(parents=True)
    elif not out.is_dir():
        raise ValueError(f"refusing to build into {out}: it is not a directory")
    else:
        children = sorted(out.iterdir())
        if children and not (out / MARKER).is_file():
            raise ValueError(f"refusing to delete {out}: it is not empty, and package_skill.py "
                             f"did not make it (no {MARKER})")
        foreign = [child.name for child in children
                   if child.name not in (MARKER, PACKAGE_NAME, RELEASE, *OS_METADATA)]
        if foreign:  # e.g. --out ~/.claude/skills, and another skill installed there since
            raise ValueError(f"refusing to delete {out}: it also holds {', '.join(foreign)}, "
                             "which no build makes")
        for folder, dirs, names in os.walk(out):
            for entry in dirs + names:
                if is_link(Path(folder) / entry):  # a build never makes one: do not follow it
                    raise ValueError(f"refusing to delete {out}: it holds a link "
                                     f"({Path(folder) / entry}); remove it by hand")
        # Everything but the marker first, so a file that cannot be deleted (open elsewhere)
        # leaves a directory the next build still recognizes as its own.
        for child in children:
            if child.name == MARKER:
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    (out / MARKER).write_bytes(MARKER_TEXT)
    return out


def output_problems(out, outputs):
    """Compare the output directory on disk with `outputs`: no file, folder or link it should not
    hold, nothing missing, every byte as built, and the package under the file limit."""
    out = Path(out)
    expected = dict(outputs)
    expected[MARKER] = MARKER_TEXT
    folders = {parent.as_posix() for rel in expected for parent in PurePosixPath(rel).parents}
    problems, seen, count = [], set(), 0
    for folder, dirs, names in os.walk(out):
        for entry in sorted(dirs) + sorted(names):
            path = Path(folder) / entry
            rel = path.relative_to(out).as_posix()
            if rel.startswith(f"{PACKAGE_NAME}/") and entry in names:
                count += 1  # every file in the package, hidden ones included
            if is_link(path):
                problems.append(f"{rel}: a symbolic link or junction")
            elif entry in dirs:
                if rel not in folders:
                    problems.append(f"{rel}/: a folder the build did not make")
            elif rel not in expected:
                problems.append(f"{rel}: a file the build did not make")
            else:
                seen.add(rel)
                if path.read_bytes() != expected[rel]:
                    problems.append(f"{rel}: differs from what was built")
    problems += [f"{rel}: missing" for rel in sorted(set(expected) - seen)]
    if count > MAX_FILES:
        problems.append(f"{PACKAGE_NAME}/ holds {count} files; the limit is {MAX_FILES}")
    return problems


def build_into(out, root=ROOT):
    """Plan, write and verify a build in `out`: (outputs, problems). Nothing is written unless
    the plan has no problems."""
    outputs, problems = plan(root)
    if problems:
        return {}, problems
    out = prepare_out(out, root)
    for rel, data in sorted(outputs.items()):
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)  # bytes: the same LF output on every OS
    return outputs, output_problems(out, outputs)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build and verify the skill package and the release assets.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help="output directory (default: dist/); deleted and rebuilt, but only "
                             "if it is empty or an earlier output of this script")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # paths and messages may hold non-ASCII
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    try:
        outputs, problems = build_into(args.out, ROOT)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    for problem in problems:
        print(f"ERROR: {problem}", file=sys.stderr)
    if problems:
        print(f"\nERROR: {len(problems)} problem(s); nothing in {args.out} may be published.",
              file=sys.stderr)
        return 1

    out = Path(os.path.abspath(args.out))
    package = sorted(rel for rel in outputs if rel.startswith(f"{PACKAGE_NAME}/"))
    print(f"Package {out / PACKAGE_NAME} ({len(package)} files; the limit is {MAX_FILES}):")
    for rel in package:
        print(f"  {sha256(outputs[rel])}  {len(outputs[rel]):>6} bytes  {rel}")
    print(f"Release assets in {out / RELEASE}:")
    for rel in sorted(rel for rel in outputs if rel.startswith(f"{RELEASE}/")):
        print(f"  {sha256(outputs[rel])}  {len(outputs[rel]):>6} bytes  {rel}")
    print("Checks passed: allowlist, frontmatter, method headings, links, content rules, "
          "adapters in sync, zip layout, file count, output tree.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
