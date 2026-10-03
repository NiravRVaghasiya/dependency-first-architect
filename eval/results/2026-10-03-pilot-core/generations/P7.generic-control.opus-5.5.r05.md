# Architecture and Build Plan: `exifrename`, a CLI that renames photos by EXIF capture date

## 1. Summary

- **What:** A command-line tool that renames the photos in a folder to their capture date and time, e.g. `IMG_4821.HEIC` → `2024-07-14_18-03-22.HEIC`. It's for one person tidying their own photo library. *(Assumption: personal or small-team use, not a product.)*
- **Shape:** One installable Python package with seven internal modules: scanner → metadata reader → planner → executor/journal, with the CLI and the report on top. There's no service and no database.
- **Key decisions:**
  1. **Preview by default.** The tool only renames when you pass `--apply`.
  2. **Plan first, then apply.** The full rename plan is computed and checked before any file is touched.
  3. **Safe renaming with a log.** Files are renamed in two passes (first to unique temporary names, then to final names). Each step is written to a log file *before* it happens (a "write-ahead journal"). This makes every run reversible with `exifrename undo` and lets a failed run roll itself back.
  4. **EXIF reading sits behind one interface.** It starts with the pure-Python `exifread` library. ExifTool can be plugged in as a second backend if the coverage spike shows gaps.
  5. **Files without a trustworthy date are skipped and reported, never guessed.**
- **First milestone (about 3–5 dev-days):** JPEG files work end to end (preview, apply, undo) with burst-collision handling, CI on Windows, macOS and Linux, and two spikes: metadata format coverage and filesystem rename behaviour.
- **Top risks:** losing or overwriting a file (the only truly expensive failure), formats the reader can't parse (HEIC, Canon CR3), and companion files such as `.xmp` and Live Photo `.mov` getting separated from their photos.

## 2. Context and Goals

- **Problem:** Photos from phones and cameras have names like `IMG_4821` or `DSC_0042`. When several devices are combined in one folder, they don't sort in time order. Renaming by hand is slow and easy to get wrong. The existing option is an ExifTool one-liner (`exiftool '-FileName<DateTimeOriginal' -d %Y-%m-%d_%H-%M-%S%%-c.%%e DIR`). It works but has no preview, no undo, and is hard to remember. **The reason to build this tool is safety and ease of use, not capability.** If you're happy with that one-liner, it may be enough (see Open Questions).
- **Goals:**
  - Rename every photo that has a capture date in one command.
  - Show a preview first.
  - Never lose or overwrite a file.
  - Make any run fully reversible.
- **Non-goals:**
  - Changing file contents or metadata.
  - Converting time zones or correcting a wrong camera clock (deferred).
  - Recursing into subfolders (deferred).
  - Renaming standalone videos.
  - A GUI.
  - Detecting duplicates.
- **Success measures:**
  - Zero files lost or overwritten, in testing and in use.
  - At least 99% of files in the user's real library receive a date-based name.
  - Applying and then undoing a run returns the folder to its exact original names.

## 3. Drivers, Requirements, and Constraints

**Ranked quality attributes**

| # | Attribute | Measurable target | Why it matters |
|---|---|---|---|
| 1 | Data safety | 0 files lost or overwritten. Every applied run can be undone to identical names and content hashes. A failure at any step leaves the folder fully original or fully renamed. | Photos can't be replaced. One overwrite costs more than every other feature is worth. |
| 2 | Date correctness | 100% of fixture files get the name their `DateTimeOriginal` implies. 0 files get a silently guessed date. | A wrong name is worse than no rename, because it misleads later. |
| 3 | Format coverage | JPEG and HEIC, plus CR2, NEF, ARW and DNG RAW files, are read correctly. At least 99% of the user's library gets a date. *(Assumption)* | It decides which metadata backend to use, which is the main technical uncertainty. |
| 4 | Easy to install, cross-platform | One command (`pipx install exifrename` or `uv tool install`) on Windows, macOS and Linux. | The user is evidently on Windows. Windows filesystem behaviour differs from POSIX. |
| 5 | Speed | Preview of 10,000 local JPEGs in under 60 s on an SSD. *(Assumption)* | Users with big folders will cancel a slow run, but this is easy to meet. |

