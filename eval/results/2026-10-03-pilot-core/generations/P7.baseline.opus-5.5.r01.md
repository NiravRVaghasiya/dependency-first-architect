# Plan: `exifrename`, a CLI that renames photos by EXIF capture date

## 1. Goal and scope

**Goal:** Rename every photo in a folder to a consistent, sortable name based on when it was taken. For example, `IMG_4821.JPG` becomes `2024-07-14_18-32-05.jpg`.

**Version 1 includes:**
- JPEG, HEIC/HEIF, TIFF, and common RAW formats (CR2, CR3, NEF, ARW, DNG)
- Recursive mode as an option
- A dry-run preview
- An undo log

**Version 1 leaves out:** Moving files into date-based folders, editing EXIF data, and a GUI. These could come later.

## 2. Technology choice

| Option | Pros | Cons |
|---|---|---|
| **Python + `exifread` / Pillow + `pillow-heif`** | Easy to distribute with `pipx`, easy to test | RAW/HEIC coverage is incomplete |
| **Python wrapping `exiftool`** | Reads nearly every format, including video | Needs an external Perl/exe install |
| **Go / Rust single binary** | No dependencies, fast | Fewer mature EXIF libraries |

**Recommendation:** Python 3.10+ and `click`, with a pluggable "metadata reader" layer:
- Default to a pure-Python reader (`exifread` + `pillow-heif`).
- Use `exiftool` automatically if it's on `PATH`, since it covers more formats.

This keeps installation simple without limiting which formats work.

## 3. Command-line interface

```
exifrename [OPTIONS] PATH

Options:
  -n, --dry-run / --apply     Show planned renames (default: dry-run)
  -r, --recursive             Descend into subfolders
  -f, --format TEXT           Name template (default: "{date:%Y-%m-%d_%H-%M-%S}")
  --ext-case [lower|upper|keep]  Default: lower
  --fallback [skip|mtime|filename]  What to do when there's no EXIF date (default: skip)
  --on-conflict [suffix|skip|error]  Default: suffix  → _01, _02 …
  --include TEXT / --exclude TEXT    Glob filters
  --tz-shift TEXT             Correct a wrong camera clock, e.g. "+1h" or "-00:30"
  --with-sidecars / --no-sidecars  Rename .xmp/.aae/Live Photo .mov too (default: on)
  --undo LOGFILE              Revert a previous run
  -v, --verbose / -q, --quiet
```

**Template tokens:**
- `{date:<strftime>}`: the capture date
- `{subsec}`: sub-second digits
- `{camera}`: camera model
- `{orig}`: the original file name stem
- `{seq}`: a per-day counter

**Safety default:** The tool only previews unless `--apply` is passed. Renaming files without asking is the main thing that can go wrong.

## 4. Architecture

```
exifrename/
  cli.py          # argument parsing, output, exit codes
  scanner.py      # walks folders, applies filters, groups sidecars with their main file
  metadata.py     # MetadataReader protocol; ExifreadReader, ExiftoolReader
  dates.py        # picks which date tag to trust, parses it, handles sub-seconds and time zones
  naming.py       # fills in the template, cleans up the name for the OS
  planner.py      # builds the full rename plan and resolves name collisions
  executor.py     # renames in two phases and writes the undo log
  undo.py
tests/
```

**How data moves through the tool:**

```
scan → read metadata → resolve date → render name → plan (resolve collisions) → preview | execute → log
```

## 5. Key logic

### Choosing the date
Check these sources in order and use the first one found:
1. `DateTimeOriginal` + `SubSecTimeOriginal` + `OffsetTimeOriginal`
2. `CreateDate` / `DateTimeDigitized`
3. QuickTime `CreationDate` (for Live Photo videos)
4. The `--fallback` source: file modification time, or a date parsed from the filename (e.g. `IMG_20240714_183205`)

**Edge cases to handle:**
- Treat values like `0000:00:00 00:00:00` and blank strings as missing.
- Apply `--tz-shift` before the name is built.
- Record which source was used, so it can appear in the verbose and dry-run output.

### Building the file name
- Characters like `:` and `?` aren't allowed in Windows file names, so the default template uses `-` and `_`. Every rendered name gets cleaned up.
- Keep the original extension (with the chosen letter case). Handle `.jpeg` and `.jpg` the same way when checking for collisions.

### Resolving collisions
This happens in the planner, before any file is touched:
- **Burst shots taken in the same second:** Sort by sub-second value first, then by original name. Add `_01`, `_02`, and so on.
- **Target name already exists and isn't part of this run:** Apply the `--on-conflict` policy.
- **Case-insensitive file systems** (Windows, macOS): Compare names using `casefold()`.
- **Already-renamed files:** If a file's name already matches its target, mark it as a no-op. This makes it safe to run the tool twice.

### Running the renames safely
- **Two phases:** First rename every file to a temporary name (`.exifrename-tmp-<uuid>`), then to its final name. Without this, chains like A→B and B→C, or swaps, would overwrite files.
- **Undo log:** Write `.exifrename-log-<timestamp>.jsonl` *before* each step and flush it each time. If the run crashes partway, the log can still restore everything.
- **Sidecars move with their main file:** `IMG_1.HEIC`, `IMG_1.xmp`, `IMG_1.AAE`, and `IMG_1.MOV` (when it's a Live Photo pair) all get the same new name stem.
- **No silent overwrites:** Use `os.rename` with an existence check rather than `os.replace`.

## 6. Output and exit codes

**Dry-run output:**
```
IMG_4821.JPG   →  2024-07-14_18-32-05.jpg      [DateTimeOriginal]
IMG_4822.JPG   →  2024-07-14_18-32-05_01.jpg   [DateTimeOriginal, burst]
scan_003.png   →  (skipped: no capture date)
Summary: 2 rename, 0 unchanged, 1 skipped, 0 errors
```

**Exit codes:**
- `0`: success
- `1`: some files were skipped or failed
- `2`: bad usage

**Scripting:** Add a `--json` option for machine-readable output.

## 7. Testing strategy

- **Unit tests:**
  - Date parsing, including bad values, sub-seconds, and time-zone offsets
  - Template rendering and name cleanup
  - Collision resolution, including burst shots, swaps, and case-only differences
- **Fixture tests:** Use a small set of real sample files (JPEG, HEIC, a DNG, a Live Photo pair, a file with no EXIF).
- **Property test** (with Hypothesis): For any generated set of file names, applying the plan and then undoing it gives back the original names. No file is ever lost or overwritten.
- **Cross-platform CI:** Run on Windows, macOS, and Linux to cover differences in path handling and letter case.

## 8. Milestones

1. **MVP:** JPEG only, dry-run and apply, default template, collision suffixes, undo log.
2. **Formats:** HEIC and RAW support, `exiftool` backend, sidecar grouping.
3. **Options:** Fallbacks, `--tz-shift`, custom templates, include/exclude filters.
4. **Polish:** JSON output, progress bar for large folders, packaging (`pipx`, plus a standalone exe via PyInstaller).

## 9. Open questions

These decisions would change the design:
- **Videos:** Should the tool rename MP4 and MOV files that aren't part of a Live Photo pair?
- **Default name:** Should it include sub-seconds or the camera model? This affects how often collisions happen.
- **Undo log location:** Keep it in the photo folder, or in a central app-data directory?
- **Folder sorting:** Add a `--into "{date:%Y}/{date:%m}"` option later, or keep the tool rename-only?
