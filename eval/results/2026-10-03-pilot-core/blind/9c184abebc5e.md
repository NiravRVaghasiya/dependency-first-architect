# BUILD PLAN: `photoname`, a CLI that renames photos by their EXIF capture date

## 1. Classification
- **What:** A local, single-user command-line tool. It reads the capture date from each photo's metadata and renames files in place to `YYYY-MM-DD_HHMMSS[_NN].ext`.
- **Type:** Software (a local CLI). There is no infra tier and no AI component.
- **Dominant constraint:** Correctness, and specifically not losing data. Speed matters much less.
- **Worst failure:** Losing or orphaning someone's photos. That can happen if a rename overwrites a file when two photos share a timestamp, if a run is interrupted and leaves the folder half-renamed with no way back, or if sidecar files (`.xmp`, `.aae`, Live Photo `.mov`) get separated from their image. Renames change real files on disk, so every design decision below is about making them safe and undoable.

## 2. Tradeoff gates (resolved up front)

| Decision | Default (chosen now) | Flip condition |
|---|---|---|
| Consistency vs availability (here: all-or-nothing vs best-effort) | **Plan → validate → apply.** The tool builds the full rename plan first and aborts before touching anything if it finds any collision or unsafe target. During apply, every rename is journaled, so a partial run can always be undone. | Users with very large folders containing a few unreadable files ask for partial progress. Then add an opt-in `--continue-on-error` that skips bad units but keeps journaling. |
| Monolith vs services | **Single CLI process**, with an internal `core` package (planner, validator, renamer) kept separate from the `cli` layer. | Another front end (GUI, Lightroom plugin) needs the logic. Then publish `core` as a library. A service split never applies. |
| Sync vs async | **Synchronous and serial.** Metadata is read in one batch call; renames run one at a time. | Planning more than 10k files takes over 30s and metadata reading is the bottleneck. Then read metadata in parallel batches. **Apply stays serial regardless**, because ordering and journaling depend on it. |
| Build vs buy (metadata parsing) | **Buy: ExifTool** via `exiftool -json -n`, behind a `MetadataReader` interface. It handles JPEG, HEIC, RAW (CR2/NEF/ARW/DNG) and MOV/MP4, plus the vendor quirks, better than any library I could write. | A zero-dependency install becomes a hard requirement. Then add a pure-Python backend (Pillow + pillow-heif) that covers only JPEG and HEIC. |
| Language / distribution | **Python 3.12, locked with `uv`, installed via `pipx` / `uv tool install`.** | Users can't or won't install Python. Then ship a PyInstaller single binary, or rewrite the thin CLI in Go against the same contract tests. |
| **Filename contract** (the most expensive decision to reverse) | `YYYY-MM-DD_HHMMSS.ext`. The original extension is kept exactly, case included. Collisions get `_01`, `_02`… in a fixed order: sub-second time, then original name. | Users need a different pattern. Then add `--format` templates, but only after the default is frozen in v1 (see Deferred). Once people's libraries are renamed, changing the default breaks their workflows. |
| Timezone semantics | **Use the wall-clock time exactly as recorded** (EXIF `DateTimeOriginal` has no timezone). Never convert to the host's local time. | Users merge photos from several cameras across timezones. Then add opt-in `--normalize-utc` using `OffsetTimeOriginal`, or `--shift`. |
| Missing-date fallback | **Skip the file and report why.** Never fall back to file mtime silently, because copying files resets mtime. | Users ask for it. Then add opt-in `--fallback=mtime`, and record the date source in the journal. |
| What gets renamed | **A unit = the primary image plus any same-stem siblings** (`.xmp`, `.aae`, Live Photo `.mov`), renamed together. | None. This is the planner's data model and has to be right from Phase 1. |
| Data privacy boundary | **Nothing leaves the machine.** No network calls and no telemetry. Logs stay local. | None. Remote telemetry, if ever added, must be opt-in. |
| (AI) prompt+RAG vs fine-tune | N/A: there is no model in this system. | — |
| (AI) hosted API vs self-host | N/A: there is no model in this system. | — |

## 3. Walking skeleton (Phase 0)

For a CLI, "production" means a released artifact installed on a clean machine and run against a real folder.

- **The one real request:** `photoname ./fixtures/single --apply` on a folder holding one real JPEG (`IMG_0001.JPG`, `DateTimeOriginal = 2024:07:14 15:30:12`).
- **The real response:** the file becomes `2024-07-14_153012.JPG`, a journal file `.photoname/journal-<run-id>.jsonl` is written, a summary line `scanned=1 renamed=1 skipped=0 errors=0` is printed, and the exit code is 0.
- **Tiers it crosses** (each as thin as possible, but all present):
  1. CLI argument parsing (`argparse`). **Dry run is the default**; `--apply` is required to change anything.
  2. Scanner: lists files, sorted, no recursion, no symlink following.
  3. `MetadataReader` (ExifTool): one `-json` subprocess call.
  4. Planner: date → target name.
  5. Validator: target doesn't exist and isn't duplicated in the plan.
  6. Renamer: journal line written and fsynced, then a no-clobber rename.
  7. Reporter: summary line and exit code.
  8. `photoname undo <journal>` reverses the run. Undo is in the skeleton because it is what makes `--apply` safe to try.
