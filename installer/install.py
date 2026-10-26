#!/usr/bin/env python3
"""Install Dependency-First Architect into a project, for Claude Code, Cursor or Codex.

Run it in your project's folder:
  python3 install-dependency-first-architect-vX.Y.Z.py               find the agent and install
  ... --agent claude-code | --agent cursor | --agent codex   choose the agent (repeatable)
  ... --global      Claude Code only: install for all your projects, in ~/.claude/skills
  ... --project DIR the project folder (default: the git repository around the current folder,
                    else the current folder)
  ... --dry-run     show what would change, and change nothing
  ... --verify      check what is installed, and change nothing
  ... --uninstall   remove what this installer manages, and nothing else

What it writes, and nothing else:
  Claude Code  .claude/skills/dependency-first-architect/   SKILL.md, LICENSE, reference/*.md
  Cursor       .cursor/rules/dependency-first-architect.mdc
  Codex        .codex/dependency-first-architect.md, and a marked block in AGENTS.md (the rest
               of AGENTS.md is kept byte for byte)

Trust boundary: this file carries the release's files and their SHA-256 sums inside it. It makes
no network connection and starts no other program, and its own code reads no environment
variable: the standard library reads the usual few (the home folder, for Path.home(); the
terminal width and language, for argparse's messages). Before it writes anything it checks every
file it carries against those sums. Check this file itself
against the SHA-256 the README gives for this release before you run it.
Safety: a file or folder at a path it manages is replaced or deleted only when this installer
wrote it: a generated file carries the skill's BEGIN banner, and every file in the skill folder
must be, byte for byte, a file of a released version. Anything else is left alone, and the run
stops before writing. Every write goes to a temporary name first and is renamed into place, so an
interrupted run leaves the old install or the new one, never half of each, and the next run
tidies up. Running it again changes nothing.
Exit status: 0 done (or nothing to do); 1 stopped or failed (the agent it stopped at is as it
was; agents reported as installed before that stay installed); 2 usage error, or a question that
could not be asked. Python 3.9+, standard library only.
"""

import argparse
import hashlib
import os
import re
import secrets
import stat
import sys
from pathlib import Path

# package_skill.py replaces the next line with the release's version, its checksums, the files
# it installs, and the checksums of the package files of earlier releases.
VERSION = MANIFEST = PAYLOAD = RELEASED = None

NAME = "dependency-first-architect"
TITLE = "Dependency-First Architect"
# The lines build.py brackets every generated adapter with; installed copies are found by them.
OPEN_LINE = "<!--"
BEGIN_LINE = "BEGIN dependency-first-architect \u2014 GENERATED FILE, DO NOT HAND-EDIT."
END_MARKER = "<!-- END dependency-first-architect -->"
# The banner of the v1.0.0 adapters, which had no BEGIN and END lines.
LEGACY_BANNER = "GENERATED FILE \u2014 DO NOT HAND-EDIT."
# The skill package (package_skill.PACKAGE_FILES), and the release assets each agent installs.
PACKAGE_FILES = (
    "SKILL.md",
    "LICENSE",
    "reference/plan-template.md",
    "reference/validation.md",
    "reference/ai-systems.md",
    "reference/layer-map.md",
)
CURSOR_ASSET = "cursor-dependency-first-architect.mdc"
CODEX_FILE_ASSET = "codex-lazy-dependency-first-architect.md"
CODEX_BLOCK_ASSET = "codex-lazy-AGENTS-snippet.md"
PAYLOAD_NAMES = tuple(f"{NAME}/{rel}" for rel in PACKAGE_FILES) + (
    CURSOR_ASSET, CODEX_FILE_ASSET, CODEX_BLOCK_ASSET)
# Where each agent reads the skill, relative to the project (Claude Code: or the home folder).
SKILL_DIR = (".claude", "skills", NAME)
CURSOR_RULE = (".cursor", "rules", f"{NAME}.mdc")
CURSOR_RULE_V1 = (".cursor", "rules", "cursor-dependency-first-architect.mdc")  # older README
CODEX_FILE = (".codex", f"{NAME}.md")
AGENTS_MD = ("AGENTS.md",)
# Files an operating system drops into folders it shows; never a reason to stop.
OS_METADATA = (".DS_Store", "Thumbs.db", "desktop.ini")
AGENTS = {"claude-code": "Claude Code", "cursor": "Cursor", "codex": "Codex"}
# Codex reads at most this much of its project instructions by default (project_doc_max_bytes).
CODEX_DOC_LIMIT = 32 * 1024
TRY_IT = "Plan a customer-support chatbot over our help-center docs."
LINK_REPARSE_TAGS = {getattr(stat, name) for name in ("IO_REPARSE_TAG_SYMLINK",
                                                      "IO_REPARSE_TAG_MOUNT_POINT")
                     if hasattr(stat, name)}


