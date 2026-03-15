# Quality Scanner Replacement Workflow — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a two-phase replacement workflow to the quality scanner — auto-match flagged files against the Windows Downloads folder, confirm/nuke via an overlay panel, and execute as a tracked job.

**Architecture:** The quality scanner gains a JSON output alongside its existing HTML report. A new `routers/replace.py` handles fuzzy matching (GET) and async execution (POST). The frontend gains a "Review Replacements" button on completed quality-scanner job cards, which opens a full-page overlay for review and confirmation before any files are touched.

**Tech Stack:** Python 3.11, FastAPI, aiosqlite, rapidfuzz 3.9.7, asyncio, vanilla JS + Tailwind CSS

**Spec:** `docs/superpowers/specs/2026-03-15-quality-scanner-replacement-workflow-design.md`

**No automated test suite exists in this project.** All verification is manual via browser and curl as noted in each task.

**Rebuild required after backend/frontend changes:**
```bash
cd c:/Code/media-manager
docker-compose up -d --build
```
Scripts in `backend/scripts/` are live-mounted and do NOT require a rebuild.

---

## Chunk 1: Backend

### Task 1: Add rapidfuzz dependency

**Files:**
- Modify: `backend/requirements.txt`

- [ ] **Step 1: Add rapidfuzz to requirements**

In `backend/requirements.txt`, add after line 6:
```
rapidfuzz==3.9.7
```

Final file:
```
fastapi==0.115.0
uvicorn[standard]==0.30.0
aiosqlite==0.20.0
Pillow==10.4.0
imagehash==4.3.1
python-multipart==0.0.9
rapidfuzz==3.9.7
```

- [ ] **Step 2: Commit**

```bash
cd c:/Code/media-manager
git add backend/requirements.txt
git commit -m "Add rapidfuzz dependency for fuzzy filename matching"
```

---

### Task 2: Add JSON output to quality_scanner.py

**Files:**
- Modify: `backend/scripts/quality_scanner.py`

This is a live-mounted script — no rebuild needed after editing.

**Key locations:**
- Line 448: `"path": str(rel)` — change to `str(path)` for absolute paths
- Lines 471–498: HTML report write block — add JSON write immediately after HTML write succeeds
- After the `write_html_report` function definition (before `scan()`) — add `write_results_json()`

- [ ] **Step 1: Change relative path to absolute path in results list**

At line 448, change:
```python
"path":        str(rel),
```
to:
```python
"path":        str(path),
```

This makes every path in the `results` list absolute instead of relative to `source`. The HTML report renderer uses `rel` for display — verify `rel` is still used for display in `write_html_report` (it passes `results` which now contains absolute paths, but the HTML rendering should use a basename or relative display — check `write_html_report` to see if it needs the `rel` field. If the HTML report breaks, add back a `"rel": str(rel)` key for display and keep `"path"` as absolute.)

