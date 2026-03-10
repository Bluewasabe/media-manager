# Media Organizer

Renames and restructures Movie and TV Show video files into the folder layout
expected by Plex and Jellyfin. Runs as a **dry-run by default** — no files are
touched until you explicitly pass `--execute`.

---

## Requirements

- Python 3.9+
- No third-party packages — standard library only

---

## Usage

```
python media_organizer.py <mode> [path] [--execute]
```

| Argument | Required | Description |
|---|---|---|
| `mode` | Yes | `movies` or `tv` |
| `path` | No | Root directory to scan. Defaults to `M:\` for movies, `D:\Seasons` for TV |
| `--execute` | No | Actually move/rename files. Omit to preview changes only |

### Examples

```powershell
# Preview movie changes — safe, nothing moves
python media_organizer.py movies "M:\"

# Preview TV changes
python media_organizer.py tv "D:\Seasons"

# Apply movie changes to default path (prompts for confirmation)
python media_organizer.py movies "M:\" --execute

# Apply TV changes to a non-default path
python media_organizer.py tv "E:\Shows" --execute

# Run the parser unit tests
python test_parser.py
```

---

## What It Does

### Movie mode (`movies`)

**Target structure:**
```
M:\
  Movie Title (Year)\
    Movie Title (Year).mkv
  Multi-Part Film (2003)\
    Multi-Part Film (2003) - part1.avi
    Multi-Part Film (2003) - part2.avi
```

**Per-folder decision logic:**

| Folder contents | Treatment |
|---|---|
| 1 video file | Single movie — parent folder name used as title/year hint |
| N files, same title, all have disc markers (CD1/CD2…) | Multi-part movie — kept together, renamed `- part1`, `- part2` |
| N files with different titles | Collection folder — each film split into its own subfolder |

**After all moves:** empty folders are automatically deleted and listed in the output.

**Duplicate detection:** if a destination file already exists the move is
skipped. A report at the end shows both the existing file and the skipped file
with their sizes so you can decide which to keep.

---

### TV mode (`tv`)

**Target structure:**
```
D:\Seasons\
  Breaking Bad\
    Season 03\
      Breaking Bad - S03E07.mkv
  Game of Thrones\
    Season 01\
      Game of Thrones - S01E01.mkv
```

Episode numbering is standardised to `S##E##`. If a file has no season number
the script checks parent folder names (e.g. `Season 2/`) before falling back
to Season 01. Files with no detectable episode number are skipped with a
`[WARN]` message printed to the console.

---

## Filename Cleaning Pipeline

Every filename passes through these steps in order:

```
raw stem
  └─► 1. Dots/underscores → spaces
  └─► 2. Disc marker detection  (CD/Disc before year, or Part/Pt after year)
  └─► 3. Year extraction        (first 1900–2039 year; everything after = junk)
  └─► 4. JUNK_PATTERN removal   (quality, codec, audio, source, release group tags)
  └─► 5. Whitespace / trailing punctuation cleanup
  └─► 6. Smart title-casing
        • articles & prepositions stay lowercase unless first/last word
        • all-caps Roman numerals (II, III, IV…) preserved as-is
  └─► (title, year, part)
```

### Disc / part marker rules

| Marker type | Examples | Behaviour |
|---|---|---|
| **Hard** — `CD`, `Disc`, `Disk` | `CD1`, `Disc2`, `Disk3` | **Always** stripped — these never appear in real titles |
| **Soft** — `Part`, `Pt` | `Part 1`, `Pt2` | Stripped **only** when the marker appears *after* the year. If it appears *before* the year it belongs to the title (e.g. `Mockingjay Part 1 (2014)` stays intact) |

---

## Code Map