class Stop(Exception):
    """A reason to change nothing: the message says what was found and what to do."""


class Release:
    """The files this installer carries, checked against the checksums it carries."""

    def __init__(self, version, manifest, payload, released):
        if not (isinstance(version, str) and isinstance(manifest, str)
                and isinstance(payload, dict) and isinstance(released, str)):
            raise Stop("this is the installer's template, not a release. Download the installer "
                       "from a release (see the README), or build one with "
                       "`python3 package_skill.py`.")
        sums = {}
        for line in manifest.splitlines():
            digest, _, name = line.partition("  ")
            sums[name] = digest
        self.version = version
        self.files = {}
        for name in PAYLOAD_NAMES:
            text = payload.get(name)
            data = text.encode("utf-8") if isinstance(text, str) else None
            if data is None or name not in sums or hashlib.sha256(data).hexdigest() != sums[name]:
                raise Stop(f"this installer is damaged: its copy of {name} does not match its "
                           "checksum. Download it again.")
            self.files[name] = data
        # {package path: SHA-256 sums of every released version of it}, line endings as LF.
        self.known = {}
        for line in (manifest + released).splitlines():
            digest, _, name = line.partition("  ")
            if name.startswith(f"{NAME}/") and not line.startswith("#"):
                self.known.setdefault(name[len(NAME) + 1:], set()).add(digest)
        self.known_names = set(self.known)

    def package(self):
        return {rel: self.files[f"{NAME}/{rel}"] for rel in PACKAGE_FILES}


# --- paths and links ---------------------------------------------------------------------------

def is_link(path):
    """True for a symbolic link or a Windows junction; a path that cannot be examined stops the
    run rather than being trusted."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise Stop(f"cannot examine {path}: {exc}") from None
    return stat.S_ISLNK(info.st_mode) or getattr(info, "st_reparse_tag", 0) in LINK_REPARSE_TAGS


def under(base, parts, parents_may_link=False):
    """`base` joined with `parts`, after checking that no existing component below `base` is a
    link: the installer never writes through one. With `parents_may_link` (your own home folder,
    where ~/.claude is often a link a dotfiles manager made) only the last component is checked."""
    path = base
    for index, part in enumerate(parts):
        path = path / part
        if parents_may_link and index < len(parts) - 1:
            continue
        if is_link(path):
            raise Stop(f"{path} is a symbolic link or junction; the installer does not write "
                       "through links. Replace it with a real file or folder, or install by hand "
                       "(README, Manual installation).")
    return path


def lf(data):
    return data.replace(b"\r\n", b"\n")


def token():
    return secrets.token_hex(4)


def write_atomic(path, data):
    """Write `data` to `path` through a temporary file in the same folder, renamed into place.
    An existing file keeps its permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.dfa-tmp-{token()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o644)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if path.is_file():
            os.chmod(tmp, stat.S_IMODE(os.stat(path).st_mode) | stat.S_IWRITE)
        os.replace(tmp, path)
    except BaseException:
        unlink_quietly(tmp)
        raise


def unlink_quietly(path):
    """Delete a temporary file of ours, clearing a read-only flag first (Windows will not delete
    a read-only file); a failure is left for the next run."""
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        os.unlink(path)
    except OSError:
        pass


def unlink_or_fail(path):
    """Delete a file this installer owns, clearing a read-only flag first; errors propagate."""
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass
    os.unlink(path)


def check_writable(path):
    """Stop before writing anything if `path` exists but cannot be replaced or deleted."""
    if path.exists() and not os.access(path, os.W_OK):
        raise Stop(f"{path} is read-only (some version-control tools and sync tools make files "
                   "read-only). Make it writable, then run again.")


def prune_empty(project, parts):
    """After an uninstall: remove the folders `parts` names under `project`, deepest first, for
    as long as each is empty (an installer made them; an empty folder holds nothing of yours)."""
    for depth in range(len(parts), 0, -1):
        folder = project.joinpath(*parts[:depth])
        try:
            if not folder.is_dir() or is_link(folder) or any(folder.iterdir()):
                return
            folder.rmdir()
        except OSError:
            return


def remove_leftover_temp_files(path):
    """Delete the temporary files an interrupted write of `path` left next to it."""
    folder = path.parent
    if not folder.is_dir() or is_link(folder):
        return []
    removed = []
    for child in folder.iterdir():
        if child.name.startswith(f".{path.name}.dfa-tmp-") and child.is_file() and not is_link(child):
            unlink_quietly(child)
            if not child.exists():
                removed.append(child)
    return removed


