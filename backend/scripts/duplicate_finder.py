#!/usr/bin/env python3
"""
Duplicate Finder
Scans multiple drives/folders, finds duplicate and near-duplicate files,
scores them by quality, and consolidates the best version.

Usage:
  python duplicate_finder.py --sources <dir> [<dir> ...] --output <dir> [options]

Default behavior (no --execute): scan + HTML report only, nothing is moved.

Examples:
  # Dry-run: scan two drives and write report
  python duplicate_finder.py --sources "D:\\Movies" "E:\\Backup" --output "M:\\Movies"

  # Execute: archive losers, move winners to output
  python duplicate_finder.py --sources "D:\\Movies" "E:\\Backup" --output "M:\\Movies" --execute --archive "Z:\\Dupes"

  # Execute: delete losers (use with caution)
  python duplicate_finder.py --sources "D:\\Movies" "E:\\Backup" --output "M:\\Movies" --execute --action delete
"""

import hashlib
import html
import json
import re
import shutil
import subprocess
import sys
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Optional dependencies
# ---------------------------------------------------------------------------

try:
    from PIL import Image, ExifTags
    PILLOW_AVAILABLE = True
except ImportError:
    PILLOW_AVAILABLE = False

try:
    import imagehash
    IMAGEHASH_AVAILABLE = True
except ImportError:
    IMAGEHASH_AVAILABLE = False


# ---------------------------------------------------------------------------
# ffprobe discovery (same as disk_drill_organizer.py)
# ---------------------------------------------------------------------------

def _find_ffprobe() -> Optional[str]:
    script_dir = Path(__file__).resolve().parent
    for candidate in sorted(script_dir.glob("ffmpeg*/bin/ffprobe.exe")):
        if candidate.is_file():
            return str(candidate)
    try:
        subprocess.run(["ffprobe", "-version"], capture_output=True, timeout=5)
        return "ffprobe"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

FFPROBE_BIN = _find_ffprobe()
FFPROBE_AVAILABLE = FFPROBE_BIN is not None


# ---------------------------------------------------------------------------
# Extension sets
# ---------------------------------------------------------------------------

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".heic", ".tiff", ".tif",
              ".webp", ".cr2", ".cr3", ".nef", ".arw", ".dng", ".orf", ".rw2"}
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".wmv", ".flv", ".mpg",
              ".mpeg", ".ts", ".m2ts", ".m1v", ".rm", ".rmvb", ".3gp", ".vob"}
AUDIO_EXTS = {".mp3", ".flac", ".wav", ".m4a", ".aac", ".wma", ".ogg", ".mpa", ".ra"}
DOCUMENT_EXTS = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt",
                 ".txt", ".html", ".htm", ".xml", ".csv"}

# Format quality rankings (higher = better)
PHOTO_FORMAT_RANK = {
    ".dng": 100, ".cr2": 95, ".cr3": 95, ".nef": 95, ".arw": 95,
    ".orf": 90, ".rw2": 90,        # RAW formats
    ".tiff": 70, ".tif": 70,       # Lossless
    ".png": 60,                    # Lossless compressed
    ".bmp": 55,                    # Uncompressed but no metadata
    ".heic": 45,                   # Apple HEIF — good quality but lossy
    ".webp": 40,                   # Lossy
    ".jpg": 30, ".jpeg": 30,       # Lossy
    ".gif": 10,                    # Very lossy / limited color
}

VIDEO_CODEC_RANK = {
    "av1": 100,
    "hevc": 90, "h265": 90, "h.265": 90,
    "h264": 70, "avc": 70, "h.264": 70,
    "vp9": 65, "vp8": 55,
    "mpeg4": 40, "xvid": 35, "divx": 35,
    "mpeg2video": 25, "vc1": 20,
    "wmv3": 15, "wmv2": 12, "wmv1": 10,
}

VIDEO_CONTAINER_RANK = {
    ".mkv": 100,   # Multi-track, subtitles, flexible
    ".mp4": 90,    # Widely compatible
    ".m4v": 85,
    ".mov": 80,
    ".m2ts": 75, ".ts": 70,    # Broadcast
    ".vob": 60,                # DVD
    ".avi": 50,
    ".wmv": 40,
    ".flv": 30,
    ".mpg": 25, ".mpeg": 25, ".m1v": 25,
    ".rm": 15, ".rmvb": 15,
    ".3gp": 10,
}

AUDIO_CODEC_RANK = {
    # Lossless
    "truehd": 100, "dts-hd ma": 95, "dts-hd": 90, "flac": 90, "pcm": 85,
    # Lossy high quality
    "dts": 70, "ac3": 65, "eac3": 70, "aac": 60,
    # Lossy medium
    "mp3": 40, "vorbis": 45, "opus": 55,
    # Poor
    "wmav2": 20, "wmav1": 15,
}

