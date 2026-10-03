"""Run-directory layout and record I/O: the contract between every stage of the harness.

A run directory is self-contained. Every stage reads what earlier stages wrote here and nothing
else, so a committed run can be re-aggregated, re-reported and audited without any provider.

    <run>/manifest.json                 run-level provenance (config copy, skill hashes, versions)
    <run>/generations/<gen_id>.json     one record per generated plan (metadata, usage, cost)
    <run>/generations/<gen_id>.md       the plan text exactly as the provider returned it
    <run>/blind/<blind_id>.md           what judges saw (blinded copy)
    <run>/blind/key.json                blind_id -> gen_id (the sealed key; judges never see it)
    <run>/judgments/<judgment_id>.json  one record per judge call (parsed scores + validation)
    <run>/probes/<probe_id>.json        one record per leakage-probe call
    <run>/lint/<gen_id>.json            deterministic plan-structure checks
    <run>/raw/<call_id>.txt             raw provider stdout for every call, for audit
    <run>/rubrics/<rubric_id>.json      the rubrics the run was judged with (aggregate reads these)
    <run>/superseded/...                records (and raw output) a retry replaced, numbered; kept
                                        for audit and cost, never used in a metric (generate.py)
    <run>/summary.json                  aggregate statistics (generated)
    <run>/REPORT.md                     human-readable report (generated from summary.json)

An outcome-benchmark run adds outcomes/ and outcomes-summary.json / OUTCOMES.md (outcomes.py).

All JSON is UTF-8, LF line endings, two-space indent, keys in insertion order, trailing newline,
so re-running a deterministic stage yields byte-identical files on every OS.
"""

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

MANIFEST = "manifest.json"
GENERATIONS = "generations"
BLIND = "blind"
BLIND_KEY = "key.json"
JUDGMENTS = "judgments"
PROBES = "probes"
LINT = "lint"
RAW = "raw"
SUMMARY = "summary.json"
REPORT = "REPORT.md"

# Every id becomes a file name, so ids are restricted to a portable character set.
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def check_id(value, what="id"):
    # fullmatch: `$` alone would also accept a trailing newline.
    if not isinstance(value, str) or not ID_RE.fullmatch(value) or ".." in value:
        raise ValueError(f"invalid {what}: {value!r} (allowed: letters, digits, '.', '_', '-')")
    return value


def gen_id(prompt_id, condition, generator_id, run_index):
    """`P1.dfa.opus-5.5.r01`. The id names the condition; judges never see ids or file names."""
    return check_id(f"{prompt_id}.{condition}.{generator_id}.r{run_index:02d}", "gen_id")


def judgment_id(blind_id, rubric_id, judge_id, repeat):
    return check_id(f"{blind_id}.{rubric_id}.{judge_id}.k{repeat}", "judgment_id")


def probe_id(blind_id, judge_id):
    return check_id(f"{blind_id}.probe.{judge_id}", "probe_id")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_text(text):
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path):
    return sha256_bytes(Path(path).read_bytes())


def dumps(data):
    return json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def _atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path, data):
    """Write JSON deterministically (LF, UTF-8, no NaN) and atomically."""
    _atomic_write(path, dumps(data).encode("utf-8"))


def write_text(path, text):
    """Write text as UTF-8 with LF line endings, atomically."""
    _atomic_write(path, text.replace("\r\n", "\n").encode("utf-8"))


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_text(path):
    return Path(path).read_text(encoding="utf-8")


class RunDir:
    """Paths and loaders for one run directory."""

    def __init__(self, root):
        self.root = Path(root)

    # --- paths -----------------------------------------------------------------------------
    @property
    def manifest_path(self):
        return self.root / MANIFEST

    def generation_json(self, gid):
        return self.root / GENERATIONS / f"{check_id(gid, 'gen_id')}.json"

    def generation_text(self, gid):
        return self.root / GENERATIONS / f"{check_id(gid, 'gen_id')}.md"

    def blind_text(self, blind_id):
        return self.root / BLIND / f"{check_id(blind_id, 'blind_id')}.md"

    @property
    def blind_key_path(self):
        return self.root / BLIND / BLIND_KEY

    def judgment_path(self, jid):
        return self.root / JUDGMENTS / f"{check_id(jid, 'judgment_id')}.json"

    def probe_path(self, pid):
        return self.root / PROBES / f"{check_id(pid, 'probe_id')}.json"

    def lint_path(self, gid):
        return self.root / LINT / f"{check_id(gid, 'gen_id')}.json"

    def raw_path(self, call_id):
        return self.root / RAW / f"{check_id(call_id, 'call_id')}.txt"

    @property
    def summary_path(self):
        return self.root / SUMMARY

    @property
    def report_path(self):
        return self.root / REPORT

    def relative(self, path):
        """Path relative to the run directory, POSIX style (what records store)."""
        return Path(path).resolve().relative_to(self.root.resolve()).as_posix()

    # --- loaders ---------------------------------------------------------------------------
    def manifest(self):
        return read_json(self.manifest_path)

    def _records(self, sub):
        folder = self.root / sub
        if not folder.is_dir():
            return []
        return [read_json(p) for p in sorted(folder.glob("*.json")) if p.name != BLIND_KEY]

    def generations(self):
        """Generation records sorted by gen_id."""
        return self._records(GENERATIONS)

    def judgments(self):
        return self._records(JUDGMENTS)

    def probes(self):
        return self._records(PROBES)

    def lints(self):
        return self._records(LINT)

    def blind_key(self):
        return read_json(self.blind_key_path) if self.blind_key_path.exists() else None
