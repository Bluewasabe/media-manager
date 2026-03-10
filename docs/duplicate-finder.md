# Duplicate Finder

Scans multiple drives and folders, finds duplicate and near-duplicate files, scores each copy by quality, and consolidates the best version. **Runs as a dry-run by default** — nothing is moved or deleted until you pass `--execute`.

---

## Requirements

| Dependency | Purpose | Install |
|---|---|---|
| Python 3.9+ | Runtime | — |
| [Pillow](https://pillow.readthedocs.io/) | Photo EXIF + dimensions | `pip install Pillow` |
| [ffmpeg/ffprobe](https://ffmpeg.org/download.html) | Video codec, bitrate, audio, duration | Place `ffmpeg-*/` folder next to this script **or** add `ffprobe` to `PATH` |
| [imagehash](https://github.com/JohannesBuchner/imagehash) | Perceptual image hashing *(optional)* | `pip install imagehash` |

Pillow, ffprobe, and imagehash are all optional — the script degrades gracefully, falling back to exact-hash-only matching for any missing dependency.

---

## Usage

```
python duplicate_finder.py --sources <dir> [<dir> ...] --output <dir> [options]
```

### Positional / required arguments

| Argument | Description |
|---|---|
| `--sources` | One or more folders/drives to scan, **in priority order** (leftmost = highest priority) |
| `--output` | Where to move winning files when `--execute` is used |

---

## CLI Options

| Flag | Default | Description |
|---|---|---|
| `--execute` | off | Actually move/archive/delete files. Without this the script is a **dry-run** — scans and writes the report, nothing is touched. |
| `--action` | `archive` | What to do with losing duplicates: `archive` (move to `--archive` folder), `delete` (permanent, requires `YES` confirmation), or `report` (no file operations at all). |
| `--archive DIR` | `<output>/duplicates` | Folder to move losers into when `--action archive`. |
| `--types` | `all` | Which categories to deduplicate: `photos` `videos` `audio` `documents` `other` `all`. Can combine: `--types photos videos`. |
| `--perceptual` | off | Enable perceptual hashing for near-duplicate image detection (requires `imagehash`). |
| `--skip-video-meta` | off | Skip ffprobe entirely for videos. Much faster — useful for a quick first pass. Disables codec/bitrate scoring. |
| `--hash` | `sha256` | Hash algorithm for exact matching: `sha256` (safe) or `md5` (faster). |
| `--workers N` | `8` | Number of parallel scan threads. Reduce on slow or heavily loaded drives. |
| `--report FILE` | `<output>/duplicate_report.html` | Path for the HTML report. Always written regardless of `--execute`. |
| `--min-size BYTES` | `4096` | Skip files smaller than this. Filters out thumbnails, icon caches, etc. |

---

## Examples

```powershell
# Dry-run: scan two drives, write report, touch nothing
python duplicate_finder.py --sources "D:\Movies" "E:\Backup" --output "M:\Movies"

# Fast preview: skip ffprobe (seconds instead of minutes on large collections)
python duplicate_finder.py --sources "D:\Movies" "E:\Backup" --output "M:\Movies" --skip-video-meta

# Execute: archive losers to default location (<output>/duplicates)
python duplicate_finder.py --sources "D:\Movies" "E:\Backup" --output "M:\Movies" --execute

# Execute: archive losers to a specific drive
python duplicate_finder.py --sources "D:\Movies" "E:\Backup" --output "M:\Movies" --execute --archive "Z:\Dupes"

# Execute: delete losers permanently (prompts for YES)
python duplicate_finder.py --sources "D:\Movies" "E:\Backup" --output "M:\Movies" --execute --action delete

# Photos only with perceptual matching (same photo in different quality)
python duplicate_finder.py --sources "D:\Photos" "E:\OldPhotos" "F:\Recovered" --output "M:\Photos" --types photos --perceptual --execute

# Scan three drives, P1 is master (wins all ties)
python duplicate_finder.py \
  --sources "D:\Organized" "E:\Backup" "F:\OldDrive" \
  --output "M:\Final" \
  --execute --workers 16
```

---

## How Source Priority Works

Sources are listed left-to-right; **P1 is the highest priority**. Priority only matters for **tie-breaking** within a duplicate group — if two files are identical in quality score, the copy from the earlier source wins and is kept.

```
--sources "D:\Organized" "E:\Backup" "F:\OldDrive"
            P1 (master)    P2          P3
```

If `D:\Organized\movie.mkv` and `E:\Backup\movie.mkv` are byte-identical, the P1 copy (`D:\`) is kept and the P2 copy is archived/deleted.

This lets you declare "my main drive is authoritative" without needing to pre-sort files.

---

## Detection Tiers

Three layers of duplicate detection run in sequence. Each file can only appear in one group.

### Tier 1 — Exact (SHA-256 hash)

Files with identical bytes. Pre-filtered by size first (different sizes cannot be identical), so this is fast even across millions of files.

### Tier 2 — Near-duplicate video

Files not caught by Tier 1 that share the same media identity and a similar duration.

**Identity matching:**
- **Movies:** cleaned title + year (e.g. `The Matrix (1999)` matches `Matrix.1999.1080p.mkv`)
- **TV episodes:** show name + season + episode (e.g. `S03E07` matching is exact)

**Duration tolerance:** ±5% — handles different versions that trim credits or include/exclude intros.

### Tier 3 — Near-duplicate photo (optional)

Enabled with `--perceptual`. Uses **pHash** (perceptual hash) with a Hamming distance threshold of 10. Catches:
- Same photo at different JPEG compression levels
- Same photo resized slightly
- Same photo with minor color adjustment

Requires `pip install imagehash`. Skipped entirely if the library is not installed.

---

## Quality Scoring

Every file in a duplicate group is scored. The highest-scoring file is the **winner** (kept). All others are **losers** (archived or deleted).

### Photos (0–100 points)

| Factor | Weight | Notes |
|---|---|---|
| Resolution | 0–60 pts | Logarithmic scale — 12 MP ≈ 40 pts, 48 MP ≈ 55 pts |
| Format | 0–20 pts | RAW > TIFF > PNG > JPEG > WebP (see table below) |
| EXIF completeness | 0–10 pts | +4 date, +3 camera model, +3 GPS |
| File size | 0–10 pts | Tiebreaker within same format; capped at 50 MB |

**Format ranking:**

| Format | Score | Category |
|---|---|---|
| `.dng`, `.cr2`, `.cr3`, `.nef`, `.arw` | 95–100 | RAW |
| `.orf`, `.rw2` | 90 | RAW |
| `.tiff`, `.tif` | 70 | Lossless |
| `.png` | 60 | Lossless compressed |
| `.bmp` | 55 | Uncompressed |
| `.heic` | 45 | Lossy (Apple) |
| `.webp` | 40 | Lossy |
| `.jpg`, `.jpeg` | 30 | Lossy |
| `.gif` | 10 | Very lossy / 256-color |

### Video (0–100 points)

| Factor | Weight | Notes |
|---|---|---|
| Resolution | 0–40 pts | Logarithmic — 720p ≈ 24 pts, 1080p ≈ 30 pts, 4K ≈ 38 pts |
| Codec | 0–20 pts | AV1 > HEVC/H.265 > H.264 > VP9 > XviD/DivX > WMV |
| Bitrate | 0–15 pts | Higher is better; capped at 40 000 kbps |
| Container | 0–10 pts | MKV > MP4 > MOV > AVI > WMV |
| Audio codec | 0–5 pts | TrueHD/DTS-HD > DTS/AC3 > AAC > MP3 |
| Audio channels | 0–5 pts | 7.1 > 5.1 > stereo > mono |
| HDR bonus | +5 pts | Detected via `color_transfer`, `color_space`, pixel format |

**Codec ranking (abbreviated):**

| Codec | Score |
|---|---|
| AV1 | 100 |
| HEVC / H.265 | 90 |
| H.264 / AVC | 70 |
| VP9 | 65 |
| XviD / DivX | 35 |
| WMV | 10–15 |

### Audio / Documents / Other

Scored purely by file size (logarithmic). Larger = more complete.

---

## Output Structure

When `--execute` is used, files are organized under `--output` by category:

```
<output>/
├── photo/          ← winning photos
├── video/          ← winning videos
├── audio/          ← winning audio
├── document/       ← winning documents
└── duplicates/     ← losers (when --action archive)
    ├── photo/
    ├── video/
    ├── audio/
    └── document/
```

Filename collisions within a destination folder are resolved automatically by appending `_1`, `_2`, etc.

---

## HTML Report

Always written — even in dry-run mode. Located at `<output>/duplicate_report.html` (override with `--report`).

**Report includes:**
- Summary stat boxes: files scanned, groups found, space recoverable, exact/near-video/near-photo counts, tool availability
- Source priority list
- Every duplicate group with winner (green left border) and losers (red left border)
- Quality score badge per file (color-coded: green ≥ 70, orange ≥ 40, red < 40)
- Detail column: resolution, codec, bitrate, audio, HDR flag, duration (video) or resolution, camera, date, GPS (photo)
- DRY-RUN / EXECUTED status banner

---

## Code Map

```
duplicate_finder.py
│
├── MODULE-LEVEL CONSTANTS
│   ├── PHOTO_EXTS / VIDEO_EXTS / AUDIO_EXTS / DOCUMENT_EXTS
│   ├── PHOTO_FORMAT_RANK     Dict: extension → quality score (0–100)
│   ├── VIDEO_CODEC_RANK      Dict: codec name → quality score (0–100)
│   ├── VIDEO_CONTAINER_RANK  Dict: extension → quality score (0–100)
│   ├── AUDIO_CODEC_RANK      Dict: codec name → quality score (0–100)
│   └── AUDIO_CHANNELS_SCORE  Dict: channel count → quality score (0–100)
│
├── METADATA EXTRACTION  (no side effects)
│   ├── read_photo_metadata(path) → dict
│   │     Pillow: actual width/height, EXIF date, camera make/model, GPS flag.
│   │     Avoids "Apple Apple iPhone" double-brand strings.
│   │
│   ├── read_video_metadata(path) → dict
│   │     ffprobe (JSON): codec, bitrate, width, height, audio codec/channels,
│   │     duration, creation date, HDR detection (color_transfer / pix_fmt).
│   │     15-second timeout; returns empty dict on failure — no crash.
│   │
│   ├── compute_sha256(path) → str
│   │     Reads file in 1 MB chunks. Returns "" on I/O error.
│   │
│   └── compute_phash(path) → str | None
│         Pillow + imagehash.phash(). Returns None if either library is absent.
│
├── QUALITY SCORING
│   ├── score_photo(rec) → float       Uses pixels + format rank + EXIF bonus + size
│   ├── score_video(rec) → float       Uses pixels + codec + bitrate + container + audio + HDR
│   ├── score_general(rec) → float     Log-scaled file size only
│   └── compute_quality_score(rec)     Dispatch: calls the right scorer by category
│
├── MEDIA IDENTITY PARSING
│   └── parse_media_identity(path) → dict
│         Extracts (title, year) or (show, season, episode) from filename.
│         Uses same junk-tag regex as media_organizer.py.
│         Used for Tier 2 near-duplicate video grouping.
│
├── PER-FILE ANALYSIS
│   └── analyze_file(path, priority, perceptual, skip_video_meta) → FileRecord | None
│         1. stat() for size; skip if 0
│         2. Classify by extension → category
│         3. compute_sha256
│         4. read_photo_metadata or read_video_metadata
│         5. compute_phash (if --perceptual and photo)
│         6. parse_media_identity (videos only)
│         7. compute_quality_score
│         Returns None on any OS error.
│
├── SCANNING
│   ├── collect_paths(sources) → [(path, priority), …]
│   │     Recursive rglob, skips .git / __pycache__ / $RECYCLE.BIN etc.
│   │
│   └── scan(sources, workers, perceptual, skip_video_meta) → [FileRecord, …]
│         Dispatches analyze_file across a ThreadPoolExecutor.
│         Prints progress every 500 files.
│
├── GROUPING
│   └── group_duplicates(records) → [DupeGroup, …]
│         Tier 1: hash_map → exact groups (hash → [records])
│         Tier 2: identity_map → near-video groups; sub-clustered by duration ±5%
│         Tier 3: pHash Hamming ≤ 10 → near-photo groups (if imagehash available)
│         Each group is sorted: quality_score DESC, source_priority ASC, size DESC
│         group.winner = records[0] after sort.
│         group.space_recoverable = sum of all loser sizes.
│
├── EXECUTION
│   └── execute_dedup(groups, output, archive, action)
│         action="archive": moves winner to output/category/, loser to archive/category/
│         action="delete":  moves winner, unlinks losers
│         action="report":  no-op (report only)
│         Collision-safe: appends _1, _2, … to filenames that already exist.
│
├── HTML REPORT
│   └── write_report(groups, records, report_path, sources, action, executed)
│         Dark-theme HTML. Always written regardless of --execute.
│         Stat boxes, source priority list, per-group tables with winner/loser rows.
│
└── ENTRY POINT
    └── main()
          argparse → validate sources → confirm if execute+delete or execute+archive
          → scan → filter by --types / --min-size → group_duplicates
          → print console summary → execute_dedup (if --execute)
          → write_report (always)
```

---

## Extending the Script

### Add a new video codec to the ranking

Edit `VIDEO_CODEC_RANK` near the top of the file. Keys are lowercase codec names as reported by ffprobe (e.g. `"av1"`, `"hevc"`). Values are 0–100.

### Add a new photo format

Add the lowercase extension to `PHOTO_EXTS` and give it a score in `PHOTO_FORMAT_RANK`.

### Change the perceptual hash threshold

Edit `PHASH_THRESHOLD` (default `10`). Lower = stricter (fewer near-dupes detected). Higher = more aggressive (may group different photos). Range is 0–64 for pHash.

### Change the video duration tolerance

Edit the `0.05` constant in `group_duplicates` (the `±5%` check). E.g. `0.03` for stricter (±3%), `0.10` for looser (±10%).

### Add a new file category

1. Add extensions to a new `_EXTS` set.
2. Add the set to the `ext in` dispatch chain in `analyze_file`.
3. Add a scorer in the `compute_quality_score` dispatch.
4. Add the category name to `--types` choices in `main()`.
