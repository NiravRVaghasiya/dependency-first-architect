## BUILD PLAN: photo renaming CLI

A plan for a command-line tool that renames a folder of photos by their EXIF capture date.

### Classification and constraints

- **What:** a CLI that renames every photo in a folder to its EXIF capture date, with an undo.
- **Type:** software; greenfield; a small build (one maintainer, no outside service, no money).
- **Dominant constraint:** correctness.
- **Worst failure:** a run renames thousands of files wrongly and the original names are lost.

The budgets that bind for a small local tool:

| Budget | Label | Target | Checked by |
|---|---|---|---|
| Latency | ASSUMPTION: a laptop SSD; confirm on the maintainer's machine | 1,000 photos renamed in under 10 s | Phase 1 exit check |
| Throughput | UNKNOWN: needs the largest folder users expect (the maintainer, before Phase 2) | – | Phase 2 |
| Resource use | REQUIREMENT: runs on an 8 GB laptop, stated in the request | Peak memory under 200 MB | [V1](#validation-gates) |

**Missing inputs:** the largest folder size; it changes the batch size, not the phase order.

### Dependency map

| What waits | Depends on | Kind | Blocked from being |
|---|---|---|---|
| Write mode on real folders | The undo journal restores every name (V1) | Validation | exposed |
| The first public release | The maintainer's release sign-off (V2) | Organizational | committed |

No decision, risk/security or economic dependency beyond those rows.

### Trade-off gates

| Decision | Reversibility | Default (chosen now) | Assumption | Validated by | Flip condition |
|---|---|---|---|---|---|
| Write path on real folders | R3 (a wrong rename across thousands of files cannot be undone without the journal) | Dry run by default; every write journaled before it happens | Users keep the journal file | V1 | V1 fails, or users delete journals |
| Collision policy | R2: changing it later renames files differently | Append a counter (`_2`, `_3`) | Two photos rarely share a second | A cheap check on a burst-mode folder | Bursts collide more than 1% of the time |

**R1 defaults:** argument parser → argparse; output format → one line per file.

### Walking skeleton (Phase 0)
- The released package, installed as users install it, renames a real folder in dry-run mode.
- Monitored by its own output: a summary line and a non-zero exit code on any error.

Exit check (V0): the installed package runs on a real folder and prints the planned renames; a broken release is yanked and the previous one reinstalled.

### Phases

#### Phase 1 — Write mode with the undo journal
- Unlocks: write mode for users.
- Depends on: V0.
- Tasks: the journal format; write mode; the undo command.
- Rollback: undo from the journal.
- Exit check: V1 passes.

#### Phase 2 — Public release
- Unlocks: users outside the maintainer.
- Depends on: Phase 1 and V2.
- Tasks: signed release; install docs.
- Rollback: yank the release.
- Exit check: V2 passes.

### Validation gates

| ID | Hypothesis | Method | Acceptance threshold | Evidence | Unlocks | Phase |
|---|---|---|---|---|---|---|
| V0 | The released package runs on a real folder | Install from the package index and run in dry-run mode | The V0 exit check | CI install log | Phase 1 | 0 |
| V1 | The undo journal restores every original name | Rename then undo three real folders (500, 5,000 and 20,000 photos) | 100% of names restored (REQUIREMENT: no data loss, stated in the request) | Undo test report in CI | Write mode; if it fails: keep dry run only | 1 |
| V2 | The release is ready for users | Release checklist review | The maintainer signs off on the checklist | Signed checklist in the repository | Public release; if it fails: fix and re-review | 2 |

### Cross-cutting concerns

| Phase | Security | Observability | Reproducibility | Resilience |
|---|---|---|---|---|
| Phase 0 (skeleton) | Release signed in CI | Summary line and exit code | Lock file; reproducible build | Yank and reinstall |
| Phase 1 | Never follows symlinks out of the folder | Per-file log line in the journal | Journal format versioned | Undo from the journal |
| Phase 2 | Signature checked by the installer docs | Error report template | Release built from a tag | Previous release kept installable |

### AI layer

N/A — no AI component.

### Methodology exceptions

| ID | Rule bypassed | Why it does not apply | Replacement validation | Evidence required | Resumes when |
|---|---|---|---|---|---|
| E1 | Dependency order (principle 1) | A partner contract fixes the release date before the EXIF library's stable API ships | Build against the beta API behind an adapter with contract tests; the date-parsing risk stays open | Contract tests pass against the stable API | The library's stable release ships and the adapter is re-checked |

### Deliberately deferred
1. Video files: pulled forward when a user asks for them twice.
2. A GUI: pulled forward when 100 users ask for it.
