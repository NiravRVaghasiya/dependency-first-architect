# Plan: `exifren`, a CLI that renames photos by EXIF capture date

## 1. Goal and scope

**What it does:** It renames every photo in a folder to its capture timestamp. For example, `IMG_4821.JPG` becomes `2024-07-14_18-32-05.jpg`.

**Requirements:**
- **Safe by default.** It shows a preview first, never overwrites a file, and every run can be undone.
- **Predictable.** Running it twice gives the same result, and files that already have the right name are skipped.
- **Cross-platform.** It works on Windows, macOS and Linux. Windows needs extra care with illegal characters and case-insensitive file names.

**Not in v1:** moving files into date folders, editing metadata, and deduplication. These could be added later.

> **Check first:** ExifTool can already do the basic rename in one line:
> `exiftool -d "%Y-%m-%d_%H-%M-%S%%-c.%%le" "-FileName<DateTimeOriginal" DIR`
> If that's all you need, you may not need a custom tool. A custom tool is worth it for safe previews, undo, keeping related files together, sensible fallbacks and clear reports.

## 2. Technology

| Choice | Recommendation | Why |
|---|---|---|
| Language | **Python 3.10+** | Fast to build, works on all platforms, easy to package with `pipx` |
| Metadata reading | **ExifTool** (run once in batch mode: `exiftool -json -n -r ...`) as the main reader, with **`exifread`** or **Pillow** as a backup | ExifTool reads HEIC, RAW (CR2/NEF/ARW/DNG) and video (MP4/MOV) formats. The Python libraries can't read all of these. |
| CLI framework | `argparse` or `typer` | Simple to use, with built-in help text |
| Tests | `pytest` and a folder of small sample images | |

If you need a single executable with no dependencies, Go with `rwcarlsen/goexif` also works, but its HEIC and video support is weaker.

## 3. Command-line interface

```
exifren [OPTIONS] PATH

  -n, --dry-run            Show planned renames, change nothing (DEFAULT)
  -y, --apply              Actually perform renames
  -f, --format TEXT        Name template (default: "{date:%Y-%m-%d_%H-%M-%S}")
  -r, --recursive          Descend into subfolders
      --ext LIST           Extensions to include (default: jpg,jpeg,heic,png,tif,dng,cr2,nef,arw,mp4,mov)
      --fallback MODE      none | filename | mtime   (default: none → skip)
      --tz-shift DURATION  Correct a wrong camera clock, e.g. "+1h", "-00:30"
      --on-collision MODE  suffix | subsec | skip    (default: suffix)
      --no-sidecars        Don't rename .xmp/.aae/paired files
      --undo LOGFILE       Revert a previous run
      --log FILE           Where to write the undo log (default: PATH/.exifren-<timestamp>.json)
  -v, --verbose / -q, --quiet
```

**Template fields:** `{date:<strftime>}`, `{subsec}`, `{camera}` (model), `{orig}` (original name), `{counter}` and `{ext}`. The extension is always lowercase.

## 4. Finding the capture date

The tool takes the first field it finds, in this order:

1. `DateTimeOriginal`, plus `SubSecTimeOriginal` and `OffsetTimeOriginal`
2. `CreateDate` / `DateTimeDigitized`
3. QuickTime `CreationDate` for videos. Note that QuickTime `CreateDate` is often stored in **UTC**, so it has to be converted to local time.
4. *(only if `--fallback filename`)* a date in the file name, such as `IMG_20240714_183205` or `PXL_20240714_...`
5. *(only if `--fallback mtime`)* the file's modified time. This is unreliable, so the report marks it clearly.
6. If none of these is found, the file is **skipped** and listed in the report.

**Checks:**
- Treat `0000:00:00 00:00:00`, empty values and dates before 1990 or in the future as missing.
- Apply `--tz-shift` after reading the date.

## 5. Related files