```
media_organizer.py
│
├── MODULE-LEVEL CONSTANTS
│   ├── VIDEO_EXTENSIONS      Set of recognised video suffixes (.mkv, .mp4, …)
│   ├── JUNK_PATTERN          Verbose regex stripping quality/codec/source/group tags
│   ├── HARD_PART_RE          Matches CD/Disc/Disk N — always a disc marker
│   ├── SOFT_PART_RE          Matches Part/Pt N   — disc marker only after year
│   └── ROMAN_RE              Full Roman numeral validator (used by is_roman_numeral)
│
├── PURE HELPERS  (no I/O, safe to import and unit-test)
│   │
│   ├── is_roman_numeral(w) → bool
│   │     Returns True if `w` is a non-empty valid Roman numeral string.
│   │     Called by title_case_smart to preserve words like II, XIV.
│   │
│   ├── replace_dots_and_underscores(name) → str
│   │     Converts dot/underscore word separators to spaces.
│   │     Called first in every parsing function.
│   │
│   ├── extract_year(name) → (year_str, index) | (None, -1)
│   │     Finds the first 4-digit year in range 1900–2039.
│   │     Returns both the value and its character position so callers
│   │     can use position to decide whether surrounding tokens are
│   │     title words or junk.
│   │
│   ├── title_case_smart(name) → str
│   │     Title-cases a cleaned string:
│   │       • First and last words always capitalised
│   │       • Articles / short prepositions (a, an, the, of, in, …) lowercase
│   │       • All-caps words that pass is_roman_numeral kept fully uppercase
│   │
│   ├── parse_movie_stem(raw_stem) → (title, year, part)   ← CORE PARSER
│   │     The single function that does all filename intelligence.
│   │     1. replace_dots_and_underscores
│   │     2. extract_year (peek — position needed for soft-marker decision)
│   │     3. HARD_PART_RE  — strip unconditionally if found
│   │        else SOFT_PART_RE — strip only if match.start() > year position
│   │     4. Re-extract year after any removal
│   │     5. Truncate at year position (discard trailing junk)
│   │     6. Apply JUNK_PATTERN to remaining prefix
│   │     7. Clean whitespace and trailing punctuation
│   │     8. title_case_smart
│   │     Returns (title_str, year_str_or_None, part_int_or_None)
│   │
│   ├── clean_movie_name(raw_stem) → (title, year)
│   │     Thin wrapper — calls parse_movie_stem and drops the part value.
│   │     Used by TV parser and get_movie_info where part info is irrelevant.
│   │
│   └── parse_tv_filename(stem) → (show, season, episode)
│         Tries TV_PATTERNS in priority order:
│           1. S01E01 / s01e01
│           2. 1x01
│           3. Season 1 Episode 1
│           4. E01 / EP01 (no season)
│         Everything before the matched pattern is the show name
│         (cleaned via JUNK_PATTERN + extract_year).
│         Falls back to clean_movie_name if no pattern matches.
│
├── MOVIE ORGANISER
│   │
│   ├── get_movie_info(item, root) → (title, year)
│   │     Compares two parse results — from the filename stem and from
│   │     the parent folder name — and merges them:
│   │       • Year:  folder year preferred (more intentional)
│   │       • Title: longer of the two (more complete)
│   │     Used for single-file folders only.
│   │
│   ├── _queue_change(root, item, title, year, part, changes)
│   │     Constructs the destination Path:
│   │       folder_name = "Title (Year)"  or  "Title"
│   │       filename    = folder_name + ".ext"
│   │                  or folder_name + " - partN.ext"
│   │     Appends (src, dst) to changes if src != dst.
│   │     Prints [SKIP] and returns early if title is empty.
│   │
│   ├── plan_movie_changes(root) → [(src, dst), …]
│   │     Groups every video file under root by its immediate parent folder.
│   │     For each group:
│   │       Single file → get_movie_info + _queue_change
│   │       Multi-file, 1 unique base title, all files have part numbers
│   │                  → multi-part movie path through _queue_change
│   │       Multi-file, otherwise
│   │                  → collection folder; each file gets _queue_change
│   │                    using only its own filename (folder name ignored)
│   │     Pure function — no files are moved.
│   │
│   ├── remove_empty_folders(root) → [Path, …]
│   │     Iterates root.rglob("*") in reverse-sorted order (deepest first).
│   │     Calls Path.rmdir() on any directory with no remaining children.
│   │     Returns the list of removed paths for reporting.
│   │
│   └── run_movies(root, execute)
│         Orchestrator for movie mode:
│           1. plan_movie_changes  — build change list
│           2. Print FROM/TO for every change
│           3. If execute=True:
│                mkdir dst.parent
│                Skip + record if dst already exists (duplicate)
│                shutil.move(src, dst)
│           4. remove_empty_folders
│           5. Print duplicate report (file sizes included)
│
├── TV ORGANISER
│   │
│   ├── plan_tv_changes(root) → (changes, warnings)
│   │     Walks all video files, calls parse_tv_filename for each.
│   │     Missing season → scans parent folder names for "Season N" / "SN".
│   │     Missing episode → adds to warnings list, skips file.
│   │     Builds destination:  root/Show/Season NN/Show - S##E##.ext
│   │     Returns ([(src, dst, label), …], [warning_str, …])
│   │
│   └── run_tv(root, execute)
│         Prints warnings, then FROM/TO for each change.
│         If execute=True: mkdir + shutil.move.
│         No duplicate detection (TV files have unique S##E## names).
│
└── ENTRY POINT
    └── main()
          argparse → resolve default paths → if --execute prompt "Type YES"
          → run_movies(root, execute) or run_tv(root, execute)
```

---

## Extending the Script

### Add a new junk tag

Edit `JUNK_PATTERN` near the top of the file. Tags are grouped by category —
add to the matching group or create a new one. The flag `re.IGNORECASE` is
already set so case does not matter.

### Add a new release group name

Find the `# Release groups` line inside `JUNK_PATTERN` and append the name
to the alternation group.

### Add a new TV episode pattern

Append a `(compiled_regex, season_group_index, episode_group_index)` tuple to
`TV_PATTERNS`. Patterns are tried in list order. Use `None` for
`season_group_index` when the pattern has no season capture group.

### Support a new video container format

Add the lowercase extension string (e.g. `".webm"`) to `VIDEO_EXTENSIONS`.

### Change default paths

Edit the two `Path(...)` literals in `main()` where `args.mode == "movies"`
and the `else` branch below it.
