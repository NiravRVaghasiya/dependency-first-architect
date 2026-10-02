#!/usr/bin/env python3
"""Regenerate the Cursor and Codex adapters from the canonical SKILL.md.

Single source of truth: SKILL.md. The adapters are DERIVED — never hand-edit them.
Run:  python build.py
No third-party dependencies; standard library only.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SKILL = ROOT / "SKILL.md"
ADAPTERS = ROOT / "adapters"
CURSOR = ADAPTERS / "cursor-dependency-first-architect.mdc"
CODEX = ADAPTERS / "AGENTS.md"

BANNER_LINES = [
    "GENERATED FILE — DO NOT HAND-EDIT.",
    "Source of truth: SKILL.md. Regenerate with: python build.py",
    "Any manual change here will be overwritten on the next build.",
]


def parse_skill(text):
    """Split SKILL.md into (frontmatter_dict, description, body_markdown)."""
    if not text.startswith("---"):
        raise ValueError("SKILL.md must start with YAML frontmatter delimited by ---")
    parts = text.split("---", 2)
    # parts[0] == "" , parts[1] == frontmatter , parts[2] == body
    front_raw = parts[1]
    body = parts[2].lstrip("\n")

    name = ""
    description = ""
    # Minimal frontmatter parse: support `key: value` and a `description: >-` block.
    lines = front_raw.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("name:"):
            name = line.split(":", 1)[1].strip()
            i += 1
        elif line.strip().startswith("description:"):
            after = line.split(":", 1)[1].strip()
            if after in (">-", ">", "|", "|-", "|+"):
                # Folded/literal block: collect indented following lines.
                i += 1
                block = []
                while i < len(lines) and (lines[i].startswith("  ") or not lines[i].strip()):
                    if not lines[i].strip():
                        i += 1
                        continue
                    block.append(lines[i].strip())
                    i += 1
                description = " ".join(block).strip()
            else:
                description = after
                i += 1
        else:
            i += 1

    if not name:
        raise ValueError("frontmatter missing `name`")
    if not description:
        raise ValueError("frontmatter missing `description`")
    return name, description, body


def banner(comment_prefix):
    return "\n".join(f"{comment_prefix} {line}" for line in BANNER_LINES)


def build_cursor(name, description, body):
    """Cursor .mdc: YAML frontmatter with alwaysApply:false + an HTML-comment banner."""
    fm = [
        "---",
        f"description: {description}",
        "globs:",
        "alwaysApply: false",
        "---",
    ]
    head = "\n".join(fm)
    note = f"<!--\n{banner('')}\n-->".replace("\n \n", "\n\n")
    return f"{head}\n\n{note}\n\n{body.rstrip()}\n"


def build_codex(name, description, body):
    """Codex AGENTS.md: plain markdown, banner as an HTML comment, trigger stated inline."""
    note = f"<!--\n" + "\n".join(BANNER_LINES) + "\n-->"
    header = f"# {name}\n\n**When to use:** {description}"
    return f"{note}\n\n{header}\n\n{body.rstrip()}\n"


def main():
    text = SKILL.read_text(encoding="utf-8")
    name, description, body = parse_skill(text)
    ADAPTERS.mkdir(exist_ok=True)

    cursor_out = build_cursor(name, description, body)
    codex_out = build_codex(name, description, body)

    CURSOR.write_text(cursor_out, encoding="utf-8")
    CODEX.write_text(codex_out, encoding="utf-8")

    # Sanity: the shared sentinel from SKILL.md must survive into both adapters.
    sentinel = "Tradeoff gates"
    for path, out in ((CURSOR, cursor_out), (CODEX, codex_out)):
        if sentinel not in out:
            print(f"ERROR: sentinel '{sentinel}' missing from {path.name}", file=sys.stderr)
            return 1

    print(f"Generated {CURSOR.relative_to(ROOT)}")
    print(f"Generated {CODEX.relative_to(ROOT)}")
    print(f"Sentinel '{sentinel}' present in SKILL.md and both adapters: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
