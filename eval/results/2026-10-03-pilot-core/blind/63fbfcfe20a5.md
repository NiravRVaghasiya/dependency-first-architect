# Plan: `photodate`, a CLI that renames photos by their EXIF capture date

## 0. Check first: do you need a new tool?

ExifTool can already do the basic rename in one line:

```
exiftool '-FileName<DateTimeOriginal' -d '%Y-%m-%d_%H%M%S%%-c.%%e' ./photos
```

A separate tool is only worth building if you want what that command doesn't give you easily:
- a preview by default
- undo
- renaming related files (`.xmp`, `.aae`, Live Photo `.mov`) along with the photo
- a clear fallback when a photo has no date
- a readable report

The plan below assumes you do want those.

---

## 1. Goals and non-goals

**Goals**
- Rename every photo in a folder to a name based on when it was taken, e.g. `2024-07-14_183205.jpg`.
- Be safe: it never overwrites a file, it previews by default, and every run can be undone.
- Handle the common formats: JPEG, HEIC, PNG, the main RAW formats (CR2/CR3/NEF/ARW/DNG), and optionally phone videos (MOV/MP4).
- Give the same result if you run it twice. Files that already have the right name are left alone.

**Non-goals (v1)**
- Moving files into date folders (good v2 feature: `--into "{year}/{month}"`)
- Editing EXIF data
- Finding duplicates
- A GUI

---

## 2. Command-line interface

```
photodate [OPTIONS] PATH

  -n, --dry-run / --apply     Preview (default) or actually rename
  -f, --format TEXT           Name template, default "{date:%Y-%m-%d_%H%M%S}"
  -r, --recursive             Include subfolders
      --fallback [none|mtime|filename]
                              What to do when there's no EXIF date (default: none = skip)
      --tz-shift "+2h"        Fix a camera clock that was set wrong
      --include-video         Also rename MOV/MP4 using QuickTime dates
      --no-sidecars           Don't rename matching .xmp/.aae/.mov files
      --on-collision [suffix|skip|error]   (default: suffix)
      --undo LOGFILE          Reverse an earlier run
  -v, --verbose / -q, --quiet
      --json                  Machine-readable report
```

Example output:
```
$ photodate ~/Pictures/trip
  IMG_4021.HEIC  →  2024-07-14_183205.heic
  IMG_4021.MOV   →  2024-07-14_183205.mov      (live photo pair)
  DSC00012.ARW   →  2024-07-14_183205-2.arw    (collision → suffix)
  scan_003.jpg   –  SKIPPED: no capture date (use --fallback)
3 to rename, 1 skipped. Re-run with --apply to perform.
```

---

## 3. How the date is chosen (the core logic)

Check these sources in order and use the first one that's present and valid:

1. `EXIF:DateTimeOriginal`: when the shutter fired. This is the one you want.
2. `EXIF:CreateDate` (DateTimeDigitized)
3. `QuickTime:CreateDate`: videos only. **This is stored in UTC**, so convert it to local time using `OffsetTime*` or the system timezone.
4. `XMP:DateCreated`: often present on edited or exported files
5. Fallback, only if the user asked for one:
   - `filename`: parse names like `IMG_20240714_183205.jpg` or `PXL_20240714_...`
   - `mtime`: the file's modified time (least reliable; flag it in the report)

**Do not use** `EXIF:ModifyDate` (IFD0 DateTime). Editing software rewrites it.

**Throw out bad dates.** Treat `0000:00:00 00:00:00`, empty strings, and years before 1990 or in the future as missing.

**Sub-seconds:** if `SubSecTimeOriginal` exists, keep it internally so burst shots sort in the right order, and offer a `{subsec}` token in the template.

**Clock fixes:** `--tz-shift` is applied after the date is chosen.

Every file records *which source* its date came from, and that appears in the report.

---

## 4. How a run works

```
scan → read metadata → plan names → check plan → (preview | apply) → write log
```

1. **Scan.** List files, filter by extension (case-insensitive), skip hidden and system files (`.DS_Store`, `Thumbs.db`).
2. **Read metadata.** Make **one** batched ExifTool call (`exiftool -json -n -G1 -api QuickTimeUTC ...`) instead of one call per file. Calling it per file is roughly 100× slower.
3. **Group related files.** Files with the same stem (`IMG_4021.HEIC`, `.MOV`, `.AAE`, `.xmp`) form a group. The photo's date names the whole group, so they stay matched.
4. **Plan names.** Fill in the template, then:
   - remove characters Windows doesn't allow (`: * ? " < > |`)
   - lowercase the extension (optional flag)
   - handle collisions: `-2`, `-3`, … checked against **both** files already on disk **and** other names in this run
