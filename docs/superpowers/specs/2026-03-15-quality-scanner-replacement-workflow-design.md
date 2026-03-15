# Quality Scanner — Replacement Workflow Design

**Date:** 2026-03-15
**Project:** media-manager
**Status:** Approved

---

## Overview

Extend the existing Quality Scanner with a two-phase replacement workflow. Phase 1 (scan) is unchanged and read-only. Phase 2 (replace) allows the user to:

1. Auto-match flagged low-quality files against replacements in the Windows Downloads folder
2. Confirm, override, or skip each match in a UI overlay panel
3. Mark files with no replacement as "nuke" (delete without replacement)
4. Execute confirmed swaps (move replacement into original's location, delete original) and nukes as a tracked job

The scanner remains strictly non-destructive. All destructive operations live exclusively in the new replace router.

---

## Architecture

```
Phase 1 (existing, unchanged):
  Quality Scanner runs → HTML report + NEW: results.json (written alongside HTML)

Phase 2 (new):
  "Review Replacements" button on completed job card
    → Overlay panel opens
    → Backend fuzzy-matches flagged files against Downloads folder
    → User confirms / overrides / nukes per file
    → "Refresh Downloads" re-runs match, preserves existing confirmations
    → "Execute Swaps" → tracked replace_workflow job in DB
```

### New Components

| Component | Location | Purpose |
|---|---|---|
| JSON output | `quality_scanner.py` | Machine-readable flagged file list for Phase 2 |
| `routers/replace.py` | New backend router | Match and execute endpoints |
| Overlay panel | `frontend/index.html` | Full-page review UI triggered from job card |
| `replace_workflow` job type | Existing `jobs` table | Swap/nuke operations tracked in job history |
| Downloads volume | `docker-compose.override.yml` | Mounts `C:\Users\Bluew\Downloads` → `/downloads` |

---

## Backend

### `quality_scanner.py` — JSON output (addition only)

When `--report` is provided, derive the JSON path by replacing the `.html` extension with `.json` and write it unconditionally at the end of the scan. No new CLI argument is needed.

Example: `--report /data/reports/quality_report_20260315_100000.html`
writes: `/data/reports/quality_report_20260315_100000.json`

If `--report` is not provided, no JSON is written and Phase 2 is unavailable for that job.

The JSON structure mirrors the scanner's existing internal result dict:

```json
{
  "scan_date": "2026-03-15T10:00:00",
  "source_dir": "/mnt/media/videos",
  "flagged": [
    {
      "path": "/mnt/media/videos/Movie.720p.mkv",
      "low_quality": true,
      "webcam": false,
      "bad_audio": false,
      "reasons": [
        "resolution 720p below 1080p threshold",
        "very low bitrate (800 kbps at 720p, threshold 1200 kbps)"
      ],
      "resolution": "1280x720",
      "bitrate": 1200000
    }
  ]
}
```

`reasons` is the existing natural-language reason list the scanner already builds. `low_quality`, `webcam`, `bad_audio` are the existing boolean flags. No normalization into a new enum — the UI uses the boolean flags for the "Why Flagged" column label and the reasons list as a tooltip/detail.

**Path format:** `path` in the `flagged` list must be written as an **absolute path** (`str(path)`, not `str(path.relative_to(source))`). This is a deliberate change from the scanner's existing internal relative-path logic. The replace router requires absolute paths for scope validation and file operations — no reconstruction from `source_dir` should be necessary.

### Locating `results.json` from a job ID

`routers/replace.py` locates the results file as follows:

1. Fetch the scan job record from the DB by `job_id`
2. Parse `jobs.stats` (JSON blob)
3. Read `stats['report_path']`
4. Derive JSON path: `Path(stats['report_path']).with_suffix('.json')`
5. If `stats['report_path']` is absent or the derived `.json` file does not exist, return HTTP 404 with `{"error": "No results.json found for this job. The scan may have run without --report."}`

### `routers/replace.py` — New router

**`GET /api/replace/match?job_id=<uuid>`**
- Locates `results.json` using the procedure above
- Scans `/downloads` top-level only (non-recursive) for video files, filtered to the same `VIDEO_EXTS` set defined in `quality_scanner.py` (import or reproduce with a comment referencing the source)
- For each flagged file, fuzzy-matches against all Downloads files using `rapidfuzz.fuzz.token_sort_ratio`:
  - Normalize each filename: strip extension, strip known quality tags (`720p`, `1080p`, `4k`, `2160p`, `BluRay`, `BDRip`, `WEBRip`, `WEB-DL`, `HDTV`, `DVDRip`, `x264`, `x265`, `HEVC`, `AAC`, `AC3`, `HDR`), replace `.` and `_` with spaces, lowercase, strip extra whitespace
  - Score the normalized original name against each normalized Downloads filename
  - Threshold: **72** (integer, 0–100 scale)
  - If multiple candidates score ≥ 72, propose the highest scorer
  - If two candidates are within 5 points of each other, set `"ambiguous": true` and populate both `proposed_match` / `score` and `alternate_match` / `alternate_score` — the UI shows a ⚠️ and requires explicit user action
  - Titles with fewer than 4 significant characters after normalization are skipped (no match attempted — too short to be reliable)
- Returns score as an **integer 0–100**
- Response shape:
  ```json
  [
    {
      "original": "/mnt/media/videos/Movie.720p.mkv",
      "low_quality": true,
      "webcam": false,
      "bad_audio": false,
      "reasons": ["resolution 720p below 1080p threshold"],
      "proposed_match": "/downloads/Movie.1080p.BluRay.mkv",
      "score": 94,
      "ambiguous": false,
      "alternate_match": null,
      "alternate_score": null
    },
    {
      "original": "/mnt/media/videos/Movie.720p.mkv",
      "low_quality": true,
      "webcam": false,
      "bad_audio": false,
      "reasons": ["resolution 720p below 1080p threshold"],
      "proposed_match": "/downloads/Movie.1080p.BluRay.mkv",
      "score": 89,
      "ambiguous": true,
      "alternate_match": "/downloads/Movie.1080p.WEBRip.mkv",
      "alternate_score": 86
    },
    {
      "original": "/mnt/media/videos/BadAudio.mkv",
      "low_quality": false,
      "webcam": false,
      "bad_audio": true,
      "reasons": ["no audio stream"],
      "proposed_match": null,
      "score": null,
      "ambiguous": false,
      "alternate_match": null,
      "alternate_score": null
    }
  ]
  ```
- **No files are touched**

**`POST /api/replace/execute`**

Payload:
```json
{
  "job_id": "<source scan uuid>",
  "confirmed": true,
  "swaps": [
    { "original": "/mnt/media/videos/Movie.720p.mkv", "replacement": "/downloads/Movie.1080p.BluRay.mkv" }
  ],
  "nukes": ["/mnt/media/videos/BadAudio.mkv"]
}
```

**Path scope validation (security gate):**
Before executing, the backend:
1. Fetches the scan job record from the DB by `job_id` and reads `config['source']` (the immutable source directory set at job creation time by `routers/jobs.py`)
2. Loads `results.json` and reads its `source_dir` field as a secondary check — if `source_dir` differs from `config['source']`, reject with HTTP 422 (tamper detection)
3. Validates every `original` and `nuke` path is under `config['source']`
4. Validates every `replacement` path is under `/downloads`
5. Any path failing validation → HTTP 422 with the offending path listed. No partial execution.

**`confirmed: true` is also required** — requests missing this field are rejected with HTTP 400.

**Execution model:**
- `POST /api/replace/execute` creates the `replace_workflow` job record in the DB immediately (status: `running`), then returns HTTP 202 with `{ "job_id": "<new uuid>" }` — the response does not wait for completion
- The actual file I/O runs as an `asyncio.create_task()` coroutine in the router, mirroring how `POST /api/jobs` dispatches to `runner.run()`
- Per-action log entries are written via `add_log()` directly (not stdout parsing — there is no subprocess)
- Job status is updated to `completed` or `failed` via `update_job_status()` when the coroutine finishes

**File operations:**
- For each swap: move replacement to original's directory (keeping replacement's filename), then delete original
- For each nuke: delete the file
- If destination filename already exists (collision on move): log error, skip that swap — do not overwrite
- All other failures are per-item and never abort the batch
- `config` stores `{ "source_scan_job_id": "<uuid>", "source_dir": "..." }`
- Each action logged: `"Moved /downloads/Movie.1080p.BluRay.mkv → /mnt/media/videos/Movie.1080p.BluRay.mkv"` / `"Deleted /mnt/media/videos/BadAudio.mkv"`