> **Caution:** Read `write_html_report()` (it's between line ~200 and ~350) to confirm it uses `r["path"]` for display. If so, the HTML report will now show absolute paths in the table — which is actually fine and more informative. If it used relative paths for display, add `"display_path": str(rel)` to the dict and update the HTML template to use `r["display_path"]`.

- [ ] **Step 2: Add write_results_json() function**

Add this function immediately before the `scan()` function (around line 330, after `write_html_report`):

```python
def write_results_json(json_path: "Path", results: list, source: "Path", scan_date: str) -> None:
    """Write machine-readable flagged-file list alongside the HTML report."""
    flagged = [
        {
            "path":        r["path"],  # absolute path
            "low_quality": r["low_quality"],
            "webcam":      r["webcam"],
            "bad_audio":   r["bad_audio"],
            "reasons":     r["reasons"],
            "resolution":  f"{r['info']['width']}x{r['info']['height']}" if r["info"].get("width") and r["info"].get("height") else "",
            "bitrate_kbps": r["info"].get("bitrate_kbps", 0),
        }
        for r in results
        if r["low_quality"] or r["webcam"] or r["bad_audio"]
    ]
    payload = {
        "scan_date":  scan_date,
        "source_dir": str(source),
        "flagged":    flagged,
    }
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
```

- [ ] **Step 3: Call write_results_json() after HTML write succeeds**

In `scan()`, find the HTML report write block (lines 471–498). It currently looks like:

```python
if args.report:
    report_path = Path(args.report)
    try:
        write_html_report(report_path, results, source, args.min_quality)
        print(f"INFO: Report saved to: {report_path}")
        print(f"REPORT_PATH: {report_path}", flush=True)
    except IsADirectoryError:
        ...
```

After the `print(f"REPORT_PATH: ...")` line, add the JSON write:

```python
        print(f"INFO: Report saved to: {report_path}")
        print(f"REPORT_PATH: {report_path}", flush=True)
        # Write machine-readable results for the replacement workflow
        json_path = report_path.with_suffix('.json')
        try:
            scan_date = datetime.now().isoformat(timespec='seconds')
            write_results_json(json_path, results, source, scan_date)
            print(f"INFO: Results JSON saved to: {json_path}")
        except OSError as e:
            print(f"WARN: Could not write results JSON: {e}", file=sys.stderr)
```

- [ ] **Step 4: Manual verification**

Run a quality scan from the UI with a report path set. After the scan completes:
```bash
# SSH into backend container or use docker exec
docker exec -it media-manager-backend-1 ls /data/reports/
# Should see both quality_report_YYYYMMDD_HHMMSS.html AND .json
docker exec -it media-manager-backend-1 cat /data/reports/quality_report_*.json | head -40
```
Expected: valid JSON with `scan_date`, `source_dir`, and `flagged` array containing only files that were flagged, with absolute paths.

- [ ] **Step 5: Commit**

```bash
cd c:/Code/media-manager
git add backend/scripts/quality_scanner.py
git commit -m "Add JSON output to quality scanner for replacement workflow"
```

---

### Task 3: Create backend/routers/replace.py

**Files:**
- Create: `backend/routers/replace.py`

- [ ] **Step 1: Create the file**

Create `backend/routers/replace.py` with the full content below:

```python
"""
Replacement workflow router.
GET  /api/replace/match?job_id=<uuid>  — fuzzy-match flagged files against /downloads
POST /api/replace/execute              — swap replacements in, delete originals
"""

import asyncio
import json
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from rapidfuzz import fuzz

from db import add_log, create_job, get_job, update_job_status, update_job_stats

router = APIRouter()

DOWNLOADS_PATH = Path("/downloads")

# Keep in sync with quality_scanner.py VIDEO_EXTS
VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".m4v", ".wmv", ".flv", ".mpg",
    ".mpeg", ".ts", ".m2ts", ".webm", ".rm", ".rmvb", ".divx",
}

# Quality/codec tags to strip during title normalization
_STRIP_TAGS = re.compile(
    r'\b(?:2160p|1080p|720p|540p|480p|4k|uhd|bluray|blu[-_]ray|bdrip|webrip|web[-_]dl|'
    r'hdtv|dvdrip|hdrip|x264|x265|h264|h265|hevc|avc|aac|ac3|dts|'
    r'hdr|hdr10|dolby|atmos|remux|proper|repack|extended|theatrical|'
    r'directors[-_.]cut|unrated|dubbed|subbed|multi|xvid|divx)\b',
    re.IGNORECASE,
)


def normalize_title(filename: str) -> str:
    """Strip extension, quality tags, and separators; return lowercase collapsed string."""
    name = Path(filename).stem
    name = _STRIP_TAGS.sub(' ', name)
    name = re.sub(r'[\._\-]+', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip().lower()
    return name


def _get_results_json_path(job: dict) -> Path:
    report_path = (job.get("stats") or {}).get("report_path")
    if not report_path:
        raise HTTPException(
            404,
            "No results.json found for this job. The scan may have run without --report."
        )
    json_path = Path(report_path).with_suffix(".json")
    if not json_path.exists():
        raise HTTPException(
            404,
            "No results.json found for this job. The scan may have run without --report."
        )
    return json_path


@router.get("/replace/match")
async def get_match(job_id: str):
    job = await get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    json_path = _get_results_json_path(job)
    data = json.loads(json_path.read_text(encoding="utf-8"))

    if not DOWNLOADS_PATH.exists():
        raise HTTPException(
            503,
            "Downloads folder not accessible — check docker-compose.override.yml"
        )

    # Top-level scan only, video files only
    candidates = [
        f for f in DOWNLOADS_PATH.iterdir()
        if f.is_file() and f.suffix.lower() in VIDEO_EXTS
    ]
    normalized_candidates = [(c, normalize_title(c.name)) for c in candidates]

    response = []
    for item in data.get("flagged", []):
        norm_original = normalize_title(Path(item["path"]).name)

        # Skip titles too short to match reliably (< 4 non-space chars)
        if len(norm_original.replace(" ", "")) < 4:
            response.append({
                **item,
                "proposed_match": None, "score": None,
                "ambiguous": False,
                "alternate_match": None, "alternate_score": None,
            })
            continue

        scored = []
        for cand, norm_cand in normalized_candidates:
            score = fuzz.token_sort_ratio(norm_original, norm_cand)
            if score >= 72:
                scored.append((score, str(cand)))

        scored.sort(key=lambda x: x[0], reverse=True)

        if not scored:
            response.append({
                **item,
                "proposed_match": None, "score": None,
                "ambiguous": False,
                "alternate_match": None, "alternate_score": None,
            })
        elif len(scored) >= 2 and (scored[0][0] - scored[1][0]) <= 5:
            response.append({
                **item,
                "proposed_match": scored[0][1], "score": scored[0][0],
                "ambiguous": True,
                "alternate_match": scored[1][1], "alternate_score": scored[1][0],
            })
        else:
            response.append({
                **item,
                "proposed_match": scored[0][1], "score": scored[0][0],
                "ambiguous": False,
                "alternate_match": None, "alternate_score": None,
            })

    return response


class SwapItem(BaseModel):
    original: str
    replacement: str


class ExecutePayload(BaseModel):
    job_id: str
    confirmed: bool
    swaps: list[SwapItem] = []
    nukes: list[str] = []


@router.post("/replace/execute", status_code=202)
async def execute_replace(body: ExecutePayload):
    if not body.confirmed:
        raise HTTPException(400, "confirmed must be true")

    source_job = await get_job(body.job_id)
    if not source_job:
        raise HTTPException(404, "Source scan job not found")

    # Immutable anchor: source dir from job config (set at job creation, never edited)
    source_dir = (source_job.get("config") or {}).get("source")
    if not source_dir:
        raise HTTPException(422, "Source scan job has no config.source")
    source_path = Path(source_dir).resolve()

    # Tamper detection: cross-check against results.json source_dir
    json_path = _get_results_json_path(source_job)
    results_data = json.loads(json_path.read_text(encoding="utf-8"))
    if Path(results_data.get("source_dir", "")).resolve() != source_path:
        raise HTTPException(
            422,
            "results.json source_dir does not match job config.source — possible tamper detected"
        )

    # Validate path scopes
    invalid = []
    for swap in body.swaps:
        if not Path(swap.original).resolve().is_relative_to(source_path):
            invalid.append(f"original not under source_dir: {swap.original}")
        if not Path(swap.replacement).resolve().is_relative_to(DOWNLOADS_PATH.resolve()):
            invalid.append(f"replacement not under /downloads: {swap.replacement}")
    for nuke in body.nukes:
        if not Path(nuke).resolve().is_relative_to(source_path):
            invalid.append(f"nuke not under source_dir: {nuke}")
    if invalid:
        raise HTTPException(422, {"errors": invalid})

    # Create replace_workflow job — set running immediately (spec requires running at creation)
    new_job_id = await create_job(
        "replace_workflow",
        {"source_scan_job_id": body.job_id, "source_dir": source_dir},
        [],
    )
    await update_job_status(new_job_id, "running")

    asyncio.create_task(
        _execute_replace_task(new_job_id, body.swaps, body.nukes)
    )
    return {"job_id": new_job_id}


async def _execute_replace_task(
    job_id: str,
    swaps: list[SwapItem],
    nukes: list[str],
) -> None:
    moved = deleted = skipped = errors = 0

    async def log(level: str, msg: str) -> None:
        await add_log(job_id, datetime.utcnow().isoformat(), level, msg)

    try:
        for swap in swaps:
            orig = Path(swap.original)
            repl = Path(swap.replacement)
            dest = orig.parent / repl.name

            if not orig.exists():
                await log("WARN", f"Original not found, skipping: {orig}")
                skipped += 1
                continue
            if not repl.exists():
                await log("ERROR", f"Replacement gone from Downloads, skipping: {repl}")
                skipped += 1
                continue
            if dest.exists():
                await log("ERROR", f"Destination exists (no overwrite), skipping: {dest}")
                skipped += 1
                continue

            try:
                shutil.move(str(repl), str(dest))
                orig.unlink()
                await log("INFO", f"Moved {repl} → {dest}")
                await log("INFO", f"Deleted {orig}")
                moved += 1
            except Exception as e:
                await log("ERROR", f"Failed swap {orig}: {e}")
                errors += 1

        for nuke_path in nukes:
            nuke = Path(nuke_path)
            if not nuke.exists():
                await log("WARN", f"Nuke target not found, skipping: {nuke}")
                skipped += 1
                continue
            try:
                nuke.unlink()
                await log("INFO", f"Deleted {nuke}")
                deleted += 1
            except Exception as e:
                await log("ERROR", f"Failed to delete {nuke}: {e}")
                errors += 1

        await update_job_stats(job_id, {
            "moved": moved, "deleted": deleted,
            "skipped": skipped, "errors": errors,
        })
        status = "completed" if (moved + deleted > 0 or errors == 0) else "failed"
        await update_job_status(job_id, status, exit_code=0 if status == "completed" else 1)

    except Exception as e:
        await log("ERROR", f"Unexpected error in replace task: {e}")
        await update_job_status(job_id, "failed", exit_code=1)
```

- [ ] **Step 2: Verify the file is syntactically valid**

```bash
cd c:/Code/media-manager
docker exec -it media-manager-backend-1 python -c "import ast; ast.parse(open('/app/routers/replace.py').read()); print('OK')"
```

*(If the container isn't rebuilt yet, run: `python -c "import ast; ast.parse(open('backend/routers/replace.py').read()); print('OK')"` from the host)*

- [ ] **Step 3: Commit**

```bash
cd c:/Code/media-manager
git add backend/routers/replace.py
git commit -m "Add replace workflow router with fuzzy match and async execute endpoints"
```

---

### Task 4: Register replace router in main.py

**Files:**
- Modify: `backend/main.py` (lines 6 and 63)

- [ ] **Step 1: Add import**

At line 6, change:
```python
from routers import filesystem, jobs, logs
```
to:
```python
from routers import filesystem, jobs, logs, replace
```

- [ ] **Step 2: Register router**

After line 63 (`app.include_router(logs.router, prefix="/api")`), add:
```python
app.include_router(replace.router, prefix="/api")
```

- [ ] **Step 3: Rebuild and verify endpoints exist**

```bash
cd c:/Code/media-manager
docker-compose up -d --build
# Wait ~10 seconds, then:
curl http://localhost:45681/api/replace/match?job_id=test 2>&1
# Expected: {"detail":"Job not found"} (404) — proves the route is registered
```

- [ ] **Step 4: Commit**

```bash
cd c:/Code/media-manager
git add backend/main.py
git commit -m "Register replace router in FastAPI app"
```

---

### Task 5: Add Downloads volume to docker-compose.override.yml

**Files:**
- Modify: `docker-compose.override.yml` (machine-specific, gitignored — NOT committed)

`docker-compose.override.yml` is generated by `setup.ps1` on first run and is never committed. Edit it directly on disk.

- [ ] **Step 1: Open docker-compose.override.yml**

File is at `c:/Code/media-manager/docker-compose.override.yml`. Find the `backend` service's `volumes:` block and add the Downloads mount.

The file looks something like:
```yaml
services:
  backend:
    volumes:
      - ./data:/data
      - ./backend/scripts:/app/scripts
      - D:/:/mnt/drives/d   # (your drive mounts vary)
```

Add the Downloads line:
```yaml
      - C:\Users\Bluew\Downloads:/downloads
```

Full example after edit:
```yaml
services:
  backend:
    volumes:
      - ./data:/data
      - ./backend/scripts:/app/scripts
      - D:/:/mnt/drives/d
      - C:\Users\Bluew\Downloads:/downloads
```

- [ ] **Step 2: Restart to pick up new volume**

```bash
cd c:/Code/media-manager
docker-compose up -d
# Note: up -d (no --build) is sufficient for volume changes
```

- [ ] **Step 3: Verify /downloads is accessible**

```bash
docker exec -it media-manager-backend-1 ls /downloads
# Expected: list of files in your Windows Downloads folder
```

- [ ] **Step 4: Verify match endpoint works with Downloads**

```bash
# Get a real completed quality_scanner job_id from the UI (Jobs tab), then:
curl "http://localhost:45681/api/replace/match?job_id=<real-job-id>"
# Expected: JSON array of flagged files with proposed_match fields
# If no --report was set: {"detail":"No results.json found..."}
```

---

## Chunk 2: Frontend

### Task 6: Extend openBrowser() with file-select callback mode

**Files:**
- Modify: `frontend/index.html`

The existing browser only supports folder selection. The "Pick different" feature needs to select a specific FILE from `/downloads`. This task adds a `'file-select'` mode where clicking a file calls a callback.

**Key locations:**
- Line 326: `browserCallback: null` — already exists in state (just never used)
- Line 2008: `openBrowser(targetInputId, mode = null)` — add callback parameter
- Line 2031: `closeBrowser()` — clear callback
- Lines 2071–2080: item rendering in `browserNavigate()` — make files clickable in file-select mode
- Lines 2109–2136: `browserSelectFolder()` — add callback branch

- [ ] **Step 1: Extend openBrowser() signature and set callback**

At line 2008, change:
```js
function openBrowser(targetInputId, mode = null) {
  state.browserTarget = targetInputId;
  state.browserMode = mode; // null = normal, 'df-add-source' = add to df sources
```
to:
```js
function openBrowser(targetInputId, mode = null, callback = null) {
  state.browserTarget = targetInputId;
  state.browserMode = mode; // null = normal, 'df-add-source' = add to df sources, 'file-select' = pick file via callback
  state.browserCallback = callback;
```

Also change the title logic (around line 2017) to handle `'file-select'`:
```js
  if (mode === 'df-add-source') {
    title.textContent = 'Select Source Directory';
  } else if (mode === 'file-select') {
    title.textContent = 'Select Replacement File';
  } else {
    title.textContent = 'Browse — Select Folder';
  }
```

- [ ] **Step 2: Clear callback in closeBrowser()**

At line 2027, `closeBrowser()` already resets `state.browserMode = null` (line 2031). Confirm `state.browserCallback = null` is also cleared. It already exists as a state property. Add the reset explicitly if missing:

```js
function closeBrowser() {
  document.getElementById('browser-modal').classList.add('hidden');
  state.browserTarget = null;
  state.browserPath = null;
  state.browserMode = null;
  state.browserCallback = null;   // ← add this line if not present
}
```

- [ ] **Step 3: Make files clickable in file-select mode**

In `browserNavigate()`, find the item rendering block (around lines 2071–2080). Currently files have `cursor:default` and no onclick. Change it to:

```js
const isFileSelect = state.browserMode === 'file-select';
const onclick = i.type === 'dir' && !i.inaccessible
  ? `onclick="browserNavigate('${i.path.replace(/'/g, "\\'")}')"`
  : (i.type !== 'dir' && isFileSelect
      ? `onclick="browserPickFile('${i.path.replace(/'/g, "\\'")}')" `
      : '');
const style = i.type !== 'dir' && !isFileSelect ? 'cursor:default;' : '';
```

Then add the `browserPickFile()` function after `browserSelectFolder()`:

```js
function browserPickFile(path) {
  if (state.browserCallback) {
    state.browserCallback(path);
  }
  closeBrowser();
}
```

- [ ] **Step 4: Add callback branch to browserSelectFolder()**

In `browserSelectFolder()` (around line 2133), before the existing `} else if (state.browserTarget) {` branch, add:

```js
  } else if (state.browserCallback) {
    state.browserCallback(state.browserPath);
  } else if (state.browserTarget) {
```

This allows using "Select This Folder" as a fallback even in callback mode, in case the user wants to select the Downloads folder itself rather than a specific file.

- [ ] **Step 5: Rebuild and manual verification**

```bash
cd c:/Code/media-manager
docker-compose up -d --build
```
Open the app at http://localhost:45681. Open any script config and click Browse. Verify:
- Normal browse still works (folder selection, writes to input)
- No JS errors in browser console

- [ ] **Step 6: Commit**

```bash
cd c:/Code/media-manager
git add frontend/index.html
git commit -m "Extend openBrowser() with file-select callback mode"
```

---

### Task 7: Add "Review Replacements" button to quality scanner job cards

**Files:**
- Modify: `frontend/index.html`

The "View Report →" link is rendered at line 1451 (static render) and updated dynamically at line 1710 (WebSocket stats update). Add a "Review Replacements" button in both places.

**Key locations:**
- Lines 1448–1452: Static job card HTML — add button after the report link div
- Lines 1707–1712: `updateJobStats()` dynamic update — add button show/hide logic

- [ ] **Step 1: Add button to static job card render**

Find the report link block (lines 1448–1452):
```html
      <!-- Report link (shown when script emits REPORT_PATH) -->
      <div id="report-link-${job.id}" style="margin-bottom:10px;${stats.report_path ? '' : 'display:none'}">
        ${stats.report_path ? `<a href="/api/file?path=...">📄 View Report →</a>` : ''}
      </div>
```

After the closing `</div>` of the report-link div, add:
```html
      <!-- Review Replacements button (quality_scanner jobs with flagged files) -->
      <div id="replace-btn-${job.id}" style="margin-bottom:10px;${
        job.script === 'quality_scanner' && ((stats.low_quality||0)+(stats.webcam||0)+(stats.bad_audio||0)) > 0
          ? '' : 'display:none'
      }">
        <button class="btn-secondary btn-sm" onclick="openReplaceOverlay('${job.id}')">
          🔄 Review Replacements
        </button>
      </div>