**Key functional requirements**

- `exifrename PATH` previews the renames. `--apply` performs them. `exifrename undo PATH` reverses the last run.
- Default name format: `YYYY-MM-DD_HH-MM-SS.ext`. There are no colons, because Windows forbids them in filenames.
- When several photos share the same second (burst shots), they get `_01`, `_02`, … in a fixed order: by subsecond if present, then by current filename.
- Running the tool twice in a row does nothing the second time.

**Constraints**

- No existing code (see §4).
- Assumed: one developer who knows Python and works part-time.
- Assumed: the code is hosted on GitHub and released on PyPI.
- No budget is needed.

**Hard parts**

1. **Rename safety.** Two things make this hard:
   - Target names can clash with other files' current names, including clashes that go in a circle.
   - On POSIX, `os.rename` **silently overwrites** an existing destination. On Windows it raises an error. The two systems behave differently, so the code can't count on either one.
2. **Metadata coverage.** HEIC and CR3 use ISO-BMFF containers (the MP4-style box format), which many EXIF libraries handle poorly. Some files have no `DateTimeOriginal`, have zero dates (`0000:00:00 00:00:00`), or have malformed values.
3. **Date meaning.** There are several date tags:
   - `DateTimeOriginal`: when the shot was taken.
   - `DateTimeDigitized`: usually the same as above.
   - `DateTime`: rewritten whenever an editor saves the file, so it's untrustworthy.

   Separately, offsets (`OffsetTimeOriginal`) are often missing.
4. **Companion files.** Lightroom/darktable `.xmp` files, iOS `.AAE` edit files and Live Photo `.MOV` files are linked to their photo by sharing its filename stem. Renaming only the photo breaks that link.

## 4. Current State

The workspace is new: `README.md` says the directory is intentionally empty, and there's no `pyproject.toml` or `package.json`. Since this is a new build, the plan sets its own conventions:

- `src/` layout
- `pyproject.toml` with hatchling
- Python ≥ 3.11
- pytest and ruff
- GitHub Actions

There's no organisational platform to fit into.

## 5. Assumptions and Open Questions

**Assumptions**

| Assumption | Impact if wrong | How and when validated |
|---|---|---|
| It's for one user (or a few), run by hand on local or USB folders. | If it's a shared tool or for NAS libraries, network-drive speed and permissions matter more. | User confirms before M1 starts. |
| Python is acceptable and the developer knows it. | If a single binary is required, switch to Go (D1). | User confirms before M1 starts. |
| The library contains JPEG, HEIC and TIFF-based RAW files. CR3 is rare. | If CR3 or HEIC is common and `exifread` misses it, ExifTool becomes the default (D2). | Spike A in M1, using a sample of the user's library. |
| Recorded local wall-clock time is the name the user wants. | If they need UTC or trip-local time, they need offset handling. | User confirms. Revisit in M2. |
| Users accept a `.exifrename/` folder created inside the photo folder for the journal. | Clutter. Fails on read-only media (but renaming would fail there too). | User feedback in M3. A `--journal-dir` option is the escape hatch. |
| Only one process modifies the folder during a run. | A check-then-rename race could overwrite a file (see §11 Security/Reliability). | Documented. Accepted for a single-user tool. |

**Open questions** (also at the end of this reply)

| Question | Who answers | Default if no answer | Needed by |
|---|---|---|---|
| Which cameras and formats are really in the library? Is CR3 present? | User | JPEG, HEIC and TIFF-based RAW | Start of M1 (feeds Spike A) |
| Is installing ExifTool acceptable? | User | No. Pure Python by default, ExifTool optional. | End of M1 |
| Should companion files (`.xmp`, `.aae`, Live Photo `.mov`) move with their photo? | User | Yes, same-stem companions move together | Start of M2 |
| What to do with files that have no EXIF date? | User | Skip and report. `--fallback mtime` is opt-in. | Start of M2 |
| Is the naming format acceptable? | User | `YYYY-MM-DD_HH-MM-SS[_NN].ext`, keeping the original extension's case | Before M3 release (renamed libraries lock it in) |