# --- the Claude Code skill folder --------------------------------------------------------------

def skill_folder_files(folder, release):
    """Every file in a skill folder, as POSIX paths; Stop if it holds a link or a file no release
    of the skill package holds (an older clone of the whole repository, or files someone added)."""
    found, foreign = [], []
    for parent, dirs, names in os.walk(folder):
        for entry in sorted(dirs) + sorted(names):
            path = Path(parent) / entry
            rel = path.relative_to(folder).as_posix()
            if is_link(path):
                raise Stop(f"{path} is a symbolic link or junction; the installer does not "
                           "change a skill folder that holds one. Move it, then run again.")
            if entry in dirs:
                if rel != "reference":
                    foreign.append(rel + "/")
            elif entry in OS_METADATA:
                continue
            elif rel in release.known_names:
                found.append(rel)
            else:
                foreign.append(rel)
    if foreign:
        hint = (" It looks like a copy of the whole repository, which older instructions cloned "
                "there." if any(f.startswith((".git", "eval/", "README")) for f in foreign) else
                " The installer does not delete files it did not write.")
        raise Stop(f"{folder} holds files this installer did not put there "
                   f"({', '.join(sorted(foreign)[:5])}{', ...' if len(foreign) > 5 else ''})."
                   f"{hint} Move that folder somewhere outside {folder.parent} (or move those "
                   "files out of it), then run again.")
    return sorted(found)


def check_skill_folder(folder, release):
    """The files of an existing skill folder this installer may replace or delete: each one must
    be, byte for byte (line endings aside), a file of a released version. Stop otherwise."""
    if not folder.is_dir():
        raise Stop(f"{folder} exists but is not a folder; move it, then run again.")
    files = skill_folder_files(folder, release)
    for rel in files:
        path = folder / rel
        if hashlib.sha256(lf(path.read_bytes())).hexdigest() not in release.known.get(rel, ()):
            raise Stop(f"{path} differs from every released version of {TITLE}: it was edited, "
                       "or it is not this skill. The installer does not delete changes it did "
                       f"not make. Move the folder somewhere outside {folder.parent} to keep it "
                       "(or undo the edit), then run again.")
    return files


def remove_tree(folder, release):
    """Delete a folder this installer made (a staging folder, or an old copy it moved aside):
    only package file names are deleted, and anything else stops the deletion."""
    skill_folder_files(folder, release)
    for parent, dirs, names in os.walk(folder, topdown=False):
        for name in names:
            unlink_quietly(Path(parent) / name)
        for name in dirs:
            os.rmdir(Path(parent) / name)
    os.rmdir(folder)


class SkillTarget:
    agent = "claude-code"

    def __init__(self, base, scope, release):
        self.base, self.scope, self.release = base, scope, release
        mine = scope == "global"  # your own ~/.claude may be a link; a project's may not
        if not mine:
            under(base, SKILL_DIR[:1])
        self.path = under(base, SKILL_DIR, parents_may_link=mine)
        # Staging and old copies sit next to the real skills folder's parent, on its filesystem,
        # so every rename stays on one device even when ~/.claude/skills is a link elsewhere.
        self.work = Path(os.path.realpath(self.path.parent)).parent
        self.label = f"{'~/' if scope == 'global' else ''}{'/'.join(SKILL_DIR)}/"

    def leftovers(self):
        if not self.work.is_dir():
            return []
        return sorted(p for p in self.work.iterdir()
                      if re.fullmatch(r"\.dfa-(staging|old)-[0-9a-f]+", p.name) and p.is_dir()
                      and not is_link(p))

    def recover(self, log):
        """Finish or undo what an interrupted run left: a copy moved aside whose replacement never
        arrived is moved back; staging folders and old copies are deleted."""
        for leftover in self.leftovers():
            try:
                if leftover.name.startswith(".dfa-old-") and not self.path.exists():
                    check_skill_folder(leftover, self.release)
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    os.rename(leftover, self.path)
                    log(f"restored {self.label}, which an interrupted run had moved aside")
                    continue
                remove_tree(leftover, self.release)
                log(f"deleted {leftover.name}, left by an interrupted run")
            except (Stop, OSError) as exc:
                log(f"warning: left {leftover} in place: {exc}")

    def state(self, release):
        if not self.path.exists() and not is_link(self.path):
            return "absent", None
        files = check_skill_folder(self.path, release)
        package = release.package()
        same = files == sorted(package) and all(
            lf((self.path / rel).read_bytes()) == data for rel, data in package.items())
        return ("current" if same else "different"), None

    def install(self, release, log):
        staging = self.work / f".dfa-staging-{token()}"
        staging.mkdir(parents=True)
        old = None
        try:
            # SKILL.md first: a staging folder is recognizable from its first file on.
            for rel in sorted(release.package(), key=lambda r: (r != "SKILL.md", r)):
                target = staging / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "xb") as handle:
                    handle.write(release.package()[rel])
                    handle.flush()
                    os.fsync(handle.fileno())
            for rel, data in release.package().items():
                if (staging / rel).read_bytes() != data:
                    raise OSError(f"{staging / rel} did not read back as written")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                check_skill_folder(self.path, release)
                old = self.work / f".dfa-old-{token()}"
                os.rename(self.path, old)
            try:
                os.rename(staging, self.path)
            except BaseException:
                if old is not None:
                    os.rename(old, self.path)
                    old = None
                raise
        except BaseException:
            if staging.exists():
                try:
                    remove_tree(staging, release)
                except (Stop, OSError):
                    pass  # the next run deletes it; the original error is the one to report
            raise
        if old is not None:
            try:
                remove_tree(old, release)
            except (Stop, OSError) as exc:
                log(f"warning: could not delete the old copy {old} ({exc}); the next run will")

    def uninstall(self, log):
        check_skill_folder(self.path, self.release)
        old = self.work / f".dfa-old-{token()}"
        os.rename(self.path, old)  # gone in one step; the rest is tidying up
        remove_tree(old, self.release)

    def prune(self, project):
        prune_empty(project, SKILL_DIR[:-1])

    def preflight(self):
        pass  # a read-only file in the folder is no obstacle: the folder is renamed, files deleted

    def verify(self, release):
        return self.state(release)[0] == "current"