```

- [ ] **Step 2: Add dynamic show/hide in updateJobStats()**

Find the `updateJobStats()` dynamic report link update (lines 1707–1712):
```js
  const reportEl = document.getElementById('report-link-' + jobId);
  if (reportEl && stats.report_path) {
    reportEl.style.display = '';
    reportEl.innerHTML = `<a href="...">📄 View Report →</a>`;
  }
```

After that block, add:
```js
  // Show "Review Replacements" for quality_scanner jobs with flagged files
  const replaceBtnEl = document.getElementById('replace-btn-' + jobId);
  if (replaceBtnEl) {
    const flaggedCount = (stats.low_quality || 0) + (stats.webcam || 0) + (stats.bad_audio || 0);
    if (flaggedCount > 0) replaceBtnEl.style.display = '';
  }
```

- [ ] **Step 3: Rebuild and verify button appears**

Run a quality scan that produces flagged files. When the scan completes, verify:
- "Review Replacements" button appears on the job card
- Button does NOT appear on media_organizer / duplicate_finder job cards
- Clicking the button should trigger a JS error ("openReplaceOverlay is not defined") — that's expected until Task 8

- [ ] **Step 4: Commit**

```bash
cd c:/Code/media-manager
git add frontend/index.html
git commit -m "Add Review Replacements button to quality scanner job cards"
```

---

### Task 8: Build the replacement overlay panel

**Files:**
- Modify: `frontend/index.html`

This is the largest task. Add:
1. HTML for the overlay panel (hidden by default) — near the end of `<body>`, after the browser modal
2. JavaScript: `openReplaceOverlay()`, `refreshDownloads()`, `renderReplaceTable()`, `executeSwaps()`, `closeReplaceOverlay()`

- [ ] **Step 1: Add overlay HTML**

Find the browser modal closing tag (around line 292 `</div>` ending the `id="browser-modal"` div). After it, add:

```html
  <!-- ============================================================= -->
  <!-- REPLACE WORKFLOW OVERLAY                                       -->
  <!-- ============================================================= -->
  <div id="replace-overlay" class="hidden" style="
    position:fixed;inset:0;background:var(--bg-primary);z-index:1000;
    display:flex;flex-direction:column;overflow:hidden;">

    <!-- Header -->
    <div style="padding:16px 20px;border-bottom:1px solid var(--border);
                display:flex;align-items:center;gap:12px;flex-shrink:0;">
      <div style="flex:1;">
        <div style="font-weight:700;font-size:1rem;margin-bottom:2px">
          🔄 Replacement Workflow
        </div>
        <div id="replace-overlay-subtitle" style="font-size:0.78rem;color:var(--text-muted);font-family:monospace"></div>
      </div>
      <div id="replace-downloads-label" style="font-size:0.75rem;color:var(--text-muted);
           padding:4px 10px;background:rgba(0,0,0,0.2);border-radius:6px;">
        Downloads: /downloads
      </div>
      <button id="replace-refresh-btn" onclick="refreshDownloads()"
              class="btn-secondary btn-sm">⟳ Refresh Downloads</button>
      <button onclick="closeReplaceOverlay()" class="btn-icon" style="font-size:1rem">✕</button>
    </div>

    <!-- Downloads error banner (hidden by default) -->
    <div id="replace-downloads-error" class="hidden" style="
         padding:10px 20px;background:rgba(239,68,68,0.1);
         border-bottom:1px solid var(--red);color:var(--red);font-size:0.82rem;"></div>

    <!-- Table area -->
    <div style="flex:1;overflow-y:auto;padding:16px 20px;">
      <div id="replace-table-container">
        <div style="text-align:center;padding:60px;color:var(--text-muted)">Loading…</div>
      </div>
    </div>

    <!-- Footer -->
    <div style="padding:14px 20px;border-top:1px solid var(--border);
                display:flex;align-items:center;gap:12px;flex-shrink:0;">
      <div id="replace-summary" style="flex:1;font-size:0.82rem;color:var(--text-muted)">—</div>
      <button id="replace-execute-btn" onclick="executeSwaps()"
              class="btn-primary" disabled
              style="background:var(--red);border-color:var(--red);">
        ⚡ Execute Swaps
      </button>
    </div>
  </div>