AUDIO_CHANNELS_SCORE = {8: 100, 7: 90, 6: 80, 4: 60, 2: 40, 1: 20}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass(eq=False)
class FileRecord:
    src: Path
    size_bytes: int
    ext: str                          # lowercase
    category: str                     # photo / video / audio / document / other
    source_priority: int              # index of --sources list (0 = highest priority)

    # Computed during analysis
    sha256: Optional[str] = None
    phash: Optional[str] = None       # perceptual hash (images only)

    # Photo metadata
    width: Optional[int] = None
    height: Optional[int] = None
    date_taken: Optional[datetime] = None
    camera_make: Optional[str] = None
    camera_model: Optional[str] = None
    has_gps: bool = False

    # Video metadata
    codec: Optional[str] = None
    bitrate_kbps: Optional[int] = None
    audio_codec: Optional[str] = None
    audio_channels: Optional[int] = None
    audio_bitrate_kbps: Optional[int] = None
    duration_sec: Optional[float] = None
    is_hdr: bool = False

    # Parsed media identity (for near-duplicate grouping)
    media_title: Optional[str] = None    # cleaned movie title or show name
    media_year: Optional[str] = None
    media_season: Optional[int] = None
    media_episode: Optional[int] = None

    # Scoring
    quality_score: float = 0.0

    @property
    def pixels(self) -> int:
        if self.width and self.height:
            return self.width * self.height
        return 0

    @property
    def resolution_label(self) -> str:
        if not self.width or not self.height:
            return "unknown"
        p = max(self.width, self.height)
        if p >= 3840: return "4K"
        if p >= 1920: return "1080p"
        if p >= 1280: return "720p"
        if p >= 720:  return "480p"
        return f"{self.width}x{self.height}"


@dataclass
class DupeGroup:
    group_id: str            # hash value or near-dupe key
    group_type: str          # "exact" | "near-video" | "near-photo"
    records: list            # list of FileRecord
    winner: Optional[object] = None   # FileRecord with highest score
    space_recoverable: int = 0        # bytes that can be freed


# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------

def read_photo_metadata(path: Path) -> dict:
    """Read EXIF metadata and actual dimensions from an image file."""
    result = {}
    if not PILLOW_AVAILABLE:
        return result
    try:
        with Image.open(path) as img:
            result["width"] = img.width
            result["height"] = img.height
            exif_raw = img._getexif() if hasattr(img, "_getexif") else None
            if not exif_raw:
                return result
            exif = {ExifTags.TAGS.get(k, k): v for k, v in exif_raw.items()}
            dt_str = exif.get("DateTimeOriginal") or exif.get("DateTime")
            if dt_str:
                try:
                    result["date_taken"] = datetime.strptime(dt_str, "%Y:%m:%d %H:%M:%S")
                except ValueError:
                    pass
            make = exif.get("Make", "").strip()
            model = exif.get("Model", "").strip()
            # Avoid "Apple Apple iPhone 11"
            if model.lower().startswith(make.lower()):
                make = ""
            result["camera_make"] = make or None
            result["camera_model"] = model or None
            gps = exif.get("GPSInfo")
            result["has_gps"] = bool(gps)
            # Try to read actual pixel dimensions from EXIF if PIL didn't get it
            px = exif.get("PixelXDimension")
            py = exif.get("PixelYDimension")
            if px and py:
                result["width"] = int(px)
                result["height"] = int(py)
    except Exception:
        pass
    return result