# --- single files: the Cursor rule and the lazy Codex file -------------------------------------

def owned_text(data):
    """True if `data` is a file this installer (or build.py's v1.0.0 adapters) generated."""
    text = data.decode("utf-8", "replace")
    return (BEGIN_LINE in text and END_MARKER in text) or (
        LEGACY_BANNER in text and "dependency-first" in text.lower())


def banner_version(data):
    match = re.search(r"Version (\S+) \u00b7 built from SKILL\.md", data.decode("utf-8", "replace"))
    return match.group(1) if match else None


class FileTarget:
    def __init__(self, base, parts, asset, label, also=()):
        self.parts = parts
        self.path = under(base, parts)
        self.asset, self.label = asset, label
        self.also = [under(base, extra) for extra in also]  # older copies to remove

    def recover(self, log):
        for path in [self.path, *self.also]:
            for leftover in remove_leftover_temp_files(path):
                log(f"deleted {leftover.name}, left by an interrupted run")

    def check(self, path):
        if not path.is_file():
            raise Stop(f"{path} exists but is not a file; move it, then run again.")
        data = path.read_bytes()
        if not owned_text(data):
            raise Stop(f"{path} was not written by this installer (it has no {TITLE} banner), "
                       "so it is left alone. Rename or move it, then run again.")
        return data

    def stale_copies(self):
        return [p for p in self.also if p.exists() and owned_text(p.read_bytes())]

    def preflight(self):
        """Stop before any write if a file this run would replace or delete is read-only."""
        for path in [self.path, *self.stale_copies()]:
            check_writable(path)

    def state(self, release):
        if not self.path.exists():
            return ("different" if self.stale_copies() else "absent"), None
        data = self.check(self.path)
        if lf(data) == release.files[self.asset] and not self.stale_copies():
            return "current", None
        return "different", banner_version(data)

    def install(self, release, log):
        before = self.check(self.path) if self.path.exists() else None
        stale = self.stale_copies()
        write_atomic(self.path, release.files[self.asset])
        try:
            for path in stale:
                unlink_or_fail(path)
                log(f"deleted an older copy, {path.name}")
        except BaseException:
            self.put_back(before)
            raise

    def put_back(self, before):
        """Undo this run's write of the file: its earlier bytes, or no file."""
        if before is None:
            unlink_quietly(self.path)
        else:
            write_atomic(self.path, before)

    def uninstall(self, log):
        for path in [self.path, *self.stale_copies()]:
            if path.exists():
                self.check(path)
                unlink_or_fail(path)

    def verify(self, release):
        return self.path.is_file() and lf(self.path.read_bytes()) == release.files[self.asset]

    def prune(self, project):
        prune_empty(project, self.parts[:-1])


class CursorTarget(FileTarget):
    agent = "cursor"

    def __init__(self, base):
        super().__init__(base, CURSOR_RULE, CURSOR_ASSET, "/".join(CURSOR_RULE),
                         also=(CURSOR_RULE_V1,))