## 6. Architecture Overview

```
                 ┌──────────────────────── exifrename (one Python package) ───────────────────────┐
 user ──argv──▶  │ cli ──▶ scanner ──▶ metadata (MetadataReader) ──▶ planner ──▶ report (stdout)  │
                 │                         │  ExifreadReader (default)      │                       │
                 │                         │  ExiftoolReader (optional) ──┐ │ --apply               │
                 │                                                      │ ▼                       │
                 │                                       executor ◀──▶ journal                    │
                 └──────────────────────────────────────────────────────┼──────────────────────────┘
   trust boundary: file contents are untrusted input                    │ subprocess (optional)
   ─────────────────────────────────────────────────                    ▼
   photo folder:  IMG_*.jpg … + .exifrename/journal-*.jsonl        exiftool binary
```

How the parts fit together:

1. The scanner lists candidate files.
2. The metadata reader extracts a `CaptureTime` for each file, or a reason it couldn't.
3. The planner, which is a **pure function** with no I/O, turns those into a `RenamePlan`. Every safety rule lives here: collisions, no-op renames, skips.
4. The report prints the plan.
5. Only with `--apply` does the executor touch the filesystem. It records each step in the journal before doing it.

| Component | Responsibility | Owns data | Exposes | Technology | Depends on |
|---|---|---|---|---|---|
| `cli` | Parse arguments, wire modules together, set exit codes | none | `exifrename PATH [--apply] [-v]`, `exifrename undo PATH` | argparse (stdlib) | all |
| `scanner` | List photo files in one folder | none | `scan(folder) -> list[Path]` | `os.scandir` | — |
| `metadata` | Extract capture time from file bytes | `CaptureTime` | `MetadataReader.read(path) -> ReadResult` | `exifread`; ExifTool optional | file contents |
| `planner` | Compute target names, collisions, skips | `RenamePlan` | `build_plan(...) -> RenamePlan` | pure Python | — |
| `executor` | Apply or undo a plan safely | file names on disk | `apply(plan)`, `undo(journal)` | `os.rename`, `os.fsync` | `journal` |
| `journal` | Durable record of every rename step | `journal-*.jsonl` | `Journal.append(op)`, `Journal.load(path)` | JSON Lines | — |
| `report` | Human-readable plan and summary | none | `print_plan`, `print_summary` | stdlib | — |

## 7. Component Details

**metadata**
- **Contract:** `ReadResult = CaptureTime | Skip(reason)`. `CaptureTime` holds:
  - `naive_dt`: the local time as recorded
  - `subsec: str | None`
  - `offset: str | None`
  - `source_tag`: which tag the date came from
- **Tag precedence:** `DateTimeOriginal` → `DateTimeDigitized`. `DateTime` (IFD0) is **never** used, because editors rewrite it.
- **Rejected values:** zero dates, unparseable strings, and years outside 1990–2100. These come back as `Skip`.
- **Failures:** any parser exception on a file becomes `Skip("unreadable: <ExceptionType>")`. A bad file never aborts the scan.
- **Not its job:** deciding names.
- **ExifTool backend (if adopted):** one batched `exiftool -j -n -DateTimeOriginal -CreateDate -SubSecTimeOriginal -OffsetTimeOriginal <files>` call, with a timeout. It does not start a new process per file.

**planner**
- **Inputs:** `(path, ReadResult)` pairs, plus the set of all names currently in the folder.
- **Output:** a `RenamePlan` containing `ops: list[RenameOp(src, dst)]`, `noops` and `skips`.
- **Rules:**
  - Names are compared case-insensitively (`casefold`) on every OS, because NTFS, APFS and exFAT are case-insensitive.
  - A target can't equal any existing file that isn't itself being renamed.
  - Burst suffixes are assigned in sorted order of `(subsec, current_name)`. This keeps the result stable across runs.
  - A file already at its target name is a no-op.
