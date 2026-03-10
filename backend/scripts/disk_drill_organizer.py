#!/usr/bin/env python3
"""
Disk Drill Recovery Organizer
Processes the output of Disk Drill's "reconstructed" folder structure,
filters junk, and reorganizes files into a human-readable layout.

Usage:
  python disk_drill_organizer.py <input_dir> <output_dir> [options]

  --min-photo-px N    Minimum pixel dimension (shorter side) for photos (default: 800)
  --min-video-sec N   Minimum video duration in seconds to keep (default: 30)
  --execute           Actually copy/move files (default is dry-run + report)
  --move              Move instead of copy (faster, saves space; use only on non-originals)
  --report FILE       Write HTML report to this file (default: report.html in output_dir)

Examples:
  # Preview what would happen
  python disk_drill_organizer.py F:/reconstructed G:/organized

  # Execute (copy files, write report)
  python disk_drill_organizer.py F:/reconstructed G:/organized --execute

  # Move files instead of copying
  python disk_drill_organizer.py F:/reconstructed G:/organized --execute --move
"""

import os
import re
import sys
import shutil
import argparse
import html
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

# Optional EXIF support via Pillow
try:
    from PIL import Image, ExifTags
    from PIL.ExifTags import TAGS, GPSTAGS
    PILLOW_AVAILABLE = True
except ImportError:
    PILLOW_AVAILABLE = False

# Resolve ffprobe: check local ./ffmpeg*/bin/ first, then fall back to PATH
def _find_ffprobe() -> Optional[str]:
    # Look for ffprobe.exe next to this script (any ffmpeg-* folder)
    script_dir = Path(__file__).resolve().parent
    for candidate in sorted(script_dir.glob("ffmpeg*/bin/ffprobe.exe")):
        if candidate.is_file():
            return str(candidate)
    # Fall back to system PATH
    try:
        subprocess.run(["ffprobe", "-version"], capture_output=True, timeout=5)
        return "ffprobe"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

FFPROBE_BIN = _find_ffprobe()
FFPROBE_AVAILABLE = FFPROBE_BIN is not None


# ---------------------------------------------------------------------------
# Extension categories
# ---------------------------------------------------------------------------

PHOTO_EXTS   = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".heic", ".tiff", ".tif", ".webp"}
VIDEO_EXTS   = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".wmv", ".flv", ".mpg", ".mpeg",
                ".m1v", ".ts", ".rm", ".rmvb", ".3gp"}
AUDIO_EXTS   = {".mp3", ".flac", ".wav", ".m4a", ".aac", ".wma", ".ogg", ".mpa", ".ra"}
DOCUMENT_EXTS = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".txt",
                 ".html", ".htm", ".xml", ".csv"}

# Extensions that are almost always recoverable junk from Disk Drill
JUNK_EXTS = {
    ".pss",   # PlayStation 2 game video
    ".bik",   # Bink game cutscene video
    ".fh3",   # FreeHand drawing (obsolete)
    ".swf",   # Adobe Flash (dead format)
    ".mlv",   # Magic Lantern Video (camera raw — user can override)
    ".plist", # Apple property list (system file)
    ".db",    # Database/thumbnail cache
    ".db3",   # SQLite database
    ".itl",   # iTunes Library (binary, not useful)
    ".lnk",   # Windows shortcut
    ".tmp",   # Temp file
}

# Known junk filenames regardless of extension
JUNK_FILENAMES = {"thumbs.db", "desktop.ini", ".ds_store", "picasa.ini"}

# Source tags that typically indicate web/app assets rather than personal photos
ASSET_SOURCES = {
    "adobe photoshop",
    "adobe fireworks",
    "adobe illustrator",
    "paint.net",
    "gimp",
    "microsoft office",
    "web browser",
    "safari",
    "chrome",
    "firefox",
}


# ---------------------------------------------------------------------------
# Filename parsing for Disk Drill naming convention
#
# Pattern: "SOURCE WxH_SEQNUM.ext"   (photos)
#          "SOURCE WxH DURATIONs_SEQNUM.ext"  (videos)
#          "SOURCE WxH HHhMMmSSs_SEQNUM.ext"  (videos, long form)
# ---------------------------------------------------------------------------

# Matches dimensions like 1920x1080, 4032x3024, 13632x2988
DIM_RE = re.compile(r"\b(\d{2,5})x(\d{2,5})\b", re.IGNORECASE)

# Matches durations like:
#   51m16s, 1h20m, 01h00m, 22m03s, 5s, 1h20m30s
DURATION_RE = re.compile(
    r"\b(?:(\d{1,3})h)?(?:(\d{1,3})m)?(?:(\d{1,3})s)?\b",
    re.IGNORECASE,
)
# More specific duration pattern to avoid false matches
DURATION_STRICT_RE = re.compile(
    r"\b(?:(\d{1,3})h)?(\d{1,3})m(\d{2})s\b"   # e.g., 51m16s, 01h00m00s
    r"|\b(\d{1,3})h(\d{2})m\b"                   # e.g., 1h20m
    r"|\b(\d+)m\b(?!\s*\d)",                      # e.g., 42m  (standalone)
    re.IGNORECASE,
)