# --- AGENTS.md: the managed block --------------------------------------------------------------

def find_blocks(text):
    """[(start, end)] character spans of every managed block in `text`: from the `<!--` line just
    above the BEGIN line through the END line and its line break. Stop on a damaged block, since
    guessing its extent could delete the user's own instructions."""
    lines, offsets, pos = text.split("\n"), [], 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1
    bare = [line.rstrip("\r").strip() for line in lines]
    blocks, i = [], 0
    while i < len(lines):
        if bare[i] == BEGIN_LINE:
            if i == 0 or bare[i - 1] != OPEN_LINE:
                raise Stop(f"AGENTS.md line {i + 1}: the {TITLE} block's BEGIN line has no `<!--` "
                           "line above it. Fix or delete the block by hand, then run again.")
            j = i + 1
            while j < len(lines) and bare[j] != END_MARKER:
                if bare[j] == BEGIN_LINE:
                    raise Stop(f"AGENTS.md line {j + 1}: a {TITLE} block starts inside another. "
                               "Fix or delete the blocks by hand, then run again.")
                j += 1
            if j == len(lines):
                raise Stop(f"AGENTS.md line {i + 1}: the {TITLE} block has no END line. Fix or "
                           "delete it by hand, then run again.")
            blocks.append((offsets[i - 1], min(offsets[j] + len(lines[j]) + 1, len(text))))
            i = j + 1
            continue
        if bare[i] == END_MARKER:
            raise Stop(f"AGENTS.md line {i + 1}: a {TITLE} END line without a BEGIN line. Fix or "
                       "delete it by hand, then run again.")
        i += 1
    return blocks


def with_blank_line_above(text, start):
    """Extend a block's start over the blank line directly above it, if there is one."""
    if start >= 1 and text[start - 1] == "\n":
        above = text.rfind("\n", 0, start - 1) + 1
        if text[above:start - 1].strip() == "":
            return above
    return start


def merge_agents_md(text, block):
    """`text` (AGENTS.md, or None if it does not exist) with exactly one managed block equal to
    `block`, or with none if `block` is None. Everything outside the managed blocks is kept;
    extra blocks (an older install appended more than once) go, with the blank line above each."""
    text = text or ""
    bom = "\ufeff" if text.startswith("\ufeff") else ""
    body = text[len(bom):]
    blocks = find_blocks(body)
    newline = "\r\n" if "\n" in body and body.count("\r\n") == body.count("\n") else "\n"
    if block is not None and newline == "\r\n":
        block = block.replace("\n", "\r\n")
    keep_first = block is not None and bool(blocks)
    for index in range(len(blocks) - 1, -1, -1):
        start, end = blocks[index]
        if index == 0 and keep_first:
            body = body[:start] + block + body[end:]
        else:
            body = body[:with_blank_line_above(body, start)] + body[end:]
    if block is not None and not blocks:
        if body.strip() == "":
            body = block
        else:
            if not body.endswith("\n"):
                body += newline
            body += newline + block
    return bom + body


def unmarked_copy_line(text):
    """The line number of an older, unmarked copy of the skill outside every managed block."""
    spans = find_blocks(text)
    for match in re.finditer(r"(?m)^# dependency-first-architect\r?$", text):
        if not any(start <= match.start() < end for start, end in spans):
            return text.count("\n", 0, match.start()) + 1
    return None


