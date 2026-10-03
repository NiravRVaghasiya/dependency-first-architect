"""What a generation session can see: its working directory, its skill, and its request.

Sessions can see their own working directory, so every one runs in a fresh, empty directory whose
name is a hash, never the condition. A skill condition gets its skill as a session-only plugin
holding SKILL.md plus exactly the reference files that SKILL.md names, so a session cannot find
material the skill does not load. That rule alone keeps the scoring rubric (reference/evals.md)
away from v2 sessions, because v2's SKILL.md never names it, and ships it to the v1.0.0 condition,
whose Step 8 self-score reads it, so v1 runs as it was measured. Providers without a skill
mechanism get the same files inlined as instructions (`render_instructions`), so every provider
sees the same content.
"""

import hashlib
import json
import posixpath
import re
import shutil
from pathlib import Path

REFERENCE_RE = re.compile(r"reference/[A-Za-z0-9_.-]+\.md")
LENGTH_CAP = "\n\nLength limit: your entire answer must be at most {n} words."
INLINE_NOTE = ("The reference files named above are included below, so they are already loaded; "
               "do not try to open them.")
NEUTRAL_NAME = re.compile(r"^w[0-9a-f]{8}$")
FRONTMATTER_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)[ \t]*:[ \t]*(.*)$")
BLOCK_SCALAR = re.compile(r"[>|][0-9+-]*")
# As in build.py: a value YAML would not read back unchanged as a plain scalar gets quoted.
YAML_NEEDS_QUOTES = re.compile(r"^[\s!&*?:,\[\]{}#|>@`'\"%-]|:\s|\s#|:$|\s$|[\x00-\x1f\x7f]")


def read_lf(path):
    """A file's bytes with CRLF as LF, so hashes match a fresh checkout on every OS."""
    return Path(path).read_bytes().replace(b"\r\n", b"\n")


def split_frontmatter(text):
    """({key: one-line value}, body) of a SKILL.md. Only top-level one-line keys are read."""
    lines = text.replace("\r\n", "\n").split("\n")
    if lines[0].rstrip() != "---":
        raise ValueError("SKILL.md must start with YAML frontmatter delimited by ---")
    close = next((i for i, line in enumerate(lines) if i and line.rstrip() == "---"), None)
    if close is None:
        raise ValueError("SKILL.md frontmatter is not closed by a --- line")
    fields = {}
    for line in lines[1:close]:
        match = FRONTMATTER_KEY.match(line)
        if match:
            fields[match.group(1)] = match.group(2).strip().strip("'\"")
    return fields, "\n".join(lines[close + 1:]).lstrip("\n")