# Sequence number suffix that Disk Drill appends: _000001
SEQ_RE = re.compile(r"_(\d{5,6})$")


def parse_duration_seconds(stem: str) -> Optional[int]:
    """Extract duration in seconds from a Disk Drill video filename stem."""
    # Try full pattern: 01h20m30s
    m = re.search(r"\b(\d{1,3})h(\d{2})m(\d{2})s\b", stem, re.IGNORECASE)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    # 51m16s
    m = re.search(r"\b(\d{1,3})m(\d{2})s\b", stem, re.IGNORECASE)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    # 1h20m  (no seconds)
    m = re.search(r"\b(\d{1,3})h(\d{2})m\b", stem, re.IGNORECASE)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60
    # 42m (standalone minutes)
    m = re.search(r"\b(\d+)m\b", stem, re.IGNORECASE)
    if m:
        return int(m.group(1)) * 60
    return None


def parse_dimensions(stem: str) -> tuple[Optional[int], Optional[int]]:
    """Return (width, height) from filename stem, or (None, None)."""
    m = DIM_RE.search(stem)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


def parse_source_device(stem: str) -> str:
    """
    Extract the 'source' prefix from Disk Drill filename.
    Everything before the dimensions or duration tag.
    """
    # Remove sequence suffix first
    clean = SEQ_RE.sub("", stem).strip()
    # Find first dimension or duration tag
    pos = len(clean)
    m = DIM_RE.search(clean)
    if m:
        pos = min(pos, m.start())
    m = DURATION_STRICT_RE.search(clean)
    if m:
        pos = min(pos, m.start())
    source = clean[:pos].strip()
    return source


# ---------------------------------------------------------------------------
# Device category mapping
# ---------------------------------------------------------------------------

def categorize_device(source: str) -> str:
    """Map a raw source string to a human-readable category folder name."""
    s = source.lower()
    if not s:
        return "Unknown"

    # Apple devices
    if "iphone" in s:
        # Extract model number if present
        m = re.search(r"iphone\s*([\w\s]+?)(?:\s+\d{3,5}x|\s*$)", s)
        model = m.group(1).strip().title() if m else ""
        return f"iPhone {model}".strip() if model else "iPhone"
    if "ipad" in s:
        return "iPad"
    if "ipod" in s:
        return "iPod"
    if "apple" in s:
        return "Apple"

    # Android brands
    for brand in ("samsung", "galaxy"):
        if brand in s:
            return "Samsung"
    for brand in ("pixel", "nexus"):
        if brand in s:
            return "Google Pixel"
    if "huawei" in s:
        return "Huawei"
    if "xiaomi" in s or "redmi" in s:
        return "Xiaomi"
    if "oneplus" in s:
        return "OnePlus"
    if "lg " in s or s.startswith("lg"):
        return "LG"
    if "motorola" in s or "moto " in s:
        return "Motorola"

    # DSLR / mirrorless cameras
    for brand in ("canon", "nikon", "sony", "fujifilm", "fuji", "olympus",
                  "pentax", "panasonic", "leica", "hasselblad"):
        if brand in s:
            return brand.title() + " Camera"

    # GoPro / action cameras
    if "gopro" in s:
        return "GoPro"
    if "dji" in s:
        return "DJI Drone"

    # Editing / graphics software (usually web assets, not personal photos)
    for app in ASSET_SOURCES:
        if app in s:
            app_name = app.title()
            return f"_Assets ({app_name})"

    # Screen captures
    if any(x in s for x in ("screenshot", "screen shot", "screen capture")):
        return "Screenshots"

    # Generic / unknown
    return "Other"


def categorize_video_source(source: str, duration_sec: Optional[int]) -> str:
    """Map video source + duration to an output folder."""
    s = source.lower()

    if "iphone" in s or "ipad" in s:
        return "iPhone"
    for brand in ("samsung", "galaxy", "pixel", "huawei"):
        if brand in s:
            return "Android"
    if "gopro" in s:
        return "GoPro"
    if "dji" in s:
        return "DJI Drone"
    for brand in ("canon", "nikon", "sony", "fuji", "panasonic"):
        if brand in s:
            return brand.title() + " Camera"

    # Classify generic/unknown by resolution and duration
    if duration_sec is not None:
        if duration_sec >= 3600:
            return "Long Clips (1h+)"
        if duration_sec >= 600:
            return "Medium Clips (10-60min)"
        if duration_sec >= 60:
            return "Short Clips (1-10min)"
        return "Very Short Clips"

    return "Other"


# ---------------------------------------------------------------------------
# File classification
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# EXIF / metadata extraction
# ---------------------------------------------------------------------------

def _exif_tag_name(tag_id):
    return TAGS.get(tag_id, str(tag_id))