### Downloads Volume

Add to `docker-compose.override.yml` (machine-specific, never committed):

```yaml
services:
  backend:
    volumes:
      - C:\Users\Bluew\Downloads:/downloads
```

---

## Frontend

### Trigger

A **"Review Replacements"** button appears on completed quality scan job cards alongside the existing "View Report" button.

Visibility condition (determinable from the job record without reading `results.json`):

```js
(stats.low_quality || 0) + (stats.webcam || 0) + (stats.bad_audio || 0) > 0
```

These stat keys are already captured from the `STATS:` stdout line by `job_runner.py` and stored in `jobs.stats`. This is a sufficient proxy for `flagged.length > 0`.

### Overlay Panel — Three Zones

**Top bar:**
- Title: `Replacement Workflow — [source_dir] · [scan_date]`
- Downloads path display: hardcoded `/downloads` label (not a dynamic endpoint — path is fixed by the volume mount)
- **"Refresh Downloads"** button
  - Disabled during an in-flight refresh fetch
  - **"Execute Swaps" button is also disabled during a refresh** (prevents race condition between stale state and execution)
  - On completion: re-enables both buttons; highlights newly matched files with a "New match found" badge
- Close (X) button — exits without executing anything

**Main table (one row per flagged file):**

| Original File | Why Flagged | Proposed Match | Score | Action |
|---|---|---|---|---|
| Movie.720p.mkv | Low quality | Movie.1080p.BluRay.mkv | 94% | ✅ Confirm / 📁 Pick different |
| BadAudio.mkv | Bad audio | *(no match)* | — | 💀 Nuke / ⏭ Skip |
| Webcam.mp4 | Webcam | Webcam.HD.mp4 ⚠️ ambiguous | 81% | ✅ Confirm / 📁 Pick different / 💀 Nuke |