```

- [ ] **Step 2: Add overlay state and helper functions**

In the `state` object (around line 319), add inside the object:
```js
  replaceJobId: null,
  replaceItems: [],        // [{original, low_quality, webcam, bad_audio, reasons, proposed_match, score, ambiguous, alternate_match, alternate_score, action, userReplacement}]
  replaceRefreshing: false,
```

Then add the following JS functions near the end of the script section (before the `document.addEventListener` calls):

```js
// =====================================================================
// REPLACE WORKFLOW OVERLAY
// =====================================================================

function openReplaceOverlay(jobId) {
  state.replaceJobId = jobId;
  state.replaceItems = [];
  document.getElementById('replace-overlay').classList.remove('hidden');
  refreshDownloads();
}

function closeReplaceOverlay() {
  document.getElementById('replace-overlay').classList.add('hidden');
  state.replaceJobId = null;
  state.replaceItems = [];
}

async function refreshDownloads() {
  if (state.replaceRefreshing) return;
  state.replaceRefreshing = true;

  const refreshBtn = document.getElementById('replace-refresh-btn');
  const executeBtn = document.getElementById('replace-execute-btn');
  refreshBtn.disabled = true;
  executeBtn.disabled = true;
  refreshBtn.textContent = '⟳ Refreshing…';

  const errBanner = document.getElementById('replace-downloads-error');
  errBanner.classList.add('hidden');

  try {
    const data = await api(`/replace/match?job_id=${state.replaceJobId}`);

    // Merge new match results — preserve existing user decisions (action, userReplacement)
    const prev = {};
    state.replaceItems.forEach(item => { prev[item.original] = item; });

    state.replaceItems = data.map(item => {
      const p = prev[item.original] || {};
      const isNewMatch = !p.proposed_match && item.proposed_match;
      return {
        ...item,
        action: p.action || (item.proposed_match ? 'confirm' : 'skip'),
        userReplacement: p.userReplacement || null,
        isNewMatch,
      };
    });

    // Set subtitle from first item's source context
    if (data.length > 0) {
      const subtitle = document.getElementById('replace-overlay-subtitle');
      subtitle.textContent = `${data.length} flagged file(s) · scan job ${state.replaceJobId.slice(0,8)}`;
    }

    renderReplaceTable();
  } catch (e) {
    errBanner.textContent = e.message || 'Failed to load match results';
    errBanner.classList.remove('hidden');
    if (e.message && e.message.includes('Downloads folder not accessible')) {
      errBanner.textContent = 'Downloads folder not accessible — add C:\\Users\\Bluew\\Downloads:/downloads to docker-compose.override.yml and restart';
    }
    document.getElementById('replace-table-container').innerHTML =
      `<div style="text-align:center;padding:60px;color:var(--text-muted)">Failed to load — see error above</div>`;
  } finally {
    state.replaceRefreshing = false;
    refreshBtn.disabled = false;
    refreshBtn.textContent = '⟳ Refresh Downloads';
    updateReplaceFooter();
  }
}