def read_photo_exif(path: Path) -> dict:
    """
    Extract useful EXIF metadata from a photo.
    Returns dict with keys (all optional):
      date_taken  : datetime object
      camera_make : str
      camera_model: str
      gps_lat     : float
      gps_lon     : float
    """
    result = {}
    if not PILLOW_AVAILABLE:
        return result
    try:
        with Image.open(path) as img:
            exif_data = img._getexif()
            if not exif_data:
                return result
            named = {TAGS.get(k, k): v for k, v in exif_data.items()}

            # Date taken
            for date_field in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
                raw = named.get(date_field)
                if raw:
                    try:
                        result["date_taken"] = datetime.strptime(raw, "%Y:%m:%d %H:%M:%S")
                        break
                    except ValueError:
                        pass

            # Camera
            make = (named.get("Make") or "").strip()
            model = (named.get("Model") or "").strip()
            if make:
                result["camera_make"] = make
            if model:
                # Remove redundant make prefix from model (e.g. "Apple iPhone 7" → keep as-is)
                result["camera_model"] = model

            # GPS
            gps_info = named.get("GPSInfo")
            if gps_info:
                gps = {GPSTAGS.get(k, k): v for k, v in gps_info.items()}
                try:
                    def dms_to_deg(dms):
                        d, m, s = dms
                        return float(d) + float(m) / 60 + float(s) / 3600
                    lat = dms_to_deg(gps["GPSLatitude"])
                    lon = dms_to_deg(gps["GPSLongitude"])
                    if gps.get("GPSLatitudeRef") == "S":
                        lat = -lat
                    if gps.get("GPSLongitudeRef") == "W":
                        lon = -lon
                    result["gps_lat"] = lat
                    result["gps_lon"] = lon
                except (KeyError, TypeError, ZeroDivisionError):
                    pass
    except Exception:
        pass
    return result


def read_video_metadata(path: Path) -> dict:
    """
    Use ffprobe to extract video metadata.
    Returns dict with keys (all optional):
      date_taken   : datetime
      camera_make  : str
      camera_model : str
      duration_sec : int
      width        : int
      height       : int
    """
    result = {}
    if not FFPROBE_AVAILABLE:
        return result
    try:
        cmd = [
            FFPROBE_BIN, "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", str(path)
        ]
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=5, text=True)
        except subprocess.TimeoutExpired:
            result["_timed_out"] = True
            return result
        data = json.loads(out.stdout)

        # Duration from format
        fmt = data.get("format", {})
        dur = fmt.get("duration")
        if dur:
            try:
                result["duration_sec"] = int(float(dur))
            except ValueError:
                pass

        # Tags (creation_time, make, model)
        tags = fmt.get("tags", {})
        # Normalize tag keys to lowercase
        tags = {k.lower(): v for k, v in tags.items()}

        for date_field in ("creation_time", "com.apple.quicktime.creationdate"):
            raw = tags.get(date_field)
            if raw:
                for fmt_str in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                                "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                    try:
                        result["date_taken"] = datetime.strptime(raw[:26], fmt_str)
                        break
                    except ValueError:
                        continue
                if "date_taken" in result:
                    break

        for make_field in ("com.apple.quicktime.make", "make", "android/manufacturer"):
            if make_field in tags:
                result["camera_make"] = tags[make_field]
                break
        for model_field in ("com.apple.quicktime.model", "model", "android/model"):
            if model_field in tags:
                result["camera_model"] = tags[model_field]
                break

        # Video stream dimensions
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                w = stream.get("width")
                h = stream.get("height")
                if w and h:
                    result["width"] = w
                    result["height"] = h
                break
    except Exception:
        pass
    return result


def check_photo_integrity(path: Path) -> str:
    """
    Quick structural check using Pillow verify().
    Returns "" if OK, or a short label like "PARTIAL" if truncated/corrupt.
    Does NOT decode pixels — fast (~1ms per file).
    """
    if not PILLOW_AVAILABLE:
        return ""
    try:
        with Image.open(path) as img:
            img.verify()   # raises on truncated/invalid files
        return ""
    except Exception:
        return "PARTIAL"


def check_video_integrity(meta: dict, timed_out: bool) -> str:
    """
    Infer corruption from ffprobe output.
    Returns "" if OK, or a label like "PARTIAL" / "NOSTREAM".
    """
    if timed_out:
        return "PARTIAL"
    if not meta:
        return ""
    # No video streams found → header corrupt or unreadable
    if "width" not in meta and "height" not in meta:
        return "NOSTREAM"
    # Duration reported as 0 (container exists but no real content)
    dur = meta.get("duration_sec", -1)
    if dur == 0:
        return "PARTIAL"
    return ""


def exif_device_name(meta: dict) -> Optional[str]:
    """Build a clean device name from EXIF camera_make + camera_model."""
    make = meta.get("camera_make", "").strip()
    model = meta.get("camera_model", "").strip()
    if not model:
        return make or None
    # Avoid double brand like "Apple Apple iPhone 7" → "Apple iPhone 7"
    if make and not model.lower().startswith(make.lower()):
        return f"{make} {model}"
    return model