- **Not its job:** any I/O. Because it's pure, property tests can check it thoroughly.

**executor and journal**
- **Apply sequence:**
  1. Recheck that no destination exists outside the plan.
  2. Write the journal header.
  3. Phase 1: rename each `src` to `.exr-<uuid8>-<src>`.
  4. Phase 2: rename each temp name to `dst`.
  5. Write `commit`.

  Every journal line is flushed and `fsync`ed **before** its rename happens.
- **Why two phases:** they remove any ordering problem. This includes circular cases, such as a file whose target is another file's current name. The cost is one extra rename per file.
- **On any exception:** reverse the completed steps in reverse order, then write `rolled_back`. If the rollback itself fails, stop, keep the journal, and print the `undo` command to run.
- **Undo:** read the journal and move each file from `dst` (or from its temp name) back to `src`. Only move it if `src` is free. Report anything that has changed since the run.

**scanner, cli, report**
- **scanner:** routine.
  - Doesn't recurse.
  - Filters by a case-insensitive extension list.
  - Skips hidden files, `.exr-*` temp files, `.exifrename/` and symlinks.
- **cli and report:** routine. Exit codes are:
  - 0: success
  - 1: some files skipped or failed (with `--strict`)
  - 2: usage error
  - 3: apply failed and was rolled back

## 8. Data Design

- **Entities:** `CaptureTime` (in memory), `RenameOp` and `RenamePlan` (in memory), and the journal (on disk). Filenames on disk are the real data. Each run's journal is the record of what that run changed.
- **Journal format (JSON Lines, versioned):**
  ```
  {"type":"header","journal_version":1,"tool_version":"0.1.0","folder":"D:\\Photos\\Trip","started":"2026-10-03T10:00:00Z","op_count":200}
  {"type":"step","seq":1,"phase":1,"from":"IMG_0001.JPG","to":".exr-3f2a91c0-IMG_0001.JPG"}
  {"type":"step","seq":201,"phase":2,"from":".exr-3f2a91c0-IMG_0001.JPG","to":"2024-07-14_18-03-22.JPG"}
  {"type":"commit"}
  ```
- **Consistency:** each run is all-or-nothing, achieved through rollback. The filesystem has no multi-file transactions, so the journal is how recovery works. Single-file `rename` within one directory is atomic on NTFS, APFS and ext4.
- **Retention:** journals are kept until the user deletes them. `undo` only works while the journal exists.
- **Sensitivity:** filenames and timestamps only. The tool reads no GPS data and logs nothing outside the folder.
- **Evolution:**
  - `journal_version` is a contract: `undo` in version N must read journals from version N-1.
  - Fields are only ever added.
  - An unknown version means `undo` refuses to run and explains why.

## 9. Key Flows

**A. Preview (the default).** `exifrename D:\Photos\Trip`
1. Scan the folder.
2. Read metadata for each file.
3. Build the plan.
4. Print a table (`IMG_0001.JPG → 2024-07-14_18-03-22.JPG`) and a summary: `180 rename, 12 already named, 8 skipped (5 no EXIF date, 2 zero date, 1 unreadable)`.
5. Write nothing. Exit 0.

**B. Apply.** `exifrename D:\Photos\Trip --apply`

Same as A, then:
1. Recheck that the destinations are free.
2. Write the journal.
3. Run the two-phase rename.
4. Write `commit`.
5. Print the summary and the journal path.

Running it again gives "0 rename, 192 already named".

**C. Failure: locked file mid-run (Windows).** Rename step 37 raises `PermissionError` because the file is open in a photo viewer.
1. The executor catches it.
2. It reverses steps 36 to 1 using the journal.
3. It writes `rolled_back`.
4. It prints: `Rename of IMG_0037.JPG failed (file in use). All changes rolled back; folder unchanged.`
5. Exit 3.