5. **Check the plan.** No two files may get the same target name. Files already correctly named are marked "unchanged".
6. **Apply.** Rename in **two passes**: first everything to a temporary name (`.photodate-tmp-<uuid>`), then to the final name. This prevents chain clashes like A→B while B→C, and handles case-only renames (`IMG.JPG` → `img.jpg`) on Windows and macOS, where filenames aren't case-sensitive.
7. **Write the log.** Save `photodate-undo-<timestamp>.json` in the folder (or `--log-dir`), listing each `{old, new, date_source}`. Write it **before** pass 2 and update it as renames happen, so a crash halfway through can still be undone.

---

## 5. Safety rules

- Preview is the default. Nothing changes without `--apply`.
- Never overwrite a file. Use an exclusive rename that fails if the target exists, rather than `os.replace`.
- Never follow symlinks out of the target folder.
- Ctrl-C finishes the current rename, then writes the log.
- `--undo` checks that each "new" file still exists and that the "old" name is free before renaming it back.
- Exit codes: `0` ok, `1` some files skipped, `2` errors, `3` bad arguments.

---

## 6. Technology choice

| Option | Pros | Cons |
|---|---|---|
| **Python + ExifTool (recommended)** | Reads every format including HEIC, RAW and video; fast in batch mode; quick to build | Needs ExifTool installed (one dependency) |
| Python + Pillow/piexif/pillow-heif only | Pure pip install | Poor RAW and video support; you'd maintain format quirks yourself |
| Go or Rust single binary | Easy to distribute | Native EXIF libraries cover fewer formats; slower to build |

Python packages: `click` or `typer` (CLI), `rich` (output), and ExifTool called through `subprocess` with `-json`. At startup, check that `exiftool` is on PATH and print install instructions if it isn't.

---

## 7. Project layout

```
photodate/
  cli.py          # argument parsing, output
  scanner.py      # file discovery, extension filtering, grouping related files
  metadata.py     # ExifTool batch call → dict per file
  dating.py       # date-source order, validation, timezone/shift logic
  naming.py       # template filling, cleaning, collision suffixes
  planner.py      # builds and checks the rename plan (pure, no I/O)
  executor.py     # two-pass rename, undo log, Ctrl-C handling
  undo.py
tests/
  fixtures/       # small sample files: jpg, heic, cr3, mov, no-exif, zero-date
```

The rule that matters: **`planner.py` is pure.** It takes metadata and returns a plan, without touching the disk. That's where most of the logic lives, and it can be unit-tested without real files.

---

## 8. Testing

- **Unit tests (planner, dating, naming):** source order, zero dates, UTC conversion for video, collisions within one run, case-only renames, Windows-unsafe characters, already-named files.
- **Integration tests:** run on a temp copy of the fixtures, then rename, then undo, and confirm the folder matches the original byte for byte.
- **Repeat run:** a second `--apply` does nothing.
- **Crash test:** kill the process between the two passes and confirm `--undo` recovers.
- **CI:** test on Windows, macOS and Linux, because filename rules differ.

---

## 9. Milestones

1. **MVP:** JPEG/HEIC, `DateTimeOriginal` only, preview and apply, suffix on collision, undo log.
2. **Robustness:** full date-source order, fallbacks, two-pass rename, crash-safe log, `--undo`.
3. **Related files and video:** grouping, Live Photos, QuickTime UTC handling.
4. **Polish:** `--json` report, `--tz-shift`, `--recursive`, more template tokens (`{camera}`, `{seq}`), `--into` folders (v2).

---

## 10. Questions to settle before building

1. **Default name format:** `2024-07-14_183205` (easy to read, sorts correctly) or `20240714_183205` (shorter)?
2. **Burst shots in the same second:** use a `-2` suffix, or sub-seconds (`_183205.123`)?
3. **Keep the original name in the new one?** e.g. `2024-07-14_183205_IMG_4021.jpg`. Easier to trace back, but longer.
4. **Videos:** include them by default, or only with `--include-video`?
5. **Undo log location:** inside the photo folder (travels with it) or in a central app-data folder (keeps the photo folder clean)?

I can scaffold the MVP (milestone 1) once you've decided these.