- **Deployed:** GitHub Actions builds the wheel and installs it with `pipx` on clean Windows, macOS and Ubuntu runners, pinned to ExifTool 12.x. Each runner runs the request above, then `undo`, then checks the folder's hash manifest against the original.
- **Logged:** each run gets a `run_id`. There is one structured decision record per file (`path, date_source, target, action, reason`). Output is human-readable on stderr, or JSON lines with `--log-json`.
- **Monitored:** there is no field telemetry (privacy gate), so monitoring has three parts:
  - documented exit codes: 0 ok, 1 completed with skips, 2 usage error, 3 environment error such as ExifTool missing, 4 validation aborted with nothing changed, 5 apply failed partway with journal written and undo available;
  - the summary counters as the metric;
  - the CI smoke job on 3 operating systems as the uptime check.

**Exit check:** on all three OS runners, a pipx-installed wheel renames the real fixture, prints the summary metric, writes the per-file trace and journal, and `undo` restores a byte-identical tree.

## 4. Phases (dependency order; widest blast radius first within each)

**Phase 0: Walking skeleton** (see §3)
- Unlocks: everything else. Each later phase thickens one of these tiers.
- Depends on: nothing.

**Phase 1: Safety substrate (data model, transactional apply, undo)**
- Unlocks: running the tool on real photo libraries without risking data. Phase 2 needs this, because testing date logic on real libraries is only safe once apply and undo are solid.
- Depends on: Phase 0's proven path through all tiers, plus the CI matrix.
- Tasks, widest blast radius first:
  1. **Freeze the rename-unit data model:** primary file plus same-stem siblings. Every later component reads it.
  2. **Freeze the filename contract and collision-suffix rules,** with golden tests.
  3. **Make apply safe:**
     - Apply in two steps: every unit first gets a unique temporary name (`.photoname-tmp-<run>-<n>`), then its final name. This handles swaps and cycles (A→B while B→A).
     - Make the rename itself no-clobber on every OS. Windows `os.rename` already refuses to overwrite. On Linux use `renameat2(RENAME_NOREPLACE)`, on macOS `renamex_np(RENAME_EXCL)`, and elsewhere fall back to `link` + `unlink`.
  4. **Validator:**
     - detect duplicate targets within the plan;
     - detect targets that already exist on disk;
     - detect **case-insensitive collisions** (NTFS, APFS, SMB shares);
     - reject any target that would resolve outside the source directory.
  5. **Journal and undo:** write and fsync the journal before each step. `undo` works after Ctrl-C or `kill -9`.
  6. **Idempotency:** a file that already has its correct name, including an `_NN` suffix it owns, is a no-op on re-run.
- Exit check:
  - A property-based test (Hypothesis) generates random folders with duplicate timestamps, case clashes, pre-existing targets and swap cycles. For each, apply then undo must give a byte-identical hash manifest, and no file may ever be lost.
  - A fault-injection test kills the process at a random step. Undo must restore the folder.
  - A second `--apply` run reports `renamed=0`.