Files that belong together share the same base name, and they must keep the same new name:
- RAW + JPEG pairs (`IMG_1.CR2` and `IMG_1.JPG`)
- Sidecar files (`IMG_1.xmp`, `IMG_1.JPG.xmp`, `IMG_1.AAE`)
- Live Photos (`IMG_1.HEIC` and `IMG_1.MOV`)

**Rule:** Group files by folder and base name, then take the date from the best file in the group (RAW > JPEG/HEIC > video). Every file in the group gets the same new base name.

## 6. Pipeline: plan first, then apply

```
scan → read metadata (batch) → group → compute target names → resolve collisions → validate → [preview] → apply → write log
```

1. **Scan:** List the files, filter by extension, and ignore hidden and system files (`Thumbs.db`, `.DS_Store`).
2. **Read metadata:** Make one ExifTool call for the whole folder. Calling it once per file is about 100× slower.
3. **Compute targets:** Fill in the name template for each file. Replace characters Windows doesn't allow (`<>:"/\|?*`). This is why the default format uses `-` instead of `:` in times.
4. **Resolve collisions** (for example, burst shots taken in the same second):
   - `subsec`: add sub-seconds when the camera records them (`_18-32-05.123`)
   - `suffix`: add a counter in capture order (`_18-32-05_01`, `_02`)
   - Check names against both files already in the folder and other planned names. Compare names case-insensitively on Windows and macOS.
5. **Validate:**
   - Skip files whose name already matches the target, so repeat runs change nothing.
   - Find renames that depend on each other in a chain or loop (A→B while B→A).
6. **Preview:** Show a table of old name → new name, plus counts of renamed, skipped and failed files. The tool stops here unless `--apply` is given.
7. **Apply** in two passes:
   - First rename everything to temporary names (`.exifren-tmp-<uuid>`), then rename those to the final names. This handles loops, chains and case-only renames on Windows.
   - Write each completed step to the log immediately, so an interrupted run can still be undone.
8. **Log:** Save a JSON file of `{old, new, tmp}` entries. `--undo` reads this log and reverses each rename.

## 7. Error handling

- A file that's locked or has no permission: skip it, record it, and keep going. The exit code shows partial failure.
- If ExifTool isn't installed: fall back to the Python library and warn that HEIC, RAW and video files may be skipped.
- Never follow symlinks out of the target folder.
- **Exit codes:** 0 = success, 1 = some files failed, 2 = usage error.

## 8. Testing

- **Unit tests:** date parsing, template filling, cleaning up illegal characters, collision resolution and rename loops.
- **Sample files:** JPEG with and without EXIF, HEIC, a RAW+JPEG pair, a Live Photo pair, an MP4 with a UTC date, a burst of 3 shots in the same second, and a bad `0000:00:00` date.
- **Property tests:** after apply followed by undo, the folder is identical to the start. A second apply changes nothing.
- **CI:** run the tests on Windows, macOS and Linux runners, since case and character rules differ between them.

## 9. Milestones

| # | Deliverable | Est. |
|---|---|---|
| 1 | Scanning, ExifTool batch reading, date order, preview only | 1 day |
| 2 | Templates, character cleanup, collisions, two-pass apply, JSON log | 1–2 days |
| 3 | `--undo`, grouping related files | 1 day |
| 4 | Fallbacks, `--tz-shift`, video date handling | 1 day |
| 5 | Tests on all platforms, packaging (`pipx install exifren`), README | 1 day |

## 10. Open questions

1. **Timezone:** Use the camera's local time as recorded (the usual choice), or convert everything to UTC or one chosen zone?
2. **Folders:** Is a rename-only tool enough, or do you want a later `--organize` option that moves files into `YYYY/MM/` folders?
3. **ExifTool:** Is it OK to require it, or must the tool be pure Python or a single executable?
4. **Library size:** How big is it (hundreds or hundreds of thousands of files)? Very large folders would need progress bars and a way to resume an interrupted run.
