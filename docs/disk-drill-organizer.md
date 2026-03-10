# Disk Drill Recovery Organizer

Parses, filters, and reorganizes the raw output of **Disk Drill**'s data recovery into a clean, human-readable folder structure. Photos are sorted by EXIF device and date. Videos are sorted by device or duration bucket. Corrupt files are labeled in their filename. Junk, tiny, and short files are separated into a `Manually Review/` folder for human triage.

---

## Requirements

| Dependency | Purpose | Install |
|---|---|---|
| Python 3.10+ | Runtime | — |
| [Pillow](https://pillow.readthedocs.io/) | Photo EXIF + integrity checks | `pip install Pillow` |
| [ffmpeg/ffprobe](https://ffmpeg.org/download.html) | Video metadata + duration | Place `ffmpeg-*/` folder next to this script **or** add `ffprobe` to `PATH` |

Both Pillow and ffprobe are optional — the script degrades gracefully if either is missing, falling back to Disk Drill's filename-encoded metadata.

---

## Usage

```bash
python disk_drill_organizer.py <input_dir> <output_dir> [options]
```

### Positional Arguments

| Argument | Description |
|---|---|
| `input_dir` | Disk Drill's recovered output folder (e.g. `F:/reconstructed`) |
| `output_dir` | Where to write the organized structure (e.g. `G:/organized`) |

---

## CLI Options

| Flag | Default | Description |
|---|---|---|
| `--execute` | off | Actually copy/move files. Without this the script is a **dry run** — it only scans and writes the report, nothing is touched. |
| `--move` | off | Move files instead of copying. Saves space and is faster. Prompts for `YES` confirmation. **Only use when the source is expendable** (i.e. the Disk Drill output copy, not your original drive). |
| `--include-filtered` | off | Also copy/move filtered files into `Manually Review/` subfolders. Without this flag, filtered files are skipped entirely and stay untouched in the source. |
| `--min-photo-px N` | `800` | Minimum pixel dimension (shorter side) for a photo to be kept. Images below this go to `Manually Review/Small Photos/`. |
| `--min-video-sec N` | `30` | Minimum video duration in seconds to keep. Clips shorter than this go to `Manually Review/Short Videos/`. |
| `--skip-video-meta` | off | Skip ffprobe entirely for videos. Uses only Disk Drill's filename-encoded duration and dimensions. Much faster — useful for a quick dry-run preview. |
| `--workers N` | `8` | Number of parallel scan threads. Reduce if the system is under load or the drive is slow. |
| `--report FILE` | `<output_dir>/report.html` | Path to write the HTML report. Use this if the output drive isn't mounted yet or you want the report elsewhere. |

---

## Examples

```bash
# Quick preview — scan everything, write report, touch nothing
python disk_drill_organizer.py F:/reconstructed G:/organized

# Fast preview — skip ffprobe on videos (seconds instead of minutes)
python disk_drill_organizer.py F:/reconstructed G:/organized --skip-video-meta

# Execute: copy files into G:/organized
python disk_drill_organizer.py F:/reconstructed G:/organized --execute

# Execute: copy + include filtered files in Manually Review/
python disk_drill_organizer.py F:/reconstructed G:/organized --execute --include-filtered

# Execute: move instead of copy (saves disk space)
python disk_drill_organizer.py F:/reconstructed G:/organized --execute --move

# Stricter filters: photos must be 1080p+, videos must be 60s+
python disk_drill_organizer.py F:/reconstructed G:/organized --min-photo-px 1080 --min-video-sec 60

# Write report to a specific path (e.g. if G: isn't mounted yet)
python disk_drill_organizer.py F:/reconstructed G:/organized --report C:/Users/me/Desktop/report.html

# Full run with all options
python disk_drill_organizer.py F:/reconstructed G:/organized --execute --include-filtered --workers 16 --report D:/report.html
```

---

## Output Structure

```
G:/organized/
├── Photos/
│   ├── iPhone 11 Pro/
│   │   ├── 2021/
│   │   │   └── 2021-08/
│   │   │       └── 2021-08-14_143022_4032x3024_000312.jpg
│   │   └── Unknown Date/
│   │       └── iPhone 11 Pro 4032x3024_000455.jpg
│   ├── Nikon Camera/
│   │   └── 2013/
│   │       └── 2013-02/
│   ├── Unknown/
│   └── Other/
├── Videos/
│   ├── iPhone/
│   │   └── 2020/
│   │       └── 2020-06/
│   ├── GoPro/
│   ├── Short Clips (1-10min)/
│   └── Long Clips (1h+)/
├── Audio/
├── Documents/
│   ├── PDF/
│   ├── Word/
│   ├── Excel/
│   └── Text/
├── Unknown/
└── Manually Review/          ← only populated with --include-filtered
    ├── Junk/
    ├── Small Photos/
    ├── Short Videos/
    ├── Corrupt Videos/
    └── _Assets (Adobe Photoshop)/
```

### Filename conventions for kept files

**Photos with EXIF date:**
```
2021-08-14_143022_4032x3024_000312_PARTIAL.jpg
│           │      │          │      └─ corruption label (if any)
│           │      │          └─ Disk Drill sequence number
│           │      └─ dimensions
│           └─ time
└─ date
```

**Photos without EXIF date:**
```
iPhone 11 Pro 4032x3024_000455_PARTIAL.jpg
```

**Videos with metadata date:**
```
2020-06-21_180530_1920x1080_51m16s_001024_NOSTREAM.mp4
```

**Corruption labels** appended to filenames:
| Label | Meaning |
|---|---|
| `PARTIAL` | File is truncated or structurally incomplete (Pillow verify failed / ffprobe timed out) |
| `NOSTREAM` | Video container exists but has no decodable video stream |

---

## How the Code Works

### Entry point and flow

```
main()
 ├── parse args
 ├── scan()             ← parallel file classification
 │    └── classify_file()  ← called once per file in a thread pool
 ├── resolve_dest()     ← assigns collision-safe output paths
 ├── copy/move loop     ← only runs with --execute
 └── write_report()     ← always runs (dry run or not)
```

---

### Module-level setup

**`_find_ffprobe()`**
Searches for `ffprobe.exe` in any `ffmpeg-*/bin/` folder next to the script (using `Path(__file__).resolve().parent`), then falls back to system `PATH`. Result stored in module constants `FFPROBE_BIN` and `FFPROBE_AVAILABLE`.

**Extension sets**
`PHOTO_EXTS`, `VIDEO_EXTS`, `AUDIO_EXTS`, `DOCUMENT_EXTS`, `JUNK_EXTS` — plain sets used for fast `ext in SET` dispatch in `classify_file()`.

**`JUNK_FILENAMES`** — exact lowercase filenames always filtered (e.g. `thumbs.db`, `desktop.ini`).

**`ASSET_SOURCES`** — software names (Photoshop, GIMP, etc.) that indicate web/design assets rather than personal photos.

---

### Filename parsing (Disk Drill convention)

Disk Drill encodes metadata into recovered filenames:
```
SOURCE WxH_SEQNUM.ext          (photos)
SOURCE WxH DURATIONs_SEQNUM.ext  (videos)
```

| Function | What it does |
|---|---|
| `parse_dimensions(stem)` | Regex `\b(\d{2,5})x(\d{2,5})\b` → `(width, height)` or `(None, None)` |
| `parse_duration_seconds(stem)` | Regex cascade matching `1h20m30s`, `51m16s`, `1h20m`, `42m` → integer seconds |
| `parse_source_device(stem)` | Strips sequence suffix, finds first dim/duration tag, returns everything before it as the source string |

---

### Device categorization

| Function | Input | Output |
|---|---|---|
| `categorize_device(source)` | Raw source string (e.g. `"Apple iPhone 11 Pro"`) | Human folder name (`"iPhone 11 Pro"`) |
| `categorize_video_source(source, duration_sec)` | Same + duration | Folder name or duration bucket (`"Short Clips (1-10min)"`) |

`categorize_device` maps known brands/apps via `str.lower()` substring matching. Unknown sources fall through to `"Other"`. Sources matching `ASSET_SOURCES` return `"_Assets (App Name)"` which triggers filtering in `classify_file`.

---

### Metadata extraction

| Function | Library | Returns |
|---|---|---|
| `read_photo_exif(path)` | Pillow `img._getexif()` | `dict` with `date_taken`, `camera_make`, `camera_model`, `gps_lat`, `gps_lon` |
| `read_video_metadata(path)` | ffprobe subprocess (JSON output) | `dict` with `date_taken`, `camera_make`, `camera_model`, `duration_sec`, `width`, `height`, `_timed_out` |
| `exif_device_name(meta)` | — | Combines make + model, avoids double-brand strings like `"Apple Apple iPhone 7"` |

`read_video_metadata` runs ffprobe with a **5-second timeout**. On `TimeoutExpired` it returns `{"_timed_out": True}` — no hang, no crash.

---

### Integrity checking

| Function | Method | Labels |
|---|---|---|
| `check_photo_integrity(path)` | `img.verify()` — structural check, does not decode pixels (~1ms) | `""` = OK, `"PARTIAL"` = truncated/invalid |
| `check_video_integrity(meta, timed_out)` | Inspect ffprobe result | `""` = OK, `"PARTIAL"` = timed out or zero duration, `"NOSTREAM"` = no video stream found |

Labels are appended to output filenames as `_PARTIAL` or `_NOSTREAM`.

---

### `FileRecord` dataclass

```python
@dataclass(eq=False)   # identity-based hash (uses object.__hash__)
class FileRecord:
    src: Path           # absolute source path
    ext: str            # lowercase extension
    size_bytes: int
    category: str       # "photo" | "video" | "audio" | "document" | "junk" | "unknown"
    dest_folder: str    # relative path inside output root
    dest_name: str      # target filename
    filtered: bool
    filter_reason: str
    source_device: str
    width / height: Optional[int]
    duration_sec: Optional[int]
    date_taken: Optional[datetime]
    gps_lat / gps_lon: Optional[float]
    corrupt_label: str  # "PARTIAL" | "NOSTREAM" | ""
```

`eq=False` is required so `FileRecord` objects can be used as dict keys. Without it, the auto-generated `__eq__` breaks `__hash__` inheritance from `object`.

---

### `classify_file(path, min_photo_px, min_video_sec, skip_video_meta)`

The core per-file decision function. Called once per file inside the thread pool.

Decision order:
1. **Junk by extension** → `Manually Review/Junk/` (filtered)
2. **Junk by exact filename** → `Manually Review/Junk/` (filtered)
3. **Too small** (< 4 KB) → `Manually Review/Junk/` (filtered)
4. **Photo path** (`ext in PHOTO_EXTS`):
   - Read EXIF (Pillow) — date, device, GPS, actual dimensions
   - If short side < `min_photo_px` → `Manually Review/Small Photos/` (filtered)
   - If source is a graphics app → `Manually Review/_Assets (App)/` (filtered)
   - Run `check_photo_integrity()` → append label to filename
   - Assign `Photos/<device>/<YYYY>/<YYYY-MM>/` (with date) or `Photos/<device>/Unknown Date/`
5. **Video path** (`ext in VIDEO_EXTS`):
   - Run ffprobe (unless `--skip-video-meta`) → date, device, duration, dims, integrity
   - If duration < `min_video_sec` → `Manually Review/Short Videos/` (filtered)
   - If file < 100 KB → `Manually Review/Corrupt Videos/` (filtered)
   - Append corruption label to filename
   - Assign `Videos/<device>/<YYYY>/<YYYY-MM>/` (with date) or `Videos/<device>/`
6. **Audio** → `Audio/`
7. **Document** → `Documents/<subtype>/` (PDF, Word, Excel, etc.)
8. **Unknown** → `Unknown/<EXT>/`

---

### `resolve_dest(records, out_root)`

Assigns a final `Path` to each record, handling filename collisions within the output by appending `_1`, `_2`, etc. Returns a list in the same order as `records` (no dict — avoids hash issues with dataclasses).

---

### `scan(in_root, ...)`

Walks `in_root` recursively with `Path.rglob("*")`, then dispatches `classify_file()` across a `ThreadPoolExecutor`. Progress is printed every 1,000 files. Results are placed back into a pre-allocated list by index to preserve order.

---

### `write_report(...)`

Generates a dark-theme HTML file with:
- Summary stat boxes (total, kept, filtered, EXIF coverage, GPS coverage)
- Kept files grouped by destination folder, sorted by date
- Filtered files grouped by filter reason
- GPS map links for photos with coordinates
- Corruption badge labels inline in the table
- Pillow / ffprobe availability status

The report is always written — even in dry-run mode.

---

## Source is never modified

The script **never writes, moves, or deletes anything in the input directory**. The source drive (`F:/reconstructed`) is always read-only from the script's perspective regardless of flags used.