def read_video_metadata(path: Path) -> dict:
    """Run ffprobe and return a dict with codec, bitrate, audio, duration, dims."""
    result = {}
    if not FFPROBE_AVAILABLE:
        return result
    try:
        cmd = [
            FFPROBE_BIN, "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=15)
        data = json.loads(proc.stdout)
    except Exception:
        return result

    fmt = data.get("format", {})
    streams = data.get("streams", [])

    # Duration
    dur = fmt.get("duration") or next(
        (s.get("duration") for s in streams if s.get("duration")), None
    )
    if dur:
        try:
            result["duration_sec"] = float(dur)
        except ValueError:
            pass

    # Overall bitrate
    br = fmt.get("bit_rate")
    if br:
        try:
            result["bitrate_kbps"] = int(br) // 1000
        except ValueError:
            pass

    # Video stream
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video:
        result["codec"] = video.get("codec_name", "").lower()
        result["width"] = video.get("width")
        result["height"] = video.get("height")
        # HDR detection
        color_transfer = video.get("color_transfer", "").lower()
        color_space = video.get("color_space", "").lower()
        pix_fmt = video.get("pix_fmt", "").lower()
        result["is_hdr"] = any(x in color_transfer for x in ("smpte2084", "arib-std-b67")) \
                        or "bt2020" in color_space \
                        or "p010" in pix_fmt

    # Audio stream (best one)
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    if audio_streams:
        # Prefer the one with most channels
        best_audio = max(audio_streams, key=lambda s: s.get("channels", 0))
        result["audio_codec"] = best_audio.get("codec_name", "").lower()
        result["audio_channels"] = best_audio.get("channels")
        abr = best_audio.get("bit_rate")
        if abr:
            try:
                result["audio_bitrate_kbps"] = int(abr) // 1000
            except ValueError:
                pass

    # Date from format tags
    tags = fmt.get("tags", {})
    for key in ("creation_time", "date"):
        val = tags.get(key, "")
        if val:
            for fmt_str in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
                try:
                    result["date_taken"] = datetime.strptime(val[:26], fmt_str)
                    break
                except ValueError:
                    continue
            if "date_taken" in result:
                break

    return result


def compute_sha256(path: Path, chunk_size: int = 1 << 20) -> str:
    """Compute SHA-256 hash of a file."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(chunk_size), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def compute_phash(path: Path) -> Optional[str]:
    """Compute perceptual hash of an image. Returns None if unavailable."""
    if not IMAGEHASH_AVAILABLE or not PILLOW_AVAILABLE:
        return None
    try:
        with Image.open(path) as img:
            return str(imagehash.phash(img))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Quality scoring
# ---------------------------------------------------------------------------

def score_photo(rec: FileRecord) -> float:
    score = 0.0
    # Resolution (0–60 points)
    pixels = rec.pixels
    if pixels > 0:
        # Scale: 1MP=10, 4MP=20, 12MP=40, 24MP=50, 48MP=60
        import math
        score += min(60.0, math.log2(pixels / 1_000_000 + 1) * 15)
    # Format quality (0–20 points)
    score += PHOTO_FORMAT_RANK.get(rec.ext, 0) * 0.20
    # EXIF completeness bonus (0–10 points)
    if rec.date_taken:
        score += 4
    if rec.camera_model:
        score += 3
    if rec.has_gps:
        score += 3
    # File size as tiebreaker within same format (0–10 points, normalized to 50MB cap)
    score += min(10.0, rec.size_bytes / (50 * 1024 * 1024) * 10)
    return score


def score_video(rec: FileRecord) -> float:
    score = 0.0
    # Resolution (0–40 points)
    pixels = rec.pixels
    if pixels > 0:
        import math
        score += min(40.0, math.log2(pixels / 100_000 + 1) * 8)
    # Codec (0–20 points)
    codec_key = (rec.codec or "").lower()
    score += VIDEO_CODEC_RANK.get(codec_key, 0) * 0.20
    # Bitrate (0–15 points, capped at 40000 kbps)
    if rec.bitrate_kbps:
        score += min(15.0, rec.bitrate_kbps / 40000 * 15)
    # Container (0–10 points)
    score += VIDEO_CONTAINER_RANK.get(rec.ext, 0) * 0.10
    # Audio (0–10 points)
    audio_key = (rec.audio_codec or "").lower()
    for key, val in AUDIO_CODEC_RANK.items():
        if key in audio_key:
            score += val * 0.05
            break
    ch = rec.audio_channels or 0
    for threshold, ch_score in sorted(AUDIO_CHANNELS_SCORE.items(), reverse=True):
        if ch >= threshold:
            score += ch_score * 0.05
            break
    # HDR bonus (0–5 points)
    if rec.is_hdr:
        score += 5
    return score


def score_general(rec: FileRecord) -> float:
    """For audio, documents, and unknown files: larger = better."""
    import math
    return min(100.0, math.log2(rec.size_bytes / 1024 + 1) * 10)


def compute_quality_score(rec: FileRecord) -> float:
    if rec.category == "photo":
        return score_photo(rec)
    if rec.category == "video":
        return score_video(rec)
    return score_general(rec)


# ---------------------------------------------------------------------------
# Media identity parsing (reused from media_organizer logic, self-contained)
# ---------------------------------------------------------------------------

_JUNK_RE = re.compile(
    r"""(?:
        \b(?:2160p|1080[pi]|720p|480p|4[Kk]|UHD|HDR(?:10(?:\+)?)?|SDR|DoVi|DV)\b
        |\b(?:Blu-?Ray|BDRip|BD|BRRip|WEB-?DL|WEBRip|WEB|HDTV|DVDRip|DVD|HDRip
               |AMZN|NF|HULU|DSNP|ATVP)\b
        |\b(?:x\.?26[45]|H\.?26[45]|HEVC|AVC|XviD|DivX|VP9|AV1|REMUX)\b
        |\b(?:(?:E?|DD\+?)(?:AC3|DTS(?:-?HD)?(?:\s*MA)?)|TrueHD|Atmos
               |AAC(?:2\.0|5\.1|LC)?|FLAC|MP3|5\.1|7\.1|2\.0)\b
        |\b(?:PROPER|REPACK|EXTENDED|UNRATED|THEATRICAL|IMAX|3D|COMPLETE)\b
        |\b(?:YIFY|YTS|RARBG|FGT|NTG|ION10|GalaxyRG|MeGusta)\b
        |\[.*?\]|\((?!\d{4}\))\S[^)]*?\)
    )""",
    re.IGNORECASE | re.VERBOSE,
)
_YEAR_RE = re.compile(r"\b(19\d{2}|20[0-3]\d)\b")
_TV_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,2})")


def _clean_title(raw: str) -> str:
    s = re.sub(r"[._]", " ", raw)
    s = _JUNK_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def parse_media_identity(path: Path) -> dict:
    """Extract title/year/season/episode from a filename for near-dupe grouping."""
    stem = path.stem
    tv_m = _TV_RE.search(stem)
    if tv_m:
        before = stem[:tv_m.start()]
        return {
            "title": _clean_title(before),
            "season": int(tv_m.group(1)),
            "episode": int(tv_m.group(2)),
            "year": None,
        }
    year_m = _YEAR_RE.search(stem)
    year = year_m.group(1) if year_m else None
    before_year = stem[:year_m.start()] if year_m else stem
    title = _clean_title(before_year)
    return {"title": title, "year": year, "season": None, "episode": None}


# ---------------------------------------------------------------------------
# Per-file analysis
# ---------------------------------------------------------------------------

def analyze_file(path: Path, source_priority: int, use_perceptual: bool,
                 skip_video_meta: bool, skip_hash: bool = False) -> Optional[FileRecord]:
    """Classify and extract all metadata for one file. Returns None on error."""
    try:
        size = path.stat().st_size
    except OSError:
        return None

    if size == 0:
        return None

    ext = path.suffix.lower()

    if ext in PHOTO_EXTS:
        category = "photo"
    elif ext in VIDEO_EXTS:
        category = "video"
    elif ext in AUDIO_EXTS:
        category = "audio"
    elif ext in DOCUMENT_EXTS:
        category = "document"
    else:
        category = "other"

    rec = FileRecord(
        src=path,
        size_bytes=size,
        ext=ext,
        category=category,
        source_priority=source_priority,
    )

    # Hash (skip if --skip-hash; size-based grouping used instead)
    if not skip_hash:
        rec.sha256 = compute_sha256(path)

    # Photo metadata
    if category == "photo":
        meta = read_photo_metadata(path)
        rec.width = meta.get("width")
        rec.height = meta.get("height")
        rec.date_taken = meta.get("date_taken")
        rec.camera_make = meta.get("camera_make")
        rec.camera_model = meta.get("camera_model")
        rec.has_gps = meta.get("has_gps", False)
        if use_perceptual:
            rec.phash = compute_phash(path)

    # Video metadata
    elif category == "video" and not skip_video_meta:
        meta = read_video_metadata(path)
        rec.width = meta.get("width")
        rec.height = meta.get("height")
        rec.codec = meta.get("codec")
        rec.bitrate_kbps = meta.get("bitrate_kbps")
        rec.audio_codec = meta.get("audio_codec")
        rec.audio_channels = meta.get("audio_channels")
        rec.audio_bitrate_kbps = meta.get("audio_bitrate_kbps")
        rec.duration_sec = meta.get("duration_sec")
        rec.date_taken = meta.get("date_taken")
        rec.is_hdr = meta.get("is_hdr", False)

        # Parse identity for near-dupe grouping
        identity = parse_media_identity(path)
        rec.media_title = identity["title"]
        rec.media_year = identity["year"]
        rec.media_season = identity["season"]
        rec.media_episode = identity["episode"]

    rec.quality_score = compute_quality_score(rec)
    return rec


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

SKIP_DIRS = {".git", "__pycache__", "$RECYCLE.BIN", "System Volume Information"}

def collect_paths(sources: list[Path]) -> list[tuple[Path, int]]:
    """Walk all source dirs, return (path, priority_index) pairs."""
    result = []
    for priority, src in enumerate(sources):
        for item in src.rglob("*"):
            if item.is_file() and item.parent.name not in SKIP_DIRS:
                result.append((item, priority))
    return result


def scan(sources: list[Path], workers: int, use_perceptual: bool,
         skip_video_meta: bool, skip_hash: bool = False) -> list[FileRecord]:
    """Scan all sources in parallel and return FileRecord list."""
    all_paths = collect_paths(sources)
    total = len(all_paths)
    print(f"Found {total:,} files across {len(sources)} source(s). Analyzing...")

    records: list[Optional[FileRecord]] = [None] * total
    completed = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(analyze_file, path, priority, use_perceptual, skip_video_meta, skip_hash): i
            for i, (path, priority) in enumerate(all_paths)
        }
        for future in as_completed(futures):
            idx = futures[future]
            try:
                records[idx] = future.result()
            except Exception:
                pass
            completed += 1
            if completed % 20 == 0 or completed == total:
                print(f"  {completed:,}/{total:,} files analyzed...", end="\r")

    print()
    return [r for r in records if r is not None]


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

PHASH_THRESHOLD = 10  # Hamming distance for near-duplicate images


def group_duplicates(records: list[FileRecord]) -> list[DupeGroup]:
    """
    Return duplicate groups. Exact matches first (by hash), then near-dupes.
    Files that are unique are not included.
    """
    groups: list[DupeGroup] = []

    # --- Phase 1: Exact duplicates by SHA-256 (or file size when --skip-hash) ---
    # When hashes are available, group by hash (definitive).
    # When skipped, group by size — for large video files this is a reliable proxy
    # since the probability of two different files sharing the exact same byte count is
    # negligible.  Small files (<1 MB) are excluded from size-only grouping to avoid
    # false positives on things like empty subtitle files or tiny metadata files.
    SIZE_ONLY_MIN = 1 * 1024 * 1024  # 1 MB minimum for size-only matching

    hash_map: dict[str, list[FileRecord]] = {}
    for rec in records:
        if rec.sha256:
            hash_map.setdefault(rec.sha256, []).append(rec)
        elif rec.size_bytes >= SIZE_ONLY_MIN:
            # Use size as key (prefixed so it never collides with a real hash)
            key = f"size:{rec.size_bytes}"
            hash_map.setdefault(key, []).append(rec)

    exact_hashes: set[str] = set()
    for sha, group_recs in hash_map.items():
        if len(group_recs) > 1:
            exact_hashes.add(sha)
            groups.append(DupeGroup(
                group_id=sha,
                group_type="exact",
                records=group_recs,
            ))

    # Records already in an exact group
    exact_paths: set[Path] = {
        rec.src for g in groups for rec in g.records
    }

    # --- Phase 2: Near-duplicate videos (same identity + similar duration) ---
    remaining_videos = [
        r for r in records
        if r.category == "video" and r.src not in exact_paths and r.media_title
    ]

    # Group by (title, year, season, episode) key
    video_identity_map: dict[tuple, list[FileRecord]] = {}
    for rec in remaining_videos:
        if rec.media_season is not None:
            # TV episode
            key = ("tv", rec.media_title, rec.media_season, rec.media_episode)
        else:
            # Movie
            key = ("movie", rec.media_title, rec.media_year)
        video_identity_map.setdefault(key, []).append(rec)

    for key, vids in video_identity_map.items():
        if len(vids) < 2:
            continue
        # Further sub-group by duration similarity (±5%)
        used = [False] * len(vids)
        for i in range(len(vids)):
            if used[i]:
                continue
            cluster = [vids[i]]
            d_i = vids[i].duration_sec or 0
            for j in range(i + 1, len(vids)):
                if used[j]:
                    continue
                d_j = vids[j].duration_sec or 0
                # Same if both unknown or within 5% of each other
                if d_i == 0 and d_j == 0:
                    cluster.append(vids[j])
                    used[j] = True
                elif d_i > 0 and d_j > 0 and abs(d_i - d_j) / max(d_i, d_j) < 0.05:
                    cluster.append(vids[j])
                    used[j] = True
            used[i] = True
            if len(cluster) > 1:
                groups.append(DupeGroup(
                    group_id=str(key),
                    group_type="near-video",
                    records=cluster,
                ))

    # --- Phase 3: Near-duplicate images by perceptual hash ---
    remaining_photos = [
        r for r in records
        if r.category == "photo" and r.src not in exact_paths and r.phash
    ]

    if remaining_photos and IMAGEHASH_AVAILABLE:
        import imagehash as ih
        used_photos: set[int] = set()
        for i, rec_i in enumerate(remaining_photos):
            if i in used_photos:
                continue
            cluster = [rec_i]
            h_i = ih.hex_to_hash(rec_i.phash)
            for j in range(i + 1, len(remaining_photos)):
                if j in used_photos:
                    continue
                rec_j = remaining_photos[j]
                h_j = ih.hex_to_hash(rec_j.phash)
                if (h_i - h_j) <= PHASH_THRESHOLD:
                    cluster.append(rec_j)
                    used_photos.add(j)
            used_photos.add(i)
            if len(cluster) > 1:
                groups.append(DupeGroup(
                    group_id=rec_i.phash,
                    group_type="near-photo",
                    records=cluster,
                ))

    # --- Score and pick winners ---
    for group in groups:
        group.records.sort(key=lambda r: (
            -r.quality_score,       # highest quality first
            r.source_priority,      # then by source priority (0 = best)
            -r.size_bytes,          # then larger file
        ))
        group.winner = group.records[0]
        group.space_recoverable = sum(r.size_bytes for r in group.records[1:])

    return groups


# ---------------------------------------------------------------------------
# Execute: archive / delete losers
# ---------------------------------------------------------------------------

def execute_dedup(groups: list[DupeGroup], output: Path, archive: Optional[Path],
                  action: str) -> None:
    """
    action: "archive" | "delete" | "report"
    Winners are moved to output (preserving relative structure from their source root).
    Losers are archived or deleted.
    """
    if action == "report":
        print("Dry-run mode — nothing moved. Pass --execute to apply changes.")
        return

    print(f"\nExecuting ({action} losers)...")
    output.mkdir(parents=True, exist_ok=True)
    if archive and action == "archive":
        archive.mkdir(parents=True, exist_ok=True)

    moved_winners = 0
    handled_losers = 0
    total_groups = len(groups)

    for group_idx, group in enumerate(groups, 1):
        winner = group.winner
        if not winner:
            continue

        print(f"  [{group_idx}/{total_groups}] {winner.src.name} "
              f"({_fmt_bytes(winner.size_bytes)})", flush=True)

        # Move winner to output dir (flatten into a single dir per category)
        dest_dir = output / winner.category
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / winner.src.name
        # Collision-safe rename
        stem, suffix = dest.stem, dest.suffix
        counter = 1
        while dest.exists():
            dest = dest_dir / f"{stem}_{counter}{suffix}"
            counter += 1

        try:
            shutil.move(str(winner.src), str(dest))
            moved_winners += 1
            print(f"    KEPT   -> {dest}", flush=True)
        except Exception as e:
            print(f"  [ERROR] Could not move winner {winner.src}: {e}")

        # Handle losers
        for loser in group.records[1:]:
            if not loser.src.exists():
                continue
            if action == "archive" and archive:
                arc_dir = archive / loser.category
                arc_dir.mkdir(parents=True, exist_ok=True)
                arc_dest = arc_dir / loser.src.name
                s2, suf2 = arc_dest.stem, arc_dest.suffix
                c2 = 1
                while arc_dest.exists():
                    arc_dest = arc_dir / f"{s2}_{c2}{suf2}"
                    c2 += 1
                try:
                    shutil.move(str(loser.src), str(arc_dest))
                    handled_losers += 1
                    print(f"    ARCHIVED -> {arc_dest}", flush=True)
                except Exception as e:
                    print(f"  [ERROR] Could not archive loser {loser.src}: {e}")
            elif action == "delete":
                try:
                    loser.src.unlink()
                    handled_losers += 1
                    print(f"    DELETED  -> {loser.src}", flush=True)
                except Exception as e:
                    print(f"  [ERROR] Could not delete {loser.src}: {e}")

    print(f"  Winners moved: {moved_winners}")
    print(f"  Losers {'archived' if action == 'archive' else 'deleted'}: {handled_losers}")


# ---------------------------------------------------------------------------
# HTML Report
# ---------------------------------------------------------------------------

def _fmt_bytes(b: int) -> str:
    if b >= 1 << 30:
        return f"{b / (1 << 30):.2f} GB"
    if b >= 1 << 20:
        return f"{b / (1 << 20):.1f} MB"
    if b >= 1 << 10:
        return f"{b / (1 << 10):.0f} KB"
    return f"{b} B"


def _fmt_duration(sec: Optional[float]) -> str:
    if not sec:
        return "—"
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def write_report(groups: list[DupeGroup], records: list[FileRecord],
                 report_path: Path, sources: list[Path], action: str,
                 executed: bool) -> None:
    total_files = len(records)
    total_groups = len(groups)
    total_recoverable = sum(g.space_recoverable for g in groups)
    exact_count = sum(1 for g in groups if g.group_type == "exact")
    near_video_count = sum(1 for g in groups if g.group_type == "near-video")
    near_photo_count = sum(1 for g in groups if g.group_type == "near-photo")

    def row_class(rec, group):
        return "winner" if rec is group.winner else "loser"

    def score_badge(score):
        color = "#4caf50" if score >= 70 else "#ff9800" if score >= 40 else "#f44336"
        return f'<span class="badge" style="background:{color}">{score:.1f}</span>'

    def video_detail(rec):
        parts = []
        if rec.resolution_label != "unknown":
            parts.append(rec.resolution_label)
        if rec.codec:
            parts.append(rec.codec.upper())
        if rec.bitrate_kbps:
            parts.append(f"{rec.bitrate_kbps} kbps")
        if rec.audio_codec:
            ch = f" {rec.audio_channels}ch" if rec.audio_channels else ""
            parts.append(f"{rec.audio_codec.upper()}{ch}")
        if rec.is_hdr:
            parts.append("HDR")
        if rec.duration_sec:
            parts.append(_fmt_duration(rec.duration_sec))
        return " · ".join(parts) if parts else "—"

    def photo_detail(rec):
        parts = []
        if rec.width and rec.height:
            parts.append(f"{rec.width}×{rec.height}")
        if rec.camera_model:
            model = rec.camera_model
            if rec.camera_make:
                model = f"{rec.camera_make} {model}"
            parts.append(model)
        if rec.date_taken:
            parts.append(rec.date_taken.strftime("%Y-%m-%d"))
        if rec.has_gps:
            parts.append("GPS")
        return " · ".join(parts) if parts else "—"

    rows_html = ""
    for idx, group in enumerate(groups, 1):
        type_badge = {
            "exact": '<span class="tag exact">Exact</span>',
            "near-video": '<span class="tag near-video">Near·Video</span>',
            "near-photo": '<span class="tag near-photo">Near·Photo</span>',
        }.get(group.group_type, group.group_type)

        rows_html += f"""
        <tr class="group-header">
          <td colspan="6">
            Group #{idx} &nbsp;{type_badge}&nbsp;
            <span class="recover">{_fmt_bytes(group.space_recoverable)} recoverable</span>
            &nbsp;·&nbsp; {len(group.records)} files
          </td>
        </tr>"""

        for rec in group.records:
            is_winner = rec is group.winner
            rc = "winner" if is_winner else "loser"
            crown = " 👑" if is_winner else ""
            detail = video_detail(rec) if rec.category == "video" else photo_detail(rec) if rec.category == "photo" else "—"
            rows_html += f"""
        <tr class="{rc}">
          <td>{html.escape(str(rec.src))}{crown}</td>
          <td>{rec.category}</td>
          <td>{_fmt_bytes(rec.size_bytes)}</td>
          <td>{detail}</td>
          <td>{score_badge(rec.quality_score)}</td>
          <td>P{rec.source_priority + 1}</td>
        </tr>"""

    status_label = "EXECUTED" if executed else "DRY-RUN"
    status_color = "#4caf50" if executed else "#ff9800"

    report_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Duplicate Finder Report</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #1a1a2e; color: #e0e0e0; font-family: 'Segoe UI', sans-serif; font-size: 13px; }}
  h1 {{ color: #90caf9; padding: 20px; font-size: 22px; }}
  .meta {{ padding: 0 20px 16px; color: #9e9e9e; font-size: 12px; }}
  .stats {{ display: flex; gap: 12px; flex-wrap: wrap; padding: 0 20px 20px; }}
  .stat {{ background: #16213e; border-radius: 8px; padding: 14px 20px; min-width: 140px; }}
  .stat .val {{ font-size: 26px; font-weight: bold; color: #90caf9; }}
  .stat .lbl {{ font-size: 11px; color: #9e9e9e; margin-top: 2px; }}
  .status {{ display: inline-block; padding: 3px 10px; border-radius: 4px; font-weight: bold;
             background: {status_color}22; color: {status_color}; font-size: 12px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ background: #0f3460; color: #90caf9; padding: 8px 10px; text-align: left; font-size: 12px; position: sticky; top: 0; }}
  td {{ padding: 7px 10px; border-bottom: 1px solid #1e1e3a; vertical-align: top; word-break: break-all; }}
  tr.group-header td {{ background: #0f3460aa; color: #90caf9; font-weight: 600; padding: 10px; word-break: normal; }}
  tr.winner td:first-child {{ border-left: 3px solid #4caf50; }}
  tr.loser td:first-child {{ border-left: 3px solid #f44336; }}
  tr.winner {{ background: #1b3a1b; }}
  tr.loser  {{ background: #3a1b1b; }}
  .badge {{ display: inline-block; padding: 2px 7px; border-radius: 10px; color: #fff; font-size: 11px; }}
  .tag {{ display: inline-block; padding: 1px 7px; border-radius: 4px; font-size: 11px; font-weight: 600; }}
  .tag.exact {{ background: #e91e6322; color: #f06292; }}
  .tag.near-video {{ background: #1565c022; color: #64b5f6; }}
  .tag.near-photo {{ background: #2e7d3222; color: #81c784; }}
  .recover {{ color: #ffb74d; font-size: 12px; }}
  .sources {{ padding: 0 20px 20px; color: #9e9e9e; font-size: 12px; }}
  .sources span {{ display: inline-block; margin: 2px 6px 2px 0; background: #16213e; padding: 2px 8px; border-radius: 4px; }}
</style>
</head>
<body>
<h1>Duplicate Finder Report</h1>
<div class="meta">
  Generated {datetime.now().strftime("%Y-%m-%d %H:%M")} &nbsp;·&nbsp;
  <span class="status">{status_label}</span> &nbsp;·&nbsp;
  Action: <b>{action}</b>
</div>

<div class="sources">
  Sources (priority order):
  {"".join(f'<span>P{i+1}: {html.escape(str(s))}</span>' for i, s in enumerate(sources))}
</div>

<div class="stats">
  <div class="stat"><div class="val">{total_files:,}</div><div class="lbl">Files scanned</div></div>
  <div class="stat"><div class="val">{total_groups:,}</div><div class="lbl">Duplicate groups</div></div>
  <div class="stat"><div class="val">{_fmt_bytes(total_recoverable)}</div><div class="lbl">Space recoverable</div></div>
  <div class="stat"><div class="val">{exact_count}</div><div class="lbl">Exact duplicates</div></div>
  <div class="stat"><div class="val">{near_video_count}</div><div class="lbl">Near-dupe videos</div></div>
  <div class="stat"><div class="val">{near_photo_count}</div><div class="lbl">Near-dupe photos</div></div>
  <div class="stat"><div class="val">{"✓" if PILLOW_AVAILABLE else "✗"}</div><div class="lbl">Pillow (EXIF)</div></div>
  <div class="stat"><div class="val">{"✓" if FFPROBE_AVAILABLE else "✗"}</div><div class="lbl">ffprobe (video)</div></div>
  <div class="stat"><div class="val">{"✓" if IMAGEHASH_AVAILABLE else "✗"}</div><div class="lbl">imagehash (pHash)</div></div>
</div>

<table>
  <thead>
    <tr>
      <th>File Path</th>
      <th>Type</th>
      <th>Size</th>
      <th>Quality Details</th>
      <th>Score</th>
      <th>Src</th>
    </tr>
  </thead>
  <tbody>
    {rows_html if rows_html else '<tr><td colspan="6" style="text-align:center;padding:40px;color:#9e9e9e">No duplicates found.</td></tr>'}
  </tbody>
</table>
</body>
</html>"""

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report_html, encoding="utf-8")
    print(f"\nReport written -> {report_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Find and deduplicate files across multiple drives/folders.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--sources", nargs="+", required=True, metavar="DIR",
        help="Folders to scan, in priority order (first = highest priority for tie-breaking).",
    )
    parser.add_argument(
        "--output", required=True, metavar="DIR",
        help="Where to move winners when --execute is used.",
    )
    parser.add_argument(
        "--action", choices=["archive", "delete", "report"], default="archive",
        help="What to do with losing duplicates: archive (default), delete, or report-only.",
    )
    parser.add_argument(
        "--archive", metavar="DIR", default=None,
        help="Folder to move losers into when --action archive (default: <output>/duplicates).",
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="Actually move/delete files. Without this flag, only a report is written (dry-run).",
    )
    parser.add_argument(
        "--types", nargs="+",
        choices=["photos", "videos", "audio", "documents", "other", "all"],
        default=["all"],
        help="Which file types to include in deduplication (default: all).",
    )
    parser.add_argument(
        "--perceptual", action="store_true",
        help="Enable perceptual hashing for images (requires: pip install imagehash).",
    )
    parser.add_argument(
        "--skip-video-meta", action="store_true",
        help="Skip ffprobe for videos. Much faster but no codec/bitrate scoring.",
    )
    parser.add_argument(
        "--skip-hash", action="store_true",
        help="Skip SHA-256 hashing. Uses file size for exact matching instead — "
             "much faster for large files. Files under 1 MB are excluded from "
             "size-only matching to avoid false positives.",
    )
    parser.add_argument(
        "--hash", choices=["sha256", "md5"], default="sha256",
        help="Hash algorithm for exact matching (default: sha256).",
    )
    parser.add_argument(
        "--workers", type=int, default=8, metavar="N",
        help="Parallel scan threads (default: 8).",
    )
    parser.add_argument(
        "--report", metavar="FILE", default=None,
        help="Path for the HTML report (default: <output>/duplicate_report.html).",
    )
    parser.add_argument(
        "--min-size", type=int, default=4096, metavar="BYTES",
        help="Ignore files smaller than this many bytes (default: 4096 = 4 KB).",
    )

    args = parser.parse_args()

    sources = [Path(s) for s in args.sources]
    output = Path(args.output)
    archive_dir = Path(args.archive) if args.archive else output / "duplicates"
    report_path = Path(args.report) if args.report else output / "duplicate_report.html"

    # Validate sources
    for src in sources:
        if not src.exists():
            print(f"[ERROR] Source does not exist: {src}")
            sys.exit(1)

    # Warn on delete
    if args.execute and args.action == "delete":
        print("WARNING: --action delete will permanently remove loser files.")
        confirm = input("Type YES to confirm: ")
        if confirm.strip() != "YES":
            print("Aborted.")
            sys.exit(0)

    # Warn on execute + archive
    if args.execute and args.action == "archive":
        print(f"Winners  → {output}")
        print(f"Losers   → {archive_dir}")
        confirm = input("Type YES to proceed: ")
        if confirm.strip() != "YES":
            print("Aborted.")
            sys.exit(0)

    print(f"\nSources ({len(sources)}):")
    for i, s in enumerate(sources):
        print(f"  P{i+1}: {s}")
    print(f"Output:  {output}")
    print(f"Action:  {args.action}")
    print(f"Mode:    {'EXECUTE' if args.execute else 'DRY-RUN'}\n")

    # Scan
    if args.skip_hash:
        print("Note: --skip-hash enabled — using file size for exact duplicate matching.")
    all_records = scan(sources, args.workers, args.perceptual, args.skip_video_meta, args.skip_hash)

    # Filter by type
    type_map = {
        "photos": "photo", "videos": "video", "audio": "audio",
        "documents": "document", "other": "other",
    }
    if "all" not in args.types:
        wanted = {type_map[t] for t in args.types if t in type_map}
        all_records = [r for r in all_records if r.category in wanted]

    # Filter by min size
    all_records = [r for r in all_records if r.size_bytes >= args.min_size]

    print(f"\n{len(all_records):,} files eligible for deduplication.")

    # Group
    print("Grouping duplicates...")
    groups = group_duplicates(all_records)

    total_recoverable = sum(g.space_recoverable for g in groups)
    print(f"Found {len(groups)} duplicate group(s) ({_fmt_bytes(total_recoverable)} recoverable).")

    # Print summary to console
    if groups:
        print("\nTop duplicate groups:")
        for g in sorted(groups, key=lambda x: -x.space_recoverable)[:10]:
            winner = g.winner
            w_label = winner.src.name if winner else "?"
            print(f"  [{g.group_type}] {w_label!r} — {len(g.records)} copies, "
                  f"{_fmt_bytes(g.space_recoverable)} recoverable")

    # Execute
    if args.execute:
        execute_dedup(groups, output, archive_dir, args.action)
    else:
        print("\nDry-run: no files moved. Use --execute to apply.")

    # Report (always)
    write_report(groups, all_records, report_path, sources, args.action,
                 executed=args.execute)


if __name__ == "__main__":
    import signal
    def _handle_sigterm(signum, frame):
        print("\n[INTERRUPTED] Shutdown signal received — stopping cleanly. "
              "Re-run with the same config to resume; already-processed files will be skipped.", flush=True)
        sys.exit(0)
    signal.signal(signal.SIGTERM, _handle_sigterm)
    main()