**Phase 2: Date-extraction correctness**
- Unlocks: trustworthy names across real cameras and phones.
- Depends on: Phase 1 (safe apply and undo), so the tool can be tested on real libraries.
- Tasks, widest first:
  1. **Tag precedence:** `DateTimeOriginal` → `CreateDate` / `DateTimeDigitized` → QuickTime `CreateDate` for video. QuickTime stores that value in UTC, which is a known quirk; convert it using the paired image or the offset tag. If none of these exist, skip with reason `no_capture_date`.
  2. **Strict date validation:** regex plus a real calendar check. Placeholder values like `0000:00:00 00:00:00`, empty strings and garbage count as missing.
  3. **Live Photo pairing:** the unit's date comes from the primary image, and the `.mov` follows it.
  4. **Burst ordering:** use `SubSecTimeOriginal` so collision suffixes follow shooting order.
  5. **Leaf work:** a per-format quirks table (RAW+JPEG pairs from one shot share a stem, so they're kept as one unit).
- Exit check: a versioned golden corpus of about 40 real files (iPhone HEIC + Live Photo, Android JPEG, Canon CR2, Nikon NEF, Sony ARW, DNG, MP4, a stripped-EXIF file, a corrupt file) matches the expected names 100%. The suite runs under `TZ=UTC` and `TZ=Asia/Kolkata` with identical results.

**Phase 3: Scale and usability**
- Unlocks: large libraries and scripting.
- Depends on: Phase 2's correct per-file behaviour. Scaling a wrong answer just spreads it faster.
- Tasks, widest first:
  1. **Machine-readable plan output:** `--json`, and `--plan-out` / `--plan-in` so users can review a plan before applying it. This is a public contract other scripts will depend on.
  2. **`--recursive`,** with each directory's renames kept inside that directory.
  3. **Batched ExifTool reads** (`-stay_open` or chunked `-json`) with a timeout per batch.
  4. Progress bar and stage timings.
  5. Shell completions.
- Exit check: a seeded synthetic corpus of 10k files plans in under 30s and applies in under 60s on the CI runner. The `--json` schema is validated against a committed JSON Schema.

**Phase 4: Harden what proved stable, then release v1.0**
- Unlocks: public distribution with stability promises.
- Depends on: Phases 1–3 being stable for at least one pre-release cycle (0.x) with no contract changes.
- Tasks, widest first:
  1. Freeze the CLI flags, exit codes, journal format and `--json` schema under semver.
  2. Backward compatibility: v1 can read and undo v0.x journals.
  3. PyPI trusted publishing with signed artifacts.
  4. Documentation and a man page.
- Exit check: a tagged release installs from PyPI on all three OSes, and the full suite passes against the published artifact, including undoing a 0.x journal.

## 5. Cross-cutting concerns (per phase, from the first commit)

| Phase | Security | Observability | Reproducibility | Resilience |
|---|---|---|---|---|
| 0 | ExifTool is called with an argument list (no `shell=True`), and `--` comes before file paths, so a file named `-delete...` can't inject options. No network access. Symlinks are not followed. | `run_id`, a per-file decision trace, summary counters, documented exit codes, CI smoke run on 3 OSes. | `uv.lock` with hashes, pinned Python 3.12, ExifTool version pinned in CI, fixtures committed with Git LFS. | Dry run by default. Missing ExifTool gives exit 3 with an install hint. Any error before apply leaves the folder untouched. Undo exists from day zero. |
| 1 | Target names are built **only from validated date digits**, never from metadata strings, so path traversal can't happen. Any target outside the source directory is rejected. | The journal doubles as an audit trail. Validation failures name the colliding files. `--verbose` prints the full plan. | The plan is deterministic: sorted traversal, never raw `listdir` order. The same input always gives the same plan. | No-clobber renames, temp-name two-step, fsynced journal, undo after a crash, idempotent re-runs, clean Ctrl-C handling. |
| 2 | Only a strictly regex-validated date value is taken from files, which may be crafted. Parsing is left to ExifTool rather than custom code. | Each decision records `date_source` (which tag it came from). The summary includes a histogram of skip reasons. | Golden corpus is versioned. Tests run under two timezones to catch any leak of host local time. | Corrupt or unreadable files are skipped and reported, never crash the run. Bad dates count as missing. |
| 3 | Timeout per ExifTool batch and a cap on files per run, so a hostile or huge directory can't hang the tool. | Stage timings (scan/read/plan/apply), a progress bar, and a JSON report for scripts. | Benchmark corpus comes from a seeded generator, so performance numbers are comparable across commits. | A crashed batch is retried one file at a time. Read-only and permission-denied files are skipped with a reason. Case-insensitivity tests run on SMB/NTFS. |
| 4 | Signed releases via PyPI trusted publishing (no long-lived tokens), an SBOM, and a dependency audit (`pip-audit`) in CI. | `--version` reports the ExifTool version. `photoname doctor` checks the environment. `--debug-report` writes a bundle with paths redacted. | Reproducible wheels (`SOURCE_DATE_EPOCH`) built from the lockfile only. | Journal format is versioned, and v1 can undo v0.x journals, so upgrades never strand an undo. |

## 6. AI layer
N/A. The system has no model, prompts or agent. (Omitted per the template.)

## 7. Deliberately deferred
- **`--format` naming templates.** Pull forward when users ask for other patterns, and only after the default contract is frozen in v1.
- **Sorting into `YYYY/MM/` folders.** Pull forward on demand. It changes undo semantics (moves across directories, possibly across filesystems), so it gets its own validation phase.
- **Timezone normalization / `--shift`.** Pull forward when someone has multi-camera or travel libraries where the order comes out wrong.
- **`--fallback=mtime`.** Pull forward on request. It stays opt-in and the journal records the date source.
- **Pure-Python metadata backend.** Pull forward if a no-ExifTool install becomes a requirement.
- **Parallel metadata reads.** Pull forward if the 10k-file planning budget is missed.
- **Single-binary distribution.** Pull forward if pipx/uv installation is a measurable obstacle for users.
- **Deduplication, writing EXIF data, watch mode, GUI.** Pull forward only if users explicitly ask. Each is a separate product surface.
- **Field telemetry.** Not planned. If it's ever needed for crash rates, it must be opt-in, and the privacy gate is reopened first.