function renderReplaceTable() {
  const container = document.getElementById('replace-table-container');
  if (!state.replaceItems.length) {
    container.innerHTML = `<div style="text-align:center;padding:60px;color:var(--text-muted)">No flagged files found in results.</div>`;
    return;
  }

  const rows = state.replaceItems.map((item, idx) => {
    const fname = item.original.split('/').pop();
    const flagLabel = item.low_quality ? 'Low quality' : item.webcam ? 'Webcam' : item.bad_audio ? 'Bad audio' : 'Flagged';
    const reasonsTip = escHtml(item.reasons.join('; '));

    const matchDisplay = item.userReplacement
      ? `<span style="color:var(--cyan)">${escHtml(item.userReplacement.split('/').pop())}</span> <span style="color:var(--text-muted);font-size:0.72rem">(manual)</span>`
      : item.proposed_match
        ? `${escHtml(item.proposed_match.split('/').pop())}${item.ambiguous ? ' <span style="color:var(--amber)" title="Two candidates are very close — please confirm">⚠️</span>' : ''}${item.isNewMatch ? ' <span style="color:var(--green);font-size:0.7rem">NEW</span>' : ''}`
        : `<span style="color:var(--text-muted);font-style:italic">no match</span>`;

    const scoreDisplay = item.score !== null ? `${item.score}%` : '—';
    const effectiveReplacement = item.userReplacement || item.proposed_match;

    const actionBtns = `
      ${effectiveReplacement ? `
        <button onclick="replaceSetAction(${idx},'confirm')" class="btn-sm ${item.action==='confirm'?'btn-primary':'btn-secondary'}" style="padding:3px 8px">✅ Confirm</button>
        <button onclick="replacePickDifferent(${idx})" class="btn-sm btn-secondary" style="padding:3px 8px">📁 Pick</button>
      ` : ''}
      <button onclick="replaceSetAction(${idx},'nuke')" class="btn-sm ${item.action==='nuke'?'btn-danger':'btn-secondary'}" style="padding:3px 8px;${item.action==='nuke'?'background:var(--red);border-color:var(--red);color:#fff':''}">💀 Nuke</button>
      <button onclick="replaceSetAction(${idx},'skip')" class="btn-sm ${item.action==='skip'?'btn-primary':'btn-secondary'}" style="padding:3px 8px">⏭ Skip</button>
    `;

    const rowBg = item.action === 'confirm' ? 'rgba(34,197,94,0.05)'
                : item.action === 'nuke'    ? 'rgba(239,68,68,0.05)'
                : 'transparent';

    return `
      <tr style="border-bottom:1px solid var(--border);background:${rowBg}">
        <td style="padding:10px 12px;font-family:monospace;font-size:0.78rem;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escHtml(item.original)}">${escHtml(fname)}</td>
        <td style="padding:10px 12px;font-size:0.78rem;" title="${reasonsTip}">${flagLabel}</td>
        <td style="padding:10px 12px;font-size:0.78rem;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${matchDisplay}</td>
        <td style="padding:10px 12px;font-size:0.78rem;color:var(--text-muted)">${scoreDisplay}</td>
        <td style="padding:10px 12px;white-space:nowrap">${actionBtns}</td>
      </tr>`;
  }).join('');

  container.innerHTML = `
    <table style="width:100%;border-collapse:collapse;">
      <thead>
        <tr style="border-bottom:2px solid var(--border);">
          <th style="padding:8px 12px;text-align:left;font-size:0.72rem;color:var(--text-muted);text-transform:uppercase">Original</th>
          <th style="padding:8px 12px;text-align:left;font-size:0.72rem;color:var(--text-muted);text-transform:uppercase">Why Flagged</th>
          <th style="padding:8px 12px;text-align:left;font-size:0.72rem;color:var(--text-muted);text-transform:uppercase">Proposed Match</th>
          <th style="padding:8px 12px;text-align:left;font-size:0.72rem;color:var(--text-muted);text-transform:uppercase">Score</th>
          <th style="padding:8px 12px;text-align:left;font-size:0.72rem;color:var(--text-muted);text-transform:uppercase">Action</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;

  updateReplaceFooter();
}

function replaceSetAction(idx, action) {
  state.replaceItems[idx].action = action;
  renderReplaceTable();
}

function replacePickDifferent(idx) {
  openBrowser(null, 'file-select', (selectedPath) => {
    state.replaceItems[idx].userReplacement = selectedPath;
    state.replaceItems[idx].action = 'confirm';
    renderReplaceTable();
  });
  // Pre-navigate to /downloads
  setTimeout(() => browserNavigate('/downloads'), 100);
}

function updateReplaceFooter() {
  const confirms = state.replaceItems.filter(i => i.action === 'confirm').length;
  const nukes    = state.replaceItems.filter(i => i.action === 'nuke').length;
  const skips    = state.replaceItems.filter(i => i.action === 'skip').length;

  document.getElementById('replace-summary').textContent =
    `${confirms} swap${confirms !== 1 ? 's' : ''} · ${nukes} nuke${nukes !== 1 ? 's' : ''} · ${skips} skip${skips !== 1 ? 's' : ''}`;

  const executeBtn = document.getElementById('replace-execute-btn');
  executeBtn.disabled = state.replaceRefreshing || (confirms + nukes) === 0;
}

async function executeSwaps() {
  const confirms = state.replaceItems.filter(i => i.action === 'confirm');
  const nukes    = state.replaceItems.filter(i => i.action === 'nuke');

  if (confirms.length + nukes.length === 0) return;

  // Build confirmation message
  const deleteList = [
    ...confirms.map(i => `DELETE: ${i.original.split('/').pop()}`),
    ...nukes.map(i => `DELETE: ${i.original.split('/').pop()}`),
  ];
  const msg = `This will permanently delete ${deleteList.length} file(s):\n\n${deleteList.join('\n')}\n\nContinue?`;
  if (!confirm(msg)) return;

  const swaps = confirms.map(i => ({
    original: i.original,
    replacement: i.userReplacement || i.proposed_match,
  }));
  const nukeList = nukes.map(i => i.original);

  const executeBtn = document.getElementById('replace-execute-btn');
  executeBtn.disabled = true;
  executeBtn.textContent = '⏳ Executing…';

  try {
    const result = await api('/replace/execute', 'POST', {
      job_id: state.replaceJobId,
      confirmed: true,
      swaps,
      nukes: nukeList,
    });
    toast(`Replacement job started — job ID: ${result.job_id.slice(0,8)}`, 'success');
    closeReplaceOverlay();
    // Refresh jobs list so the new replace_workflow job card appears
    renderJobs();
  } catch (e) {
    toast(`Execute failed: ${e.message}`, 'error');
    executeBtn.disabled = false;
    executeBtn.textContent = '⚡ Execute Swaps';
  }
}
```

- [ ] **Step 3: Add `.btn-danger` CSS class** (if not already present)

Search for `.btn-danger` in `index.html`. If it doesn't exist, find the `.btn-secondary` CSS definition and add after it:
```css
.btn-danger {
  background: var(--red);
  border-color: var(--red);
  color: #fff;
}
.btn-danger:hover { filter: brightness(1.1); }
```

- [ ] **Step 4: Verify `api()` function signature**

The actual signature in `index.html` is:
```js
async function api(path, method = 'GET', body = null) { ... }
```
It takes positional args and calls `JSON.stringify(body)` internally. The `executeSwaps()` call in the plan already uses the correct form:
```js
await api('/replace/execute', 'POST', { job_id, confirmed, swaps, nukes });
```
No changes needed here — this is a verification step only.

- [ ] **Step 5: Rebuild and end-to-end test**

```bash
cd c:/Code/media-manager
docker-compose up -d --build
```

Full workflow test:
1. Run a quality scan with a report path set (so results.json is written)
2. Go to Jobs tab — completed scan should show "Review Replacements" button
3. Click it — overlay opens, shows flagged files with match results from /downloads
4. Put a video file in your Downloads folder with a similar name to a flagged file
5. Click "⟳ Refresh Downloads" — verify new match appears with "NEW" badge
6. Confirm the match, click "Execute Swaps", confirm the dialog
7. Check Jobs tab — a `replace_workflow` job should appear with a log of moved/deleted files
8. Verify the replacement file moved to the original's directory, original is deleted

- [ ] **Step 6: Commit**

```bash
cd c:/Code/media-manager
git add frontend/index.html
git commit -m "Add replacement workflow overlay panel with fuzzy match review UI"
```

---

## Final Steps

- [ ] **Push branch and open PR**

```bash
cd c:/Code/media-manager
git push -u origin feature/quality-scanner-and-ux-improvements
gh pr create \
  --title "Quality Scanner — Replacement Workflow" \
  --body "$(cat <<'EOF'
## Summary
- Adds two-phase replacement workflow to the quality scanner
- Quality scanner now writes a machine-readable `results.json` alongside the HTML report
- New `/api/replace/match` endpoint fuzzy-matches flagged files against the Windows Downloads folder
- New `/api/replace/execute` endpoint moves replacements into place and deletes originals as a tracked async job
- UI: "Review Replacements" button on completed scan job cards opens a full-page overlay for review and confirmation

## Test plan
- [ ] Run quality scan with report path set — verify `.json` file appears alongside `.html`
- [ ] Review Replacements button appears on completed QS job cards with flagged files only
- [ ] Match endpoint returns correct fuzzy matches from /downloads
- [ ] Refresh Downloads preserves existing user confirmations
- [ ] Execute creates a replace_workflow job visible in Jobs tab with full action log
- [ ] Files that fail (missing, collision, permission) are skipped not aborted
- [ ] Path scope validation rejects paths outside source_dir or /downloads

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Share the PR URL with the user.
