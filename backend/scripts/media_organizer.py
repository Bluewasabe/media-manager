#!/usr/bin/env python3
"""
Media File Organizer
Cleans up and organizes Movie and TV Show files for Plex/Jellyfin.

Usage:
  python media_organizer.py movies [path]   # Scan movie directory
  python media_organizer.py tv [path]       # Scan TV/Seasons directory
  Add --execute to actually move/rename files (default is dry-run)

Examples:
  python media_organizer.py movies "M:\\"
  python media_organizer.py tv "D:\\Seasons"
  python media_organizer.py movies "M:\\" --execute
"""

import re
import shutil
import argparse
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Regex patterns for junk tags found in filenames
# ---------------------------------------------------------------------------

JUNK_PATTERN = re.compile(
    r"""
    (?:
        # Video quality
        \b(?:2160p|1080[pi]|720p|480p|4[Kk]|UHD|HDR(?:10(?:\+)?)?|SDR|DoVi|DV)\b
        # Source
        | \b(?:Blu-?Ray|BDRip|BD|BRRip|WEB-?DL|WEBRip|WEB|HDTV|DVDRip|DVD|HDRip
               |AMZN|NF|HULU|DSNP|ATVP|PCOK|STAN|BCORE)\b
        # Video codec
        | \b(?:x\.?26[45]|H\.?26[45]|HEVC|AVC|XviD|DivX|VP9|AV1|REMUX|VC-1|AVI)\b
        # Audio
        | \b(?:(?:E?|DD\+?)(?:AC3|DTS(?:-?HD)?(?:\s*MA)?)|TrueHD|Atmos
               |AAC(?:2\.0|5\.1|LC)?|FLAC|MP3|5\.1|7\.1|2\.0|DDP5\.1|DD5\.1)\b
        # Release flags
        | \b(?:PROPER|REPACK|EXTENDED|UNRATED|THEATRICAL|IMAX|3D|COMPLETE
               |DIRECTORS?\.?CUT|HYBRID|RETAIL|LIMITED)\b
        # Release groups (add more as you see them)
        | \b(?:YIFY|YTS|RARBG|FGT|NTG|ION10|EtHD|SPARKS|ETRG|KiNGDOM
               |GalaxyRG|MeGusta|MRSK|KORSUB|SiGMA|AMIABLE)\b
        # Brackets / parens with non-year content
        | \[.*?\]
        | \((?!\d{4}\))\S[^)]*?\)
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts", ".mpg", ".mpeg"}

# Hard disc markers — CD/Disc/Disk are never part of a movie title.
# Strip these unconditionally wherever they appear.
HARD_PART_RE = re.compile(r"\b(?:cd|disc?|disk)[\s.\-]?(\d+)\b", re.IGNORECASE)

# Soft disc markers — "Part N" / "Pt N" can legitimately appear in a title
# (e.g. "Mockingjay Part 1"). Only treated as a disc marker when they appear
# AFTER the release year in the filename.
SOFT_PART_RE = re.compile(r"\b(?:part|pt)[\s.\-]?(\d+)\b", re.IGNORECASE)

# Roman numeral pattern (used to preserve II, III, IV, etc. in titles)
ROMAN_RE = re.compile(
    r"^M{0,4}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$"
)

def is_roman_numeral(w: str) -> bool:
    """Return True for non-empty valid Roman numeral strings (I, II, IV, XIV…)."""
    return bool(w) and bool(ROMAN_RE.match(w.upper()))

# ---------------------------------------------------------------------------
# Core cleaning helpers
# ---------------------------------------------------------------------------

def replace_dots_and_underscores(name: str) -> str:
    """Replace dots/underscores used as word separators with spaces."""
    # Preserve decimal numbers like 5.1 — handled by junk pattern, so just replace
    return re.sub(r"[._]", " ", name)


def extract_year(name: str):
    """Return (year_str, index_in_string) or (None, -1)."""
    for m in re.finditer(r"\b(19\d{2}|20[0-3]\d)\b", name):
        return m.group(1), m.start()
    return None, -1


def title_case_smart(name: str) -> str:
    """Title-case but keep small words lowercase and preserve Roman numerals."""
    small = {"a", "an", "and", "as", "at", "but", "by", "for", "from",
             "in", "into", "nor", "of", "on", "or", "so", "the", "to",
             "up", "via", "with", "yet"}
    words = name.split()
    result = []
    for i, w in enumerate(words):
        # Preserve all-caps Roman numerals (II, III, IV, XIV…)
        if w == w.upper() and w.isalpha() and is_roman_numeral(w):
            result.append(w.upper())
        elif i == 0 or i == len(words) - 1 or w.lower() not in small:
            result.append(w.capitalize())
        else:
            result.append(w.lower())
    return " ".join(result)


def parse_movie_stem(raw_stem: str):
    """
    Parse a raw filename stem into (title, year, part).
    part is an int (1, 2, …) when a CD/Disc/Part tag is detected, else None.
    """
    name = replace_dots_and_underscores(raw_stem)

    # --- Disc/part detection ---
    # Hard markers (CD, Disc, Disk) are never in a real title — strip always.
    # Soft markers (Part, Pt) are only disc labels when they appear AFTER the
    # year, e.g. "Movie.1999.Part.1.DVDRip" vs "Mockingjay Part 1 (2014)".
    part = None
    year, year_idx = extract_year(name)   # peek at year position first

    m = HARD_PART_RE.search(name)
    if m:
        part = int(m.group(1))
        name = (name[:m.start()] + name[m.end():]).strip()
        # Re-find year position after removal
        year, year_idx = extract_year(name)
    else:
        m = SOFT_PART_RE.search(name)
        if m and (year_idx < 0 or m.start() > year_idx):
            # "Part N" appears after the year → disc marker
            part = int(m.group(1))
            name = (name[:m.start()] + name[m.end():]).strip()
            year, year_idx = extract_year(name)

    # Pull the year out so we can truncate junk that follows it
    # (year was already located above; use cached values)
    if year_idx >= 0:
        name = name[:year_idx]

    # Remove remaining junk tags
    name = JUNK_PATTERN.sub(" ", name)

    # Collapse whitespace and trailing punctuation
    name = re.sub(r"\s+", " ", name).strip()
    name = re.sub(r"[\[\](),\-]+$", "", name).strip()

    name = title_case_smart(name)
    return name, year, part


def clean_movie_name(raw_stem: str):
    """Backward-compatible wrapper — returns (title, year), dropping part info."""
    title, year, _ = parse_movie_stem(raw_stem)
    return title, year


# ---------------------------------------------------------------------------
# TV episode parsing
# ---------------------------------------------------------------------------

# Ordered list of (pattern, season_group, episode_group)
TV_PATTERNS = [
    (re.compile(r"[Ss](\d{1,2})[Ee](\d{1,2})"),          1, 2),  # S01E01
    (re.compile(r"\b(\d{1,2})[xX](\d{2})\b"),             1, 2),  # 1x01
    (re.compile(r"\bSeason\s*(\d{1,2})\s*Episode\s*(\d{1,2})\b", re.I), 1, 2),
    (re.compile(r"\b[Ee][Pp]?(\d{2,3})\b"),               None, 1),  # E01 / EP01 (no season)
]


def parse_tv_filename(stem: str):
    """
    Returns (show_name, season, episode) — all may be None if undetected.
    season/episode are ints.
    """
    raw = replace_dots_and_underscores(stem)

    for pattern, sg, eg in TV_PATTERNS:
        m = pattern.search(raw)
        if m:
            show_part = raw[: m.start()]
            season  = int(m.group(sg)) if sg else None
            episode = int(m.group(eg))

            # Clean show name
            show_part = JUNK_PATTERN.sub(" ", show_part)
            show_part = re.sub(r"\s+", " ", show_part).strip()
            show_part = re.sub(r"[\-,]+$", "", show_part).strip()

            # Strip trailing year from show name
            year_val, year_idx2 = extract_year(show_part)
            if year_idx2 >= 0:
                show_part = show_part[:year_idx2].strip()

            show_part = title_case_smart(show_part) if show_part else None
            return show_part, season, episode

    # No episode pattern found — just clean the name
    clean, year = clean_movie_name(stem)
    return clean, None, None


# ---------------------------------------------------------------------------
# Movie organizer
# ---------------------------------------------------------------------------

def get_movie_info(item: Path, root: Path):
    """
    Derive (title, year) for a movie file.
    Checks the parent folder name first (often more reliable), then the filename stem.
    Handles cases like:
      Bloodshot (2020)/Bloodshot.mkv           → ("Bloodshot", "2020")
      Blade Runner 2049 (2017)/Blade Runner 2049.mkv → ("Blade Runner 2049", "2017")
    """
    file_title, file_year = clean_movie_name(item.stem)

    folder_title, folder_year = None, None
    if item.parent != root:
        folder_title, folder_year = clean_movie_name(item.parent.name)

    # Prefer the folder year when present (it's usually the intentional release year)
    year = folder_year or file_year

    # Prefer the longer title (more words = more complete)
    if folder_title and (not file_title or len(folder_title) >= len(file_title)):
        title = folder_title
    else:
        title = file_title

    return title, year


def remove_empty_folders(root: Path) -> list[Path]:
    """
    Walk root bottom-up and delete any folder that is completely empty.
    Sorts by depth (number of path parts) descending so children are always
    processed before their parents, regardless of folder names.
    Returns list of folders that were removed.
    """
    removed = []
    all_dirs = [p for p in root.rglob("*") if p.is_dir() and p != root]
    # Deepest folders first — len(parts) is the reliable depth measure
    for folder in sorted(all_dirs, key=lambda p: len(p.parts), reverse=True):
        if not any(folder.iterdir()):  # truly empty (no files, no subdirs left)
            folder.rmdir()
            removed.append(folder)
    return removed


def _queue_change(root: Path, item: Path, title, year, part, changes: list):
    """Build the destination path for one movie file and append to changes."""
    if not title:
        print(f"  [SKIP] Could not parse: {item.relative_to(root)}")
        return
    ext = item.suffix.lower()
    folder_name = f"{title} ({year})" if year else title
    new_filename = (f"{folder_name} - part{part}{ext}" if part is not None
                    else f"{folder_name}{ext}")
    dst = root / folder_name / new_filename
    if item != dst:
        changes.append((item, dst))


def plan_movie_changes(root: Path):
    """
    Walk root, find video files, return list of (src_path, dst_path) tuples.

    Per-folder logic:
      - 1 video file  → normal single-movie handling (uses folder name as hint)
      - N files, same base title, all have part numbers → multi-part movie,
        kept together as Title - part1 / part2
      - N files, different titles or no part numbers → collection folder,
        each file split into its own subfolder
    """
    changes = []

    # Group video files by their immediate parent folder
    by_folder: dict[Path, list[Path]] = {}
    for item in sorted(root.rglob("*")):
        if item.is_file() and item.suffix.lower() in VIDEO_EXTENSIONS:
            by_folder.setdefault(item.parent, []).append(item)

    for folder, files in sorted(by_folder.items()):
        is_root = folder == root

        # Folder-level hints (title/year from the parent directory name)
        f_title, f_year, _ = (parse_movie_stem(folder.name)
                               if not is_root else (None, None, None))

        # Parse every file in this folder
        parsed = [(item, *parse_movie_stem(item.stem)) for item in files]
        # parsed entries: (item, title, year, part)

        if len(parsed) == 1:
            item, t, y, p = parsed[0]
            title = f_title if (f_title and len(f_title) >= len(t or "")) else t
            year  = f_year or y
            _queue_change(root, item, title, year, p, changes)

        else:
            base_titles   = {t for _, t, _, _ in parsed}
            all_have_parts = all(p is not None for _, _, _, p in parsed)

            if len(base_titles) == 1 and all_have_parts:
                # Multi-part movie — all files share the same title and each
                # has a part number (CD1/CD2, Part1/Part2, etc.)
                for item, t, y, p in parsed:
                    title = f_title if (f_title and len(f_title) >= len(t or "")) else t
                    year  = f_year or y
                    _queue_change(root, item, title, year, p, changes)
            else:
                # Collection folder — split each film into its own subfolder
                for item, t, y, p in parsed:
                    _queue_change(root, item, t, y, p, changes)

    return changes


def run_movies(root: Path, execute: bool):
    print(f"\n{'='*60}")
    print(f"MOVIE ORGANIZER — {'EXECUTE' if execute else 'DRY RUN'}")
    print(f"Root: {root}")
    print("="*60)

    changes = plan_movie_changes(root)

    if not changes:
        print("Nothing to change — everything looks good!")
        return

    moved = 0
    duplicates = []  # (src, dst) pairs where dst already existed

    for src, dst in changes:
        rel_src = src.relative_to(root)
        rel_dst = dst.relative_to(root)
        print(f"\n  FROM: {rel_src}")
        print(f"    TO: {rel_dst}")

        if execute:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                print(f"    [DUPLICATE] Destination already exists — skipped")
                duplicates.append((src, dst))
                continue
            shutil.move(str(src), str(dst))
            moved += 1

    if not execute:
        print(f"\n[DRY RUN] {len(changes)} change(s) planned. Run with --execute to apply.")
    else:
        cleaned = remove_empty_folders(root)
        if cleaned:
            print(f"\n  Removed {len(cleaned)} empty folder(s):")
            for f in cleaned:
                print(f"    {f.relative_to(root)}")
        print(f"\n[DONE] {moved} file(s) moved/renamed.")
        if duplicates:
            print(f"\n{'='*60}")
            print(f"DUPLICATES — {len(duplicates)} file(s) skipped (destination already existed):")
            print("  You may want to compare these manually and delete the one you don't want.")
            print("="*60)
            for src, dst in duplicates:
                src_size = src.stat().st_size / (1024 ** 3)
                dst_size = dst.stat().st_size / (1024 ** 3)
                print(f"\n  KEPT (existing): {dst.relative_to(root)}  [{dst_size:.2f} GB]")
                print(f"  SKIPPED:         {src.relative_to(root)}  [{src_size:.2f} GB]")


# ---------------------------------------------------------------------------
# TV organizer
# ---------------------------------------------------------------------------

def plan_tv_changes(root: Path):
    """
    Walk root, find video files, return list of (src_path, dst_path, notes) tuples.
    Expected output structure: root / ShowName / Season XX / ShowName - S01E01.ext
    """
    changes = []
    warnings = []

    for item in sorted(root.rglob("*")):
        if not item.is_file():
            continue
        if item.suffix.lower() not in VIDEO_EXTENSIONS:
            continue

        stem = item.stem
        ext  = item.suffix.lower()

        show, season, episode = parse_tv_filename(stem)

        if not show:
            warnings.append(f"  [WARN] No show name detected: {item.relative_to(root)}")
            continue

        # Try to infer show name from parent folder if filename parse was weak
        # e.g. if file is inside "Breaking Bad/Season 1/"
        if episode is None:
            warnings.append(f"  [WARN] No episode number detected: {item.relative_to(root)}")
            continue

        if season is None:
            # Try to get season from parent folder name
            for parent in item.parents:
                m = re.search(r"[Ss]eason\s*(\d+)|[Ss](\d+)", parent.name)
                if m:
                    season = int(m.group(1) or m.group(2))
                    break
            if season is None:
                season = 1  # fallback

        season_folder = f"Season {season:02d}"
        episode_tag   = f"S{season:02d}E{episode:02d}"
        new_filename  = f"{show} - {episode_tag}{ext}"
        dst = root / show / season_folder / new_filename

        if item == dst:
            continue

        changes.append((item, dst, f"{show} | {episode_tag}"))

    return changes, warnings


def run_tv(root: Path, execute: bool):
    print(f"\n{'='*60}")
    print(f"TV ORGANIZER — {'EXECUTE' if execute else 'DRY RUN'}")
    print(f"Root: {root}")
    print("="*60)

    changes, warnings = plan_tv_changes(root)

    for w in warnings:
        print(w)

    if not changes:
        print("\nNothing to change — everything looks good!")
        return

    for src, dst, label in changes:
        rel_src = src.relative_to(root)
        rel_dst = dst.relative_to(root)
        print(f"\n  [{label}]")
        print(f"  FROM: {rel_src}")
        print(f"    TO: {rel_dst}")

        if execute:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))

    if not execute:
        print(f"\n[DRY RUN] {len(changes)} change(s) planned. Run with --execute to apply.")
    else:
        cleaned = remove_empty_folders(root)
        if cleaned:
            print(f"\n  Removed {len(cleaned)} empty folder(s):")
            for f in cleaned:
                print(f"    {f.relative_to(root)}")
        print(f"\n[DONE] {len(changes)} file(s) moved/renamed.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Organize Movie and TV Show files for Plex/Jellyfin",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Preview movie changes (safe, no files moved)
  python media_organizer.py movies "M:\\"

  # Preview TV changes
  python media_organizer.py tv "D:\\Seasons"

  # Actually apply movie changes
  python media_organizer.py movies "M:\\" --execute

  # Actually apply TV changes
  python media_organizer.py tv "D:\\Seasons" --execute
        """,
    )
    parser.add_argument("mode", choices=["movies", "tv"], help="What to organize")
    parser.add_argument("path", nargs="?", help="Root directory (default: M:\\ for movies, D:\\Seasons for tv)")
    parser.add_argument("--execute", action="store_true",
                        help="Actually move/rename files. Without this flag only a preview is shown.")

    args = parser.parse_args()

    # Default paths
    if args.path:
        root = Path(args.path)
    elif args.mode == "movies":
        root = Path("M:\\")
    else:
        root = Path("D:\\Seasons")

    if not root.exists():
        print(f"ERROR: Path does not exist: {root}", file=sys.stderr)
        sys.exit(1)

    if args.execute:
        print("\nWARNING: --execute will move and rename files.")
        confirm = input("Type YES to continue: ").strip()
        if confirm != "YES":
            print("Aborted.")
            sys.exit(0)

    if args.mode == "movies":
        run_movies(root, args.execute)
    else:
        run_tv(root, args.execute)


if __name__ == "__main__":
    main()