@dataclass(eq=False)
class FileRecord:
    src: Path
    ext: str
    size_bytes: int
    category: str        # "photo", "video", "audio", "document", "junk"
    dest_folder: str     # relative path inside output root
    dest_name: str       # filename to use
    filtered: bool = False
    filter_reason: str = ""
    # Parsed metadata
    source_device: str = ""
    width: Optional[int] = None
    height: Optional[int] = None
    duration_sec: Optional[int] = None
    date_taken: Optional[datetime] = None
    gps_lat: Optional[float] = None
    gps_lon: Optional[float] = None
    corrupt_label: str = ""   # e.g. "PARTIAL", "NOSTREAM" — appended to filename


def _safe(s: str) -> str:
    """Strip characters illegal in filenames."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s).strip()


def classify_file(path: Path, min_photo_px: int, min_video_sec: int,
                  skip_video_meta: bool = False) -> FileRecord:
    ext = path.suffix.lower()
    size = path.stat().st_size
    stem = path.stem

    # Strip sequence number from display name
    display_stem = SEQ_RE.sub("", stem).strip()
    seq_match = SEQ_RE.search(stem)
    seq = seq_match.group(1) if seq_match else "00000"

    # --- Junk by extension ---
    if ext in JUNK_EXTS:
        return FileRecord(
            src=path, ext=ext, size_bytes=size,
            category="junk", dest_folder="Manually Review/Junk", dest_name=path.name,
            filtered=True, filter_reason=f"junk extension ({ext})"
        )

    # --- Junk by filename ---
    if path.name.lower() in JUNK_FILENAMES:
        return FileRecord(
            src=path, ext=ext, size_bytes=size,
            category="junk", dest_folder="Manually Review/Junk", dest_name=path.name,
            filtered=True, filter_reason="system/cache file"
        )

    # --- Very small files (likely corrupt fragments) ---
    if size < 4096:
        return FileRecord(
            src=path, ext=ext, size_bytes=size,
            category="junk", dest_folder="Manually Review/Junk", dest_name=path.name,
            filtered=True, filter_reason=f"tiny file ({size} bytes, likely corrupt)"
        )

    # --- Photos ---
    if ext in PHOTO_EXTS:
        w, h = parse_dimensions(stem)
        source_dd = parse_source_device(stem)   # from Disk Drill filename

        # Try EXIF metadata (more authoritative than filename encoding)
        exif = read_photo_exif(path)
        date_taken: Optional[datetime] = exif.get("date_taken")
        gps_lat: Optional[float] = exif.get("gps_lat")
        gps_lon: Optional[float] = exif.get("gps_lon")

        # Prefer EXIF device name over Disk Drill's filename encoding
        exif_device = exif_device_name(exif)
        source = exif_device or source_dd
        device_cat = categorize_device(source)

        # If EXIF gave us actual dimensions, prefer those
        if not (w and h) and PILLOW_AVAILABLE:
            try:
                with Image.open(path) as img:
                    w, h = img.size
            except Exception:
                pass

        # Filter small images
        short_side = min(w, h) if (w and h) else None
        if short_side is not None and short_side < min_photo_px:
            return FileRecord(
                src=path, ext=ext, size_bytes=size,
                category="photo", dest_folder="Manually Review/Small Photos", dest_name=path.name,
                filtered=True,
                filter_reason=f"image too small ({w}x{h}, min short-side {min_photo_px}px)",
                source_device=source, width=w, height=h, date_taken=date_taken
            )

        # Filter asset-source photos (Photoshop etc.)
        if device_cat.startswith("_Assets"):
            return FileRecord(
                src=path, ext=ext, size_bytes=size,
                category="photo", dest_folder=f"Manually Review/{device_cat}", dest_name=path.name,
                filtered=True,
                filter_reason=f"graphics app export ({source})",
                source_device=source, width=w, height=h, date_taken=date_taken
            )

        corrupt_label = check_photo_integrity(path)

        # Build destination folder: Photos/<device>/<YYYY>/<YYYY-MM> when date available
        clabel = f"_{corrupt_label}" if corrupt_label else ""
        if date_taken:
            date_sub = f"{date_taken.year}/{date_taken.year}-{date_taken.month:02d}"
            dest_folder = f"Photos/{device_cat}/{date_sub}"
            date_prefix = date_taken.strftime("%Y-%m-%d_%H%M%S")
            dims_tag = f"_{w}x{h}" if (w and h) else ""
            dest_name = f"{date_prefix}{dims_tag}_{seq}{clabel}{ext}"
        else:
            dest_folder = f"Photos/{device_cat}/Unknown Date"
            safe_name = _safe(display_stem)
            dest_name = f"{safe_name}_{seq}{clabel}{ext}"

        return FileRecord(
            src=path, ext=ext, size_bytes=size,
            category="photo", dest_folder=dest_folder, dest_name=dest_name,
            source_device=source, width=w, height=h,
            date_taken=date_taken, gps_lat=gps_lat, gps_lon=gps_lon,
            corrupt_label=corrupt_label
        )

    # --- Videos ---
    if ext in VIDEO_EXTS:
        source_dd = parse_source_device(stem)
        duration_dd = parse_duration_seconds(stem)
        w_dd, h_dd = parse_dimensions(stem)

        # Try ffprobe metadata (skip if --skip-video-meta passed or file likely corrupt)
        meta = {} if skip_video_meta else read_video_metadata(path)
        date_taken = meta.get("date_taken")
        duration = meta.get("duration_sec") or duration_dd
        w = meta.get("width") or w_dd
        h = meta.get("height") or h_dd

        exif_device = exif_device_name(meta)
        source = exif_device or source_dd
        device_cat = categorize_video_source(source, duration)
        corrupt_label = check_video_integrity(meta, timed_out=meta.get("_timed_out", False))

        # Filter very short clips
        if duration is not None and duration < min_video_sec:
            return FileRecord(
                src=path, ext=ext, size_bytes=size,
                category="video", dest_folder="Manually Review/Short Videos", dest_name=path.name,
                filtered=True,
                filter_reason=f"too short ({duration}s, min {min_video_sec}s)",
                source_device=source, width=w, height=h, duration_sec=duration, date_taken=date_taken
            )

        # Filter tiny video files that are likely corrupt
        if size < 1024 * 100:  # < 100 KB
            return FileRecord(
                src=path, ext=ext, size_bytes=size,
                category="video", dest_folder="Manually Review/Corrupt Videos", dest_name=path.name,
                filtered=True, filter_reason=f"file too small for a real video ({size//1024}KB)",
                source_device=source, duration_sec=duration, date_taken=date_taken
            )

        # Build destination folder with optional date sub-folder
        clabel = f"_{corrupt_label}" if corrupt_label else ""
        if date_taken:
            date_sub = f"{date_taken.year}/{date_taken.year}-{date_taken.month:02d}"
            dest_folder = f"Videos/{device_cat}/{date_sub}"
            dur_tag = f"_{fmt_duration(duration)}" if duration else ""
            dims_tag = f"_{w}x{h}" if (w and h) else ""
            dest_name = f"{date_taken.strftime('%Y-%m-%d_%H%M%S')}{dims_tag}{dur_tag}_{seq}{clabel}{ext}"
        else:
            dest_folder = f"Videos/{device_cat}"
            safe_name = _safe(re.sub(r'[._]', " ", display_stem).strip())
            dest_name = f"{safe_name}_{seq}{clabel}{ext}"

        return FileRecord(
            src=path, ext=ext, size_bytes=size,
            category="video", dest_folder=dest_folder, dest_name=dest_name,
            source_device=source, width=w, height=h, duration_sec=duration,
            date_taken=date_taken, corrupt_label=corrupt_label
        )

    # --- Audio ---
    if ext in AUDIO_EXTS:
        source = parse_source_device(stem)
        safe_name = _safe(display_stem)
        dest_name = f"{safe_name}_{seq}{ext}"
        return FileRecord(
            src=path, ext=ext, size_bytes=size,
            category="audio", dest_folder="Audio", dest_name=dest_name,
            source_device=source
        )

    # --- Documents ---
    if ext in DOCUMENT_EXTS:
        sub = {
            ".pdf": "PDF", ".html": "Web", ".htm": "Web", ".xml": "Web",
            ".docx": "Word", ".doc": "Word",
            ".xlsx": "Excel", ".xls": "Excel",
            ".pptx": "PowerPoint", ".ppt": "PowerPoint",
            ".txt": "Text", ".csv": "Text",
        }.get(ext, "Other")
        safe_name = _safe(display_stem)
        dest_name = f"{safe_name}_{seq}{ext}"
        return FileRecord(
            src=path, ext=ext, size_bytes=size,
            category="document", dest_folder=f"Documents/{sub}", dest_name=dest_name,
        )

    # --- Unknown ---
    return FileRecord(
        src=path, ext=ext, size_bytes=size,
        category="unknown", dest_folder=f"Unknown/{ext.lstrip('.').upper() or 'NO_EXT'}",
        dest_name=path.name
    )


# ---------------------------------------------------------------------------
# Duplicate detection within output (same dest path)
# ---------------------------------------------------------------------------

def resolve_dest(records: list[FileRecord], out_root: Path) -> list[Path]:
    """Assign final destination paths, appending _1 _2 etc. for collisions.
    Returns a list of Paths in the same order as records."""
    used: dict[Path, int] = {}
    result: list[Path] = []
    for rec in records:
        base = out_root / rec.dest_folder / rec.dest_name
        if base not in used:
            used[base] = 0
            result.append(base)
        else:
            used[base] += 1
            result.append(base.with_stem(f"{base.stem}_{used[base]}"))
    return result


# ---------------------------------------------------------------------------
# Main scan + organize
# ---------------------------------------------------------------------------

def scan(in_root: Path, min_photo_px: int, min_video_sec: int,
         skip_video_meta: bool = False, workers: int = 8) -> list[FileRecord]:
    paths = [p for p in sorted(in_root.rglob("*")) if p.is_file()]
    records: list[FileRecord] = [None] * len(paths)
    done = 0

    def _classify(idx_path):
        idx, path = idx_path
        return idx, classify_file(path, min_photo_px, min_video_sec, skip_video_meta)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_classify, (i, p)): i for i, p in enumerate(paths)}
        for fut in as_completed(futures):
            try:
                idx, rec = fut.result()
                records[idx] = rec
            except Exception as e:
                print(f"  [ERROR] {e}", file=sys.stderr)
            done += 1
            if done % 1000 == 0:
                print(f"  ... scanned {done}/{len(paths)}")

    return [r for r in records if r is not None]


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_duration(sec: Optional[int]) -> str:
    if sec is None:
        return ""
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------

REPORT_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #1a1a2e; color: #e0e0e0; margin: 0; padding: 20px; }
h1 { color: #e94560; }
h2 { color: #0f3460; background: #16213e; padding: 8px 12px; border-radius: 4px; }
h3 { color: #a8dadc; margin: 16px 0 4px; }
.stats { display: flex; gap: 20px; flex-wrap: wrap; margin: 20px 0; }
.stat-box { background: #16213e; border-radius: 8px; padding: 16px 24px; min-width: 140px; }
.stat-num { font-size: 2em; font-weight: bold; color: #e94560; }
.stat-label { color: #a0a0c0; font-size: 0.85em; }
table { width: 100%; border-collapse: collapse; font-size: 0.82em; margin-bottom: 24px; }
th { background: #0f3460; color: #e0e0e0; padding: 6px 10px; text-align: left; }
td { padding: 4px 10px; border-bottom: 1px solid #2a2a4a; word-break: break-all; }
tr:hover td { background: #1f1f3a; }
.kept { color: #a8e6cf; }
.filtered { color: #ff8b94; }
.tag { display: inline-block; padding: 1px 6px; border-radius: 3px;
       font-size: 0.75em; font-weight: bold; }
.tag-photo { background: #0f5c2e; color: #a8e6cf; }
.tag-video { background: #2e0f5c; color: #c8a8e6; }
.tag-audio { background: #5c4a0f; color: #e6d8a8; }
.tag-doc   { background: #0f3c5c; color: #a8cce6; }
.tag-junk  { background: #3a1a1a; color: #e0a0a0; }
.tag-unknown { background: #2a2a2a; color: #b0b0b0; }
"""