def _skill_text(skill_dir):
    path = Path(skill_dir) / "SKILL.md"
    try:
        return read_lf(path).decode("utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from None
    except UnicodeDecodeError:
        raise ValueError(f"{path} is not valid UTF-8") from None


def read_skill(skill_dir):
    """(frontmatter name, body) of `skill_dir`/SKILL.md."""
    fields, body = split_frontmatter(_skill_text(skill_dir))
    return fields.get("name", ""), body


def skill_description(skill_dir):
    """The `description` of `skill_dir`/SKILL.md as one line ("" if none), read as build.py reads
    it: a value may continue on indented lines or be a `>-` / `|` block, joined with spaces."""
    lines = _skill_text(skill_dir).split("\n")
    split_frontmatter("\n".join(lines))  # raises ValueError if the frontmatter is malformed
    close = next(i for i, line in enumerate(lines) if i and line.rstrip() == "---")
    parts, inside = [], False
    for line in lines[1:close]:
        if not line.strip():
            continue
        if line[0] in " \t":
            if inside:
                parts.append(line.strip())
            continue
        match = FRONTMATTER_KEY.match(line.rstrip())  # a key line, or anything else, ends a value
        inside = bool(match) and match.group(1) == "description"
        value = match.group(2).strip() if inside else ""
        if value and not BLOCK_SCALAR.fullmatch(value):
            parts.append(value)
    return " ".join(parts)


def yaml_value(text):
    """`text` as a one-line YAML value: plain when YAML reads it back unchanged, else quoted."""
    if text and not YAML_NEEDS_QUOTES.search(text):
        return text
    return json.dumps(text, ensure_ascii=False)  # a JSON string is a YAML double-quoted scalar


def cursor_rule(text, description):
    """`text` as a Cursor rule with the frontmatter build.py gives the adapter (an agent-requested
    rule: its description, no globs, alwaysApply false), so every arm's rule loads the same way.
    Without frontmatter Cursor treats a rule as manual, applied only when @-mentioned."""
    head = f"---\ndescription: {yaml_value(description)}\nglobs:\nalwaysApply: false\n---\n\n"
    return head + text.strip("\n") + "\n"


def package_contents(skill_dir, skill_name=None):
    """(included, excluded): the reference files SKILL.md's body names, in order of first mention,
    and the named files that are not shipped (always empty: a skill ships exactly what it names;
    the second value is kept so callers and manifests keep their shape).

    Raises ValueError if an included file does not exist (a skill that points at a missing file
    would silently run without it).
    """
    skill_dir = Path(skill_dir)
    _, body = read_skill(skill_dir)
    included = list(dict.fromkeys(REFERENCE_RE.findall(body)))
    excluded = []
    missing = [rel for rel in included if not (skill_dir / rel).is_file()]
    if missing:
        raise ValueError(f"{skill_dir / 'SKILL.md'} names {', '.join(missing)}, which "
                         f"{'does' if len(missing) == 1 else 'do'} not exist")
    return included, excluded


def build_plugin(skill_dir, skill_name, dest):
    """Package `skill_dir` as a session plugin at `dest`: (plugin_dir, files).

    Layout: dest/skills/<skill_name>/SKILL.md + the included reference files, and
    dest/.claude-plugin/plugin.json. `files` lists every packaged file in packaging order as
    {"path" (relative to skill_dir), "packaged" (relative to dest), "sha256", "words"}.
    """
    skill_dir, dest = Path(skill_dir), Path(dest)
    name, _ = read_skill(skill_dir)
    if name != skill_name:
        raise ValueError(f"{skill_dir / 'SKILL.md'} is named {name!r}, not {skill_name!r}")
    included, _ = package_contents(skill_dir, skill_name)
    if dest.exists():
        if any(dest.iterdir()) and not (dest / ".claude-plugin" / "plugin.json").is_file():
            raise ValueError(f"refusing to replace {dest}: it exists and is not a plugin directory")
        shutil.rmtree(dest)
    files = []
    for rel in ["SKILL.md", *included]:
        data = read_lf(skill_dir / rel)
        packaged = f"skills/{skill_name}/{rel}"
        target = dest / packaged
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        files.append({"path": rel, "packaged": packaged,
                      "sha256": hashlib.sha256(data).hexdigest(),
                      "words": len(data.decode("utf-8").split())})
    manifest = dest / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    plugin_json = json.dumps({"name": skill_name, "version": "0.0.0"}) + "\n"
    manifest.write_bytes(plugin_json.encode("utf-8"))
    return dest, files


def plugin_skills(plugin_dirs):
    """[(skill name, skill dir)] for every skill packaged in `plugin_dirs`, sorted by name."""
    found = []
    for plugin in plugin_dirs or []:
        skills = Path(plugin) / "skills"
        if skills.is_dir():
            found += [(d.name, d) for d in skills.iterdir() if (d / "SKILL.md").is_file()]
    return sorted(found, key=lambda item: item[0])


def render_instructions(skill_dir):
    """The skill as one instruction text, for providers with no skill mechanism.

    SKILL.md's body (frontmatter removed), then every included reference file under a
    `## Reference: <path>` heading, after a one-line note that they are already loaded.
    """
    skill_dir = Path(skill_dir)
    _, body = read_skill(skill_dir)
    included, _ = package_contents(skill_dir)
    parts = [body.strip("\n")]
    if included:
        parts.append(INLINE_NOTE)
        for rel in included:
            text = read_lf(skill_dir / rel).decode("utf-8").strip("\n")
            parts.append(f"## Reference: {rel}\n\n{text}")
    return "\n\n".join(parts) + "\n"


def neutral_name(key, salt=""):
    """`w` + 8 hex: a directory name that says nothing about the generation it hosts."""
    return "w" + hashlib.sha256(f"{key}{salt}".encode("utf-8")).hexdigest()[:8]


def prepare_workdir(base, gen_id_or_neutral_name, readme_text=None, salt=""):
    """A fresh, empty directory under `base` with a neutral name; README.md if `readme_text`."""
    key = gen_id_or_neutral_name
    name = key if NEUTRAL_NAME.match(key) else neutral_name(key, salt)
    path = Path(base) / name
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    if readme_text:
        (path / "README.md").write_bytes(readme_text.replace("\r\n", "\n").encode("utf-8"))
    return path


def build_request(prompt_cfg, condition_cfg, slash_command=True):
    """(session_prompt, recorded_request) for one generation.

    The length cap, if any, is appended to the request. A skill condition on a provider with
    slash commands (Claude Code) is invoked as `/<skill_name> <request>`. The recorded request is
    the full text the model received as its user turn.
    """
    request = prompt_cfg["request"]
    cap = condition_cfg.get("length_cap_words")
    if cap:
        request += LENGTH_CAP.format(n=cap)
    if condition_cfg["kind"] == "skill" and slash_command:
        session_prompt = f"/{condition_cfg['skill_name']} {request}"
    else:
        session_prompt = request
    return session_prompt, session_prompt


def source_path(skill_dir_cfg, rel):
    """Repo-relative POSIX path of a skill file, from the config's skill_dir and its own path."""
    return posixpath.normpath(posixpath.join(Path(skill_dir_cfg).as_posix(), rel))