If a rollback step also fails, the executor stops and prints `Run: exifrename undo D:\Photos\Trip`.

**D. Crash or power loss mid-run.** The journal has no `commit` or `rolled_back` line, and the folder contains a mix of temp and final names.
1. The next `exifrename` run on that folder detects the unfinished journal.
2. It refuses to plan anything new and tells the user to run `undo`.
3. `undo` returns every file it can find to its `from` name.

## 10. Key Decisions

| # | Decision | Options considered | Rationale (by driver) | Reversibility | Revisit if |
|---|---|---|---|---|---|
| D1 | Python package installed via pipx/uv | Python; Go single binary; shell wrapper around ExifTool | Python has the strongest EXIF libraries (coverage, #3) and the assumed team skill. Go's HEIC/RAW EXIF support is thinner. A wrapper gives no preview or undo (#1). | Hard (rewrite) | The user needs a no-Python single binary |
| D2 | `MetadataReader` interface. `exifread` by default, ExifTool optional. | `exifread`; Pillow + pillow-heif; ExifTool only | `exifread` is pure Python with no native or Perl dependency (#4) and is believed to support JPEG, HEIC and TIFF-based RAW. Pillow can't read RAW. ExifTool has the best coverage but needs a separate install. | Easy (behind the interface) | Spike A shows more than 1% misses on target formats, which would make ExifTool the default or required |
| D3 | Preview by default, `--apply` to act | Apply with a confirmation prompt; preview by default | Preview by default fails safe when scripted or mistyped (#1) | Easy | — |
| D4 | Two-phase rename with write-ahead journal and auto-rollback | Ordered single-pass rename; copy into a new folder; two-phase with journal | A single pass needs cycle detection and still can't recover from a crash. Copying is safest but doubles disk use and isn't a "rename". Two-phase is simple and recoverable (#1). | Medium (journal format is a contract) | Users report slow runs on network drives (two renames per file) |
| D5 | Name format `YYYY-MM-DD_HH-MM-SS[_NN].ext`, extension case kept | Compact `YYYYMMDD_HHMMSS`; ISO with colons | Readable and sorts correctly. Colons are illegal on Windows. `_NN` sorts correctly. | Hard in practice once libraries are renamed | User prefers another format (add `--format` before 1.0) |
| D6 | No date means skip and report | Fall back to file mtime; parse the filename | Never guess silently (#2). mtime changes on copy. | Easy (add opt-in flags) | Many skips in the real library, which would add an opt-in `--fallback mtime` |
| D7 | Use the recorded local wall-clock time, no time-zone conversion | Convert to UTC using `OffsetTime*` | Matches what the user saw on the camera. Offsets are often missing (#2). | Easy | User shoots across time zones and wants a single timeline |

## 11. Cross-Cutting Concerns

- **Security (M1):**
  - **Threat 1, crafted image files exploiting the parser.** `exifread` is pure Python, which avoids memory-corruption bugs. Exceptions are caught per file. Dependencies are pinned, and `pip-audit` runs in CI.
  - **Threat 2, path injection through metadata.** Target names are built only from parsed integers and the original extension. Metadata strings never go into a filename.
  - **Threat 3, symlinks pointing outside the folder.** The scanner skips them.
  - There are no secrets and no network access.
- **Reliability (M1):**
  - All-or-nothing runs through the journal (§7).
  - A destination-exists check comes right before each phase-2 rename, which guards against POSIX's silent overwrite. A race with another process remains possible. This is accepted and documented, because Python doesn't offer a portable no-replace rename.
  - The ExifTool subprocess, if used, has a 120 s timeout.
- **Observability (M1):**
  - `-v` prints the source tag and the reason for every skip.
  - The journal serves as the audit log.
  - The summary line always shows counts.
- **Performance (M2 benchmark):** `exifread` with `details=False` reads only headers. Expect over 200 files/s locally. Network drives are the likely bottleneck. Measured with `bench/make_corpus.py`, which creates 10k JPEGs.
- **Cost:** none (local tool). **Operations:** no on-call. The maintainer handles issues on GitHub. The README covers "how to undo" and "what the skip reasons mean."
- **AI-specific:** not applicable. The tool has no model components.

## 12. Build Sequence

Team assumption for sizes: one Python developer working focused days. These are rough guides, not commitments.

| Milestone | Goal | Scope / excluded | Deliverables | Exit criteria | Depends on | Size |
|---|---|---|---|---|---|---|
| **M1: Safe JPEG slice + spikes** (critical path) | Prove the safety design end to end. Settle the metadata-backend and filesystem unknowns. | Includes: JPEG; preview, apply, undo; burst suffixes; journal; rollback; CI on 3 OSes. Excludes: HEIC/RAW, companion files, fallbacks, PyPI. | Installable package; `exifrename` CLI; fixtures; Spike A and B reports | CI green on Windows, macOS and Linux. Property tests and fault-injection tests pass. Applying and undoing on the fixtures gives identical names and hashes. Spike reports are committed with a D2 recommendation. | — | 3–5 days |
| **M2: Real-library coverage** | Meet the format-coverage and correctness targets on real photos | Includes: HEIC and RAW per Spike A (ExifTool backend if indicated), companion files, opt-in `--fallback mtime`, recovery from unfinished journals, benchmark. Excludes: recursion, time shifting. | Corpus test suite; companion grouping in the planner; `bench/` | Corpus test: at least 99% of the target-format sample dated correctly. Companion files move together in tests. Preview of 10k files takes under 60 s. | M1 (backend choice from Spike A) | 3–5 days |
| **M3: Release 1.0** | Get it into the user's hands safely | Includes: README, `--version`, PyPI release workflow, naming format frozen | PyPI package; tagged release | `pipx install exifrename` works on a clean Windows machine. One full apply and undo on a **copy** of the real library, with zero mismatches. | M2 | 1–2 days |

The critical path runs: scaffold → planner → executor/journal → CLI (M1) → backend decision → corpus coverage (M2) → release.

## 13. First Milestone Task Breakdown

| # | Task | Location | Done when |
|---|---|---|---|
| T1 | **Scaffold** the package. hatchling, `requires-python >=3.11`, dependency `exifread` (pinned), dev dependencies `pytest`, `hypothesis`, `piexif`, `ruff`, `pip-audit`. Console script `exifrename = exifrename.cli:main`. | `pyproject.toml`, `src/exifrename/__init__.py`, `README.md` | `uv tool install .` then `exifrename --version` prints `0.1.0` on Windows |
| T2 | **Add CI**: matrix of `windows-latest`, `macos-latest`, `ubuntu-latest` × Python 3.11 and 3.13, running `ruff check`, `pytest` and `pip-audit` | `.github/workflows/ci.yml` | A trivial test passes on all 6 jobs |
| T3 | **Spike A: metadata coverage** (time box: 1 day; parallel with T1/T2). *Question:* does `exifread` correctly read `DateTimeOriginal`, `SubSecTimeOriginal` and `OffsetTimeOriginal` from JPEG, HEIC, CR2, CR3, NEF, ARW, DNG and PNG? *Build:* a script that runs `exifread` and `exiftool -j` on a corpus (public samples from raw.pixls.us plus 50–100 copies from the user's library) and compares the results. | `spikes/metadata_coverage.py`, `spikes/REPORT-A.md` | The report has a per-format agreement table and a D2 recommendation. **Changes the plan if:** any target format is under 99% agreement, in which case ExifTool becomes the M2 backend. |
| T4 | **Spike B: rename behaviour** (time box: 0.5 day). *Question:* how does `os.rename` behave on NTFS, exFAT (SD card or USB), APFS and ext4 when the destination exists, when only the case changes, when the file is locked or open (Windows), and when the path is over 260 characters? | `spikes/rename_semantics.py`, `spikes/REPORT-B.md` | Results are recorded per filesystem. **Changes the plan if:** exFAT or NTFS show non-atomic behaviour or surprises that two-phase renaming doesn't cover. |
| T5 | **Generate fixtures** with piexif. Cases: normal date; no EXIF; zero date; only `DateTime`; only `DateTimeDigitized`; 3-shot burst with subseconds; burst without subseconds; malformed string; a file already at its target name; a target name already taken by an unrelated file; a circular target clash. | `tests/fixtures/make_fixtures.py` (run inside a pytest fixture into `tmp_path`, no committed binaries) | Each case has an expected outcome in `tests/fixtures/expected.py` |
| T6 | **Implement** `CaptureTime`, `ReadResult`, the `MetadataReader` protocol and `ExifreadReader` with the §7 tag precedence and rejection rules | `src/exifrename/metadata.py`, `tests/test_metadata.py` | Every T5 case returns the expected `CaptureTime` or `Skip` reason |
| T7 | **Implement** the scanner: no recursion, case-insensitive extension filter, skip hidden files, `.exr-*`, `.exifrename/` and symlinks | `src/exifrename/scanner.py`, `tests/test_scanner.py` | Tests cover each skip rule |
| T8 | **Implement** `build_plan` as a pure function: casefold collision checks, burst suffixes ordered by `(subsec, name)`, no-op detection | `src/exifrename/planner.py`, `tests/test_planner.py` | Hypothesis properties hold: (a) no two operations share a destination (casefolded); (b) no destination equals a file outside the plan; (c) planning the result of an applied plan gives no operations; (d) the same input always gives the same output |
| T9 | **Implement** the journal (write-ahead JSONL with `fsync`) and the executor (two-phase apply, auto-rollback, `undo`, refusal when an unfinished journal exists) | `src/exifrename/journal.py`, `src/exifrename/executor.py`, `tests/test_executor.py` | Fault injection: monkeypatched `os.rename` fails at every step index *k* on a 10-file fixture, and each time the folder ends up byte-identical to the original. Applying then undoing restores names and SHA-256 hashes. |
| T10 | **Wire** the CLI and report: `exifrename PATH [--apply] [-v]`, `exifrename undo PATH [--journal FILE]`, plan table, summary, exit codes 0, 1, 2 and 3 | `src/exifrename/cli.py`, `src/exifrename/report.py`, `tests/test_cli_e2e.py` | A subprocess end-to-end test of preview → apply → second apply (0 renames) → undo passes on all 3 OSes in CI |

**Ordering:**
- T1 comes first.
- T2, T3 and T4 run in parallel on day 1.
- T5 → T6.
- T7 and T8 run in parallel after T1.
- T9 needs T8 and the Spike B results.
- T10 comes last.

## 14. Testing and Validation Strategy

- **Unit and property tests** (planner, metadata) cover date correctness (#2) and collision safety (#1).
- **Fault-injection tests** (executor) cover all-or-nothing behaviour (#1). They block merges.
- **End-to-end subprocess tests** on 3 OSes in CI cover the platform differences (#4).
- **Corpus test** (M2): public RAW/HEIC samples plus `expected.csv`. It verifies the 99% target (#3). The user's private photos are tested locally only and never committed.
- **Benchmark** (M2): `bench/make_corpus.py` creates 10k JPEGs and is timed against the 60 s target (#5). It runs manually, not in CI.
- **Release gate** (M3): all CI green, plus a manual apply and undo on a copy of the real library.

## 15. Rollout, Migration, and Rollback

- **Rollout:** preview by default acts as the safety switch. The README tells users to try it on a copy of a folder first. Releases go out as PyPI tags: `0.x` during M1 and M2, `1.0` at M3.
- **Migration:** none. This is a new tool and no existing data changes format.
- **Rollback:**
  - Each run is undone with `exifrename undo`.
  - A tool release is rolled back with `pipx install exifrename==<previous>`. Journal versioning keeps undo working across versions.
  - **Point of no return:** deleting the journal, or renaming or moving the files with other tools after the run.

## 16. Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation | Early warning | Owner |
|---|---|---|---|---|---|
| A file is overwritten (POSIX silent `rename` overwrite, race, planner bug) | Low | Severe | Casefold planner checks, recheck before each rename, two-phase temp names, property and fault-injection tests | Any property-test shrink case; an issue reporting a "missing photo" | Developer |
| `exifread` misses HEIC or CR3 dates | Medium | High | Spike A in week 1, ExifTool backend behind the interface | Spike A agreement under 99% | Developer |
| Companion files lose their link to the photo (`.xmp`, `.aae`, `.mov`) | High if not handled | Medium | Same-stem grouping in M2. Until then, the M1 preview **warns** when companions exist. | Fixtures or users with `.xmp` files | Developer |
| Wrong date because the camera clock was wrong or set to another time zone | Medium | Medium | Out of scope. Documented. `--shift` deferred. Undo makes it recoverable. | User reports | User/Developer |
| Locked files on Windows cause frequent rollbacks | Medium | Low | Clear message naming the file. Possible `--skip-locked` later. | Repeated exit 3 | Developer |
| Scope creep (GUI, recursion, video, dedupe) | Medium | Medium | Non-goals list. Deferred list with triggers. | Milestones slipping | User |
| Building something ExifTool already does | Medium | Low–Medium | Open question 1 asked up front | User is happy with the one-liner | User |

## 17. Deferred Work and Future Evolution

- **`--recursive`.** Trigger: users have nested year/event folders. The planner already works per folder, so it can run once per directory.
- **`--format` template.** Trigger: requests for a different format. This must ship before 1.0 if the user wants a different default.
- **`--shift ±HH:MM`** to correct camera clocks, and **time-zone normalisation.** Trigger: multi-camera or travel libraries.
- **Videos** as items in their own right. These need QuickTime `CreateDate`, which is stored in UTC, and so likely need ExifTool.
- **Filename-pattern fallback** (`IMG_20240714_180322.jpg`). Trigger: many skips from messaging-app exports.
- **Known shortcut:** the check-then-rename race isn't fully closed. The fix would be platform-specific no-replace renames (`renameat2` on Linux, `renamex_np` on macOS, `MoveFileEx` without the replace flag on Windows) via ctypes. Trigger: any multi-process use.

## 18. Next Steps

1. Answer the open questions below, especially formats and ExifTool. This takes 5 minutes and decides D2.
2. Copy 50–100 representative photos, including any CR3, HEIC and `.xmp` files, into a scratch folder for Spike A.
3. Run `uv init --package exifrename` (or create `pyproject.toml` by hand) in this workspace and do T1.
4. Install ExifTool on the developer machine just for Spike A's comparison, and write `spikes/metadata_coverage.py`.
5. Start T8 (`planner.py`) with its property tests. It's the safety core and needs no other module.

---

I wrote this plan without asking you anything first. The workspace contains only a placeholder README, so this is a new build. Every guess is listed in §5.

## Open questions that would most change this plan

1. **Which formats are really in your library?** Phone JPEG and HEIC only, or camera RAW too, and Canon CR3 in particular? This decides whether pure-Python `exifread` is enough or whether ExifTool becomes required.
2. **Is it fine to require ExifTool?** If yes, the metadata layer gets simpler and more complete. It's also worth asking whether ExifTool's own one-liner would already do the job, so you'd only be building the preview and undo safety layer.
3. **Do you use Lightroom or darktable `.xmp` files, or iPhone Live Photos?** If so, companion-file grouping becomes a must-have for M2, not a nice-to-have.
4. **What should happen to photos with no EXIF date** (screenshots, messaging-app exports)? Skip them (the default), use the file's modified time, or parse dates from their filenames?
5. **Is `2024-07-14_18-03-22.jpg` the format you want, and do your photos span time zones?** Once a library has been renamed, the format is effectively permanent. Time zones would add offset handling.