- **"Why Flagged"** label is derived from `low_quality`, `webcam`, `bad_audio` boolean flags. Hover/tooltip shows the full `reasons` array.
- **Score** is rendered as `{score}%` (integer from backend, frontend appends `%`). Null score renders as `—`.
- **Ambiguous** matches show a ⚠️ warning icon — user must explicitly Confirm or Pick different.
- **Confirm** — accept the proposed match
- **Pick different** — opens the existing file browser component in callback mode (see below), pre-navigated to `/downloads`
- **Nuke** — delete without replacement
- **Skip** — do nothing (leaves file untouched); this is the default state for all rows

**"Pick different" browser integration:**

Extend `openBrowser()` to accept an optional `callback` function instead of a DOM target ID. When the user confirms a selection, call `callback(selectedPath)` instead of writing to an input value. The overlay uses this callback to update the per-row replacement path in UI state. After opening, call `browserNavigate('/downloads')` to pre-navigate to the Downloads folder.

**Bottom bar:**
- Live summary: `3 swaps · 1 nuke · 2 skipped`
- **"Execute Swaps"** button:
  - Disabled until at least one Confirm or Nuke is selected
  - Disabled during an in-flight refresh (race condition prevention)
- Final confirmation dialog before executing — lists every file that will be deleted (both swaps and nukes)

### Execution Result

After the replace_workflow job completes, the overlay shows:
- Summary: `3 moved · 1 deleted · 2 skipped`
- Link to the replace_workflow job in the Jobs tab for the full audit log

The replace_workflow job card in the Jobs tab shows a "Source scan" link back to the original scan job (via `config.source_scan_job_id`).

---

## Data Flow

```
1. Quality scan completes
   → results.json written alongside HTML report (same stem, .json extension)
   → "Review Replacements" button appears on job card (based on stats counts)

2. User clicks "Review Replacements"
   → GET /api/replace/match?job_id=<uuid>
   → Backend reads results.json (located via stats.report_path in DB)
   → Overlay opens with matched/unmatched table

3. User downloads more files, clicks "Refresh Downloads"
   → Same endpoint re-called
   → Previous user confirmations preserved
   → Execute button disabled during fetch
   → Newly matched files highlighted with "New match found" badge

4. User confirms swaps + marks nukes, clicks "Execute Swaps"
   → Confirmation dialog: lists every file that will be deleted
   → POST /api/replace/execute with confirmed payload
   → Backend validates path scopes before touching anything
   → replace_workflow job created in DB (stores source_scan_job_id in config)
   → Each action logged: "Moved X → Y" / "Deleted Z"
   → Job card appears in Jobs tab with back-link to source scan

5. Execution complete
   → Overlay shows summary + link to replace_workflow job log
```

---

## Error Handling

| Scenario | Behavior |
|---|---|
| Original file missing at execute time | Log warning, skip — do not abort batch |
| Replacement file missing at execute time (disappeared from Downloads) | Log error, skip that swap |
| Destination filename already exists (collision on move) | Log error, skip that swap — do not overwrite |
| Permission denied on delete or move | Log error with exact path, continue rest |
| No fuzzy matches found in Downloads | Panel shows all files as "no match" — user can still nuke or skip |
| Downloads folder not mounted | Clear error at top of overlay: "Downloads folder not accessible — check docker-compose.override.yml" |
| `confirmed: true` missing from execute payload | HTTP 400, no action taken |
| Path scope validation failure (path outside allowed dirs) | HTTP 422 with offending paths listed, no partial execution |
| `results.json` missing or `stats.report_path` absent | HTTP 404: "No results.json found for this job" |
| Title too short for reliable fuzzy match (< 4 chars after normalization) | No match attempted, row shows "no match" |
| Ambiguous match (two candidates within 5 points) | Both shown with ⚠️ — user must explicitly confirm or pick different |
| Refresh in flight when Execute clicked | Execute button is disabled during refresh — not possible |

---

## Swap Execution Detail

```
Swap:
  Source:      /downloads/Movie.1080p.BluRay.mkv
  Destination: /mnt/media/movies/Movie.1080p.BluRay.mkv   ← replacement's filename
  Then delete: /mnt/media/movies/Movie.720p.mkv            ← original deleted after move succeeds

Nuke:
  Delete:      /mnt/media/movies/BadAudio.mkv
```

The replacement keeps its own filename in the destination directory so the filename accurately reflects the file's actual quality. The original is only deleted after the move completes successfully — never before.

---

## What Is Not In Scope

- Matching replacements outside the Downloads folder
- Renaming the replacement to match the original filename
- Undo / restore after deletion (confirmations and audit log are the safety net)
- Batch-downloading replacements from within the UI
- Scanning subdirectories of Downloads (flat top-level scan only)