class CodexTarget:
    agent = "codex"

    def __init__(self, base):
        self.file = FileTarget(base, CODEX_FILE, CODEX_FILE_ASSET, "/".join(CODEX_FILE))
        self.agents_md = under(base, AGENTS_MD)
        self.label = f"{self.file.label} and its block in AGENTS.md"

    def read_agents_md(self):
        if not self.agents_md.exists():
            return None
        if not self.agents_md.is_file():
            raise Stop(f"{self.agents_md} exists but is not a file; move it, then run again.")
        data = self.agents_md.read_bytes()
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise Stop(f"{self.agents_md} is not UTF-8 text (it may be UTF-16, which some "
                       "editors and PowerShell's `>>` write). Save it as UTF-8, then run again."
                       ) from None

    def blocks(self):
        text = self.read_agents_md()
        return [] if text is None else find_blocks(text.lstrip("\ufeff"))

    def recover(self, log):
        self.file.recover(log)
        for leftover in remove_leftover_temp_files(self.agents_md):
            log(f"deleted {leftover.name}, left by an interrupted run")

    def state(self, release):
        text = self.read_agents_md()
        file_state, version = self.file.state(release)
        blocks = [] if text is None else find_blocks(text.lstrip("\ufeff"))
        if not blocks and file_state == "absent":
            return "absent", None
        merged = merge_agents_md(text, release.files[CODEX_BLOCK_ASSET].decode("utf-8"))
        if file_state == "current" and merged == text:
            return "current", None
        if version is None and blocks:
            body = text.lstrip("\ufeff")
            version = banner_version(body[blocks[0][0]:blocks[0][1]].encode("utf-8"))
        return "different", version

    def install(self, release, log):
        text = self.read_agents_md()  # checked before anything is written
        merged = merge_agents_md(text, release.files[CODEX_BLOCK_ASSET].decode("utf-8"))
        before = self.file.path.read_bytes() if self.file.path.is_file() else None
        self.file.install(release, log)  # the file first: the block points at it
        if merged != text:
            try:
                write_atomic(self.agents_md, merged.encode("utf-8"))
            except BaseException:  # put the file back, so Codex is installed as it was
                self.file.put_back(before)
                raise
        line = unmarked_copy_line(merged.lstrip("\ufeff"))
        if line:
            log(f"warning: AGENTS.md line {line} starts an older copy of the skill without "
                "BEGIN and END lines; delete that copy by hand so Codex does not read it twice")
        body = merged.lstrip("\ufeff")
        block_end = len(body[:find_blocks(body)[0][1]].encode("utf-8"))
        if block_end > CODEX_DOC_LIMIT:
            log(f"warning: the {TITLE} block ends {block_end:,} bytes into AGENTS.md. By default "
                f"Codex reads only the first {CODEX_DOC_LIMIT // 1024} KiB of it "
                "(project_doc_max_bytes), so it may not see the block. Move the block nearer "
                "the top, or raise that setting.")

    def uninstall(self, log):
        text = self.read_agents_md()
        original = self.agents_md.read_bytes() if text is not None else None
        removed = False
        if text is not None and find_blocks(text.lstrip("\ufeff")):
            merged = merge_agents_md(text, None)  # the block first: it points at the file
            if merged.lstrip("\ufeff").strip() == "":
                unlink_or_fail(self.agents_md)
                removed = True
            else:
                write_atomic(self.agents_md, merged.encode("utf-8"))
        try:
            self.file.uninstall(log)
        except BaseException:  # the file stays, so its block goes back: Codex stays installed
            if original is not None:
                write_atomic(self.agents_md, original)
            raise
        if removed:
            log("deleted AGENTS.md, which held nothing else")

    def prune(self, project):
        self.file.prune(project)

    def preflight(self):
        check_writable(self.agents_md)
        self.file.preflight()

    def verify(self, release):
        text = self.read_agents_md()
        block = release.files[CODEX_BLOCK_ASSET].decode("utf-8")
        return (self.file.verify(release) and text is not None
                and merge_agents_md(text, block) == text)


# --- choosing what to do -----------------------------------------------------------------------

def same_path(a, b):
    """True if `a` and `b` are the same folder, through links, junctions and case."""
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def find_project(cwd, home):
    """The git repository around `cwd` (the nearest folder above it holding .git, below the home
    folder), else `cwd` itself."""
    for folder in [cwd, *cwd.parents]:
        if same_path(folder, home) or folder.parent == folder:
            break
        if (folder / ".git").exists():
            return folder
    return cwd


def signals(project):
    """{agent: [what was seen]} for the agents a project shows signs of using."""
    seen = {}
    for agent, names in (("claude-code", (".claude/", "CLAUDE.md")),
                         ("cursor", (".cursor/", ".cursorrules")),
                         ("codex", ("AGENTS.md", ".codex/"))):
        hits = [n for n in names if (project / n.rstrip("/")).exists()]
        if hits:
            seen[agent] = hits
    return seen


def no_question_message(seen):
    found = "; found " + ", ".join(h for hits in seen.values() for h in hits) if seen else ""
    return (f"Could not tell which coding agent this project uses{found}. Run again with "
            "--agent claude-code, --agent cursor or --agent codex (or several).")


def ask_agents(project, seen, ask, interactive):
    """One question: which agents. Returns a list of agent ids; exits 2 if it cannot ask."""
    choices = list(AGENTS)
    if not interactive:
        print(no_question_message(seen))
        raise SystemExit(2)
    print(f"Which coding agent should get {TITLE} in {project}?")
    for number, agent in enumerate(choices, 1):
        note = f"   (found {', '.join(seen[agent])})" if agent in seen else ""
        print(f"  {number}) {AGENTS[agent]}{note}")
    for attempt in range(3):
        try:
            answer = ask('Type a number, or several like "1 3", and press Enter: ')
        except EOFError:
            if attempt == 0:  # nobody to answer (Windows reports NUL as a terminal)
                print()
                print(no_question_message(seen))
                raise SystemExit(2) from None
            break
        picked = []
        for word in re.split(r"[\s,]+", answer.strip().lower()):
            if word.isdigit() and 1 <= int(word) <= len(choices):
                picked.append(choices[int(word) - 1])
            elif word in AGENTS:
                picked.append(word)
            elif word:
                picked = []
                break
        if picked:
            return list(dict.fromkeys(picked))
        print("Please type 1, 2 or 3.")
    print("No agent chosen; nothing was changed. To choose without a question, run again with "
          "--agent claude-code, --agent cursor or --agent codex.")
    raise SystemExit(2)