def write_report(records: list[FileRecord], dest_map: dict, out_root: Path, report_path: Path,
                 min_photo_px: int, min_video_sec: int, execute: bool, move: bool):
    from itertools import groupby
    from collections import Counter

    kept = [r for r in records if not r.filtered]
    filtered = [r for r in records if r.filtered]

    total_size_kept = sum(r.size_bytes for r in kept)
    total_size_filtered = sum(r.size_bytes for r in filtered)
    cat_counts = Counter(r.category for r in kept)

    photos_with_exif = sum(1 for r in kept if r.category == "photo" and r.date_taken)
    videos_with_meta = sum(1 for r in kept if r.category == "video" and r.date_taken)
    photos_with_gps  = sum(1 for r in kept if r.category == "photo" and r.gps_lat)

    def tag(cat):
        return f'<span class="tag tag-{cat}">{cat}</span>'

    def gps_link(lat, lon):
        if lat is None or lon is None:
            return ""
        return (f'<a href="https://www.google.com/maps?q={lat:.6f},{lon:.6f}" '
                f'target="_blank" style="color:#60a0ff;font-size:0.8em">📍map</a>')

    lines = [
        "<!DOCTYPE html><html><head>",
        '<meta charset="utf-8">',
        "<title>Disk Drill Organizer Report</title>",
        f"<style>{REPORT_CSS}</style></head><body>",
        "<h1>Disk Drill Organizer — Report</h1>",
        f"<p>{'<b>EXECUTED</b> (' + ('moved' if move else 'copied') + ')' if execute else '<b>DRY RUN</b> — no files were modified'}</p>",
        f"<p>Output root: <code>{html.escape(str(out_root))}</code></p>",
        f"<p>EXIF/metadata: {'✅ Pillow available' if PILLOW_AVAILABLE else '❌ Pillow not installed (pip install Pillow)'} &nbsp;|&nbsp; "
        f"{'✅ ffprobe available' if FFPROBE_AVAILABLE else '❌ ffprobe not found (install ffmpeg)'}</p>",
        f"<p>Thresholds: min photo px = {min_photo_px}, min video duration = {min_video_sec}s</p>",
        '<div class="stats">',
        f'<div class="stat-box"><div class="stat-num">{len(records)}</div><div class="stat-label">Total Scanned</div></div>',
        f'<div class="stat-box"><div class="stat-num kept">{len(kept)}</div><div class="stat-label">Kept ({human_size(total_size_kept)})</div></div>',
        f'<div class="stat-box"><div class="stat-num filtered">{len(filtered)}</div><div class="stat-label">Filtered ({human_size(total_size_filtered)})</div></div>',
        f'<div class="stat-box"><div class="stat-num">{photos_with_exif}</div><div class="stat-label">Photos with EXIF date</div></div>',
        f'<div class="stat-box"><div class="stat-num">{photos_with_gps}</div><div class="stat-label">Photos with GPS</div></div>',
        f'<div class="stat-box"><div class="stat-num">{videos_with_meta}</div><div class="stat-label">Videos with metadata</div></div>',
    ]
    for cat, count in sorted(cat_counts.items()):
        lines.append(f'<div class="stat-box"><div class="stat-num">{count}</div><div class="stat-label">{cat.title()}</div></div>')
    lines.append("</div>")

    # Kept files grouped by destination folder
    lines.append("<h2>Kept Files by Folder</h2>")
    kept_sorted = sorted(kept, key=lambda r: r.dest_folder)
    for folder, group in groupby(kept_sorted, key=lambda r: r.dest_folder):
        group = list(group)
        folder_size = sum(r.size_bytes for r in group)
        lines.append(f"<h3>{html.escape(folder)} <small style='color:#666'>({len(group)} files, {human_size(folder_size)})</small></h3>")
        lines.append("<table><tr><th>Original Filename</th><th>New Filename</th><th>Size</th><th>Date Taken</th><th>Info</th></tr>")
        for r in sorted(group, key=lambda x: (x.date_taken or datetime.min, x.src.name)):
            dest = dest_map.get(r) or (out_root / r.dest_folder / r.dest_name)
            info_parts = []
            if r.width and r.height:
                info_parts.append(f"{r.width}×{r.height}")
            if r.duration_sec is not None:
                info_parts.append(fmt_duration(r.duration_sec))
            if r.source_device:
                info_parts.append(html.escape(r.source_device))
            date_str = r.date_taken.strftime("%Y-%m-%d %H:%M") if r.date_taken else '<span style="color:#666">unknown</span>'
            gps_str = gps_link(r.gps_lat, r.gps_lon)
            corrupt_badge = (f' <span class="tag" style="background:#5c1a00;color:#ffb380">'
                             f'{html.escape(r.corrupt_label)}</span>') if r.corrupt_label else ""
            lines.append(
                f"<tr>"
                f"<td>{tag(r.category)} {html.escape(r.src.name)}{corrupt_badge}</td>"
                f"<td class='kept'>{html.escape(dest.name)}</td>"
                f"<td>{human_size(r.size_bytes)}</td>"
                f"<td>{date_str} {gps_str}</td>"
                f"<td style='color:#888'>{' | '.join(info_parts)}</td>"
                f"</tr>"
            )
        lines.append("</table>")

    # Filtered files
    lines.append("<h2>Filtered Files</h2>")
    filter_sorted = sorted(filtered, key=lambda r: r.filter_reason)
    for reason, group in groupby(filter_sorted, key=lambda r: r.filter_reason):
        group = list(group)
        lines.append(f"<h3 class='filtered'>{html.escape(reason)} <small>({len(group)} files)</small></h3>")
        lines.append("<table><tr><th>Filename</th><th>Size</th><th>Category</th><th>Info</th></tr>")
        for r in group:
            info_parts = []
            if r.width and r.height:
                info_parts.append(f"{r.width}×{r.height}")
            if r.source_device:
                info_parts.append(html.escape(r.source_device))
            lines.append(
                f"<tr>"
                f"<td class='filtered'>{html.escape(r.src.name)}</td>"
                f"<td>{human_size(r.size_bytes)}</td>"
                f"<td>{tag(r.category)}</td>"
                f"<td style='color:#888'>{' | '.join(info_parts)}</td>"
                f"</tr>"
            )
        lines.append("</table>")

    lines.append("</body></html>")
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport written to: {report_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Organize Disk Drill recovered files into a clean structure",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("input_dir", help="Disk Drill output directory (e.g. F:/reconstructed)")
    parser.add_argument("output_dir", help="Where to put organized files (e.g. G:/organized)")
    parser.add_argument("--min-photo-px", type=int, default=800,
                        help="Minimum pixel dimension (shorter side) to keep a photo (default: 800)")
    parser.add_argument("--min-video-sec", type=int, default=30,
                        help="Minimum video duration in seconds to keep (default: 30)")
    parser.add_argument("--execute", action="store_true",
                        help="Actually copy/move files. Without this, only a dry-run report is generated.")
    parser.add_argument("--move", action="store_true",
                        help="Move files instead of copying (use only when input is expendable)")
    parser.add_argument("--report", default=None,
                        help="Path for the HTML report file (default: <output_dir>/report.html)")
    parser.add_argument("--include-filtered", action="store_true",
                        help="Also copy/move filtered files into Manually Review/ subfolders (default: skip them)")
    parser.add_argument("--skip-video-meta", action="store_true",
                        help="Skip ffprobe on videos (fast mode — uses only Disk Drill filename data)")
    parser.add_argument("--workers", type=int, default=8,
                        help="Parallel scan threads (default: 8; reduce if system is slow)")

    args = parser.parse_args()

    in_root = Path(args.input_dir)
    out_root = Path(args.output_dir)

    if not in_root.exists():
        print(f"ERROR: Input directory does not exist: {in_root}", file=sys.stderr)
        sys.exit(1)

    if args.execute and args.move:
        print("\nWARNING: --move will move files from the source. Make sure you have a backup or the source is a Disk Drill output copy.")
        confirm = input("Type YES to continue: ").strip()
        if confirm != "YES":
            print("Aborted.")
            sys.exit(0)

    report_path = Path(args.report) if args.report else out_root / "report.html"

    print(f"\nMetadata support:")
    print(f"  Pillow (photo EXIF):  {'YES' if PILLOW_AVAILABLE else 'NO  — pip install Pillow'}")
    print(f"  ffprobe (video meta): {'YES  — ' + FFPROBE_BIN if FFPROBE_AVAILABLE else 'NO  — place ffmpeg folder next to this script or add to PATH'}")
    print(f"\nThresholds: min photo dimension = {args.min_photo_px}px | min video duration = {args.min_video_sec}s")
    if args.skip_video_meta:
        print("  [--skip-video-meta] ffprobe disabled for videos — using filename metadata only")
    print(f"\nScanning {in_root} (workers={args.workers}) ...")
    records = scan(in_root, args.min_photo_px, args.min_video_sec,
                   skip_video_meta=args.skip_video_meta, workers=args.workers)
    print(f"Found {len(records)} files.")

    kept = [r for r in records if not r.filtered]
    filtered = [r for r in records if r.filtered]
    print(f"  Keeping:  {len(kept)}")
    print(f"  Filtered: {len(filtered)}")

    to_process = kept + (filtered if args.include_filtered else [])

    dest_paths = resolve_dest(to_process, out_root)
    # Build a parallel list pairing each record with its destination
    record_dest_pairs = list(zip(to_process, dest_paths))

    # Summary by category
    print("\nBreakdown by category:")
    from collections import Counter
    cat_counter = Counter(r.category for r in kept)
    for cat, count in sorted(cat_counter.items()):
        size = sum(r.size_bytes for r in kept if r.category == cat)
        print(f"  {cat:<12} {count:>6} files   {human_size(size):>10}")

    print("\nBreakdown by destination folder:")
    folder_counter = Counter(r.dest_folder for r in kept)
    for folder, count in sorted(folder_counter.items()):
        print(f"  {folder:<40} {count:>5} files")

    filter_counter = Counter(r.filter_reason for r in filtered)
    if filter_counter:
        print("\nFiltered reasons:")
        for reason, count in sorted(filter_counter.items(), key=lambda x: -x[1]):
            print(f"  {count:>6}  {reason}")

    if not args.execute:
        print(f"\n[DRY RUN] {len(to_process)} file(s) would be {'moved' if args.move else 'copied'}.")
        print("Run with --execute to apply changes.")
    else:
        out_root.mkdir(parents=True, exist_ok=True)
        op = shutil.move if args.move else shutil.copy2
        op_name = "Moving" if args.move else "Copying"
        print(f"\n{op_name} {len(to_process)} files...")
        done = 0
        errors = 0
        for rec, dst in record_dest_pairs:
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.exists():
                    # Skip exact duplicates (same size)
                    if dst.stat().st_size == rec.size_bytes:
                        continue
                    # Otherwise append _dup suffix
                    dst = dst.with_stem(dst.stem + "_dup")
                op(str(rec.src), str(dst))
                done += 1
                if done % 500 == 0:
                    print(f"  ... {done}/{len(to_process)}")
            except Exception as e:
                print(f"  [ERROR] {rec.src.name}: {e}", file=sys.stderr)
                errors += 1
        print(f"\n[DONE] {done} files {'moved' if args.move else 'copied'}. {errors} errors.")

        # After a move, clean up empty directories left behind in the source
        if args.move:
            print("Cleaning up empty source directories...")
            removed_dirs = 0
            for dirpath, dirnames, filenames in os.walk(in_root, topdown=False):
                d = Path(dirpath)
                if d == in_root:
                    continue  # never remove the root itself
                try:
                    d.rmdir()  # only succeeds if directory is truly empty
                    removed_dirs += 1
                except OSError:
                    pass  # not empty — leave it alone
            if removed_dirs:
                print(f"  Removed {removed_dirs} empty folder(s) from {in_root}")

    # Always write the report (build lookup dict for report rendering)
    dest_map = {rec: dst for rec, dst in record_dest_pairs}
    report_path.parent.mkdir(parents=True, exist_ok=True)
    write_report(records, dest_map, out_root, report_path,
                 args.min_photo_px, args.min_video_sec, args.execute, args.move)


if __name__ == "__main__":
    main()