def describe_next_step(agent):
    if agent == "claude-code":
        return (f'start a new Claude Code session and ask: "{TRY_IT}" '
                f"(or type /{NAME} followed by your request)")
    if agent == "cursor":
        return f'open Cursor\'s Agent chat in this project and ask: "{TRY_IT}"'
    return f'start Codex in this project and ask: "{TRY_IT}"'


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="install-dependency-first-architect",
        description=f"Install {TITLE} for Claude Code, Cursor or Codex, in one project.",
        allow_abbrev=False)
    parser.add_argument("--agent", action="append", choices=list(AGENTS),
                        help="the agent to install for (repeatable); default: detect it, or ask")
    parser.add_argument("--project", type=Path,
                        help="the project folder (default: the git repository around the current "
                             "folder, else the current folder)")
    parser.add_argument("--global", dest="use_global", action="store_true",
                        help="Claude Code only: install for all your projects (~/.claude/skills)")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would change (also with --uninstall); change nothing")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify", action="store_true", help="check what is installed; change nothing")
    mode.add_argument("--uninstall", action="store_true",
                      help="remove what this installer manages, and nothing else")
    return parser.parse_args(argv)


def main(argv=None, cwd=None, home=None, ask=input, interactive=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    if sys.version_info < (3, 9):
        print("This installer needs Python 3.9 or newer.")
        return 1
    args = parse_args(argv)
    home = Path(os.path.abspath(home or Path.home()))
    cwd = Path(os.path.abspath(cwd or os.getcwd()))
    if interactive is None:
        interactive = sys.stdin is not None and sys.stdin.isatty()
    try:
        release = Release(VERSION, MANIFEST, PAYLOAD, RELEASED)
        return run(args, release, cwd, home, ask, interactive)
    except Stop as exc:
        print(f"Stopped: {exc}")
        print("Nothing was changed.")
        return 1


def report_partial(changed, what):
    if changed:
        print(f"  Already {what} in this run, and left so: "
              f"{', '.join(AGENTS[t.agent] for t in changed)}.")


def found_install(make_target, release, strict):
    """True if this installer's files for an agent are present. Something else at the same path
    is not an install of ours: it counts only when `strict` (uninstall and verify report it)."""
    try:
        return make_target().state(release)[0] != "absent"
    except Stop:
        return strict


def run(args, release, cwd, home, ask, interactive):
    if args.use_global:
        if args.agent and set(args.agent) != {"claude-code"}:
            print("--global is for Claude Code only: Cursor and Codex read the skill from the "
                  "project. Run again without --global for them.")
            return 2
        base, agents, project = home, ["claude-code"], None
    else:
        project = Path(os.path.abspath(args.project)) if args.project else find_project(cwd, home)
        if not project.is_dir():
            print(f"{project} is not a folder.")
            return 2
        if same_path(project, home) or project.parent == project:
            print(f"{project} is your {'home' if same_path(project, home) else 'top'} folder, not a "
                  "project. Open your project's folder and run the installer there (for Claude "
                  "Code in every project, use --global).")
            return 2
        base, agents = project, args.agent

    targets_for = {"claude-code": lambda: SkillTarget(base, "global" if args.use_global
                                                      else "project", release),
                   "cursor": lambda: CursorTarget(base),
                   "codex": lambda: CodexTarget(base)}
    where = "your home folder (all projects)" if args.use_global else str(project)
    print(f"{TITLE} {release.version}, in {where}")
    log = lambda message: print(f"    {message}")  # noqa: E731
    tidied = []

    def tidy_log(message):
        tidied.append(message)
        log(message)

    def stopped_here(reasons):
        for reason in reasons:
            print(f"Stopped: {reason}")
        print("Nothing else was changed (only what an earlier, interrupted run left behind was "
              "tidied up, as listed above)." if tidied else "Nothing was changed.")
        return 1

    if not (args.dry_run or args.verify):
        for agent in (["claude-code"] if args.use_global else AGENTS):
            try:
                target = targets_for[agent]()
            except Stop:
                continue  # a link where that agent's files go: reported if the agent is chosen
            try:  # what an interrupted or failed earlier run left behind
                target.recover(tidy_log)
            except (Stop, OSError) as exc:
                log(f"warning: {exc}")
    try:
        if not agents:
            strict = args.uninstall or args.verify
            installed = [a for a in AGENTS if found_install(targets_for[a], release, strict)]
            if strict or installed:
                agents = installed
            else:
                seen = signals(project)
                agents = (list(seen) if len(seen) == 1 else
                          ask_agents(project, seen, ask, interactive))
        targets = [targets_for[a]() for a in agents]
    except Stop as exc:
        return stopped_here([exc])
    if not targets:
        print(f"  Nothing from {TITLE} is installed here.")
        return 1 if args.verify else 0

    # Every check, for every chosen agent, before the first write.
    states, stopped = {}, {}
    for t in targets:
        try:
            states[t.agent] = t.state(release)
            if not args.verify and states[t.agent][0] != (
                    "absent" if args.uninstall else "current"):
                t.preflight()
        except Stop as exc:
            stopped[t.agent] = exc
    if args.verify:
        good = not stopped
        for t in targets:
            if t.agent in stopped:
                print(f"  {AGENTS[t.agent]}: cannot check: {stopped[t.agent]}")
                continue
            state, version = states[t.agent]
            if state == "current" and t.verify(release):
                print(f"  {AGENTS[t.agent]}: {release.version}, exactly as released ({t.label})")
            else:
                good = False
                what = ("not installed" if state == "absent" else
                        f"version {version}, or edited" if version else "a different version, "
                        "or edited")
                print(f"  {AGENTS[t.agent]}: {what} ({t.label})")
        return 0 if good else 1
    if stopped and not args.uninstall:
        return stopped_here([f"{AGENTS[a]}: {exc}" for a, exc in stopped.items()])

    changed = []
    for t in targets:
        name = AGENTS[t.agent]
        if t.agent in stopped:  # uninstall: left alone, and the others still removed
            print(f"  {name}: left alone: {stopped[t.agent]}")
            continue
        state, version = states[t.agent]
        if args.uninstall:
            if state == "absent":
                print(f"  {name}: not installed")
                continue
            if args.dry_run:
                print(f"  {name}: would remove {t.label}")
                continue
            try:
                t.uninstall(log)
                if project is not None:
                    t.prune(project)
            except (OSError, Stop) as exc:
                print(f"  {name}: FAILED to remove {t.label}: {exc}")
                print(f"  {name}: left installed. Close programs that may hold these files "
                      "open, then run again.")
                report_partial(changed, "removed")
                return 1
            print(f"  {name}: removed {t.label}")
            changed.append(t)
            continue
        if state == "current":
            print(f"  {name}: already {release.version}, nothing to change ({t.label})")
            continue
        action = ("install" if state == "absent" else
                  f"update from {version}" if version else "replace a different version")
        if args.dry_run:
            print(f"  {name}: would {action}: {t.label}")
            continue
        try:
            t.install(release, log)
        except (OSError, Stop) as exc:
            print(f"  {name}: FAILED to write {t.label}: {exc}")
            print(f"  {name}: left as it was before this run. Close programs that may hold "
                  "these files open, then run again.")
            report_partial(changed, "installed or updated")
            return 1
        if not t.verify(release):
            print(f"  {name}: FAILED: {t.label} does not match the release after writing it. "
                  "Run again; if it fails again, install by hand (README, Manual installation).")
            report_partial(changed, "installed or updated")
            return 1
        print(f"  {name}: {'installed' if state == 'absent' else 'updated'} {t.label} "
              "(checked against the release's SHA-256 sums)")
        changed.append(t)

    if args.dry_run:
        print("Dry run: nothing was changed.")
    elif args.uninstall:
        if stopped:
            print("Done, except for what was left alone above." if changed else
                  "Nothing was removed.")
            return 1
        print("Done." if changed else "Nothing to remove.")
    else:
        if "claude-code" in agents and not args.use_global:
            other = home / Path(*SKILL_DIR)
            if other.exists():
                print(f"  Note: {other} also holds the skill, for all projects, so Claude Code "
                      "may load both. Keep one: run this installer with --global --uninstall to "
                      "remove that copy (or with --global to update it).")
        print("Done. To try it:")
        for t in targets:
            print(f"  {AGENTS[t.agent]}: {describe_next_step(t.agent)}")
        if not args.use_global:
            print("These files are part of your project now: commit them to share the skill with "
                  "your team. Run this installer again with --uninstall to remove them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
