# Bug Tracker — Media Manager

Active, in-review, and resolved bugs for this project.
See [CLAUDE.md](../CLAUDE.md) for the workflow.

---

## Open

| ID | Summary | Type |
|----|---------|------|
| 004 | Config options have no info bubbles/tooltips | Enhancement |
| 005 | No time/quantity metrics shown during or after jobs | Enhancement |
| 009 | DDO job stats always read 0 — extract_stats regex doesn't match DDO output format | Bug |
| 010 | Log timestamps display in UTC instead of local time | Bug |
| 011 | All scripts: logs and stats delayed — Python stdout buffering when piped | Bug |

### 004 — Config options have no info bubbles/tooltips

**Reported:** The script config panels have toggleable options (e.g. "Perceptual Hashing", "Skip Video Metadata", "Include Filtered", "Min File Size") with no in-UI explanation of what they do. The README documents them, but the user has to leave the app to find out what a toggle actually does.

The README even states "Hover over any option label to see what it does" — this behavior was intended but never implemented.

**Scope:**
- All three config panels: Media Organizer, Disk Drill Organizer, Duplicate Finder
- Every toggle, number input, and segmented control that isn't self-evident
- Tooltips should appear on hover (desktop) or tap (mobile) without disrupting layout
- Suggested approach: a small `ⓘ` icon next to each label; on hover, a floating tooltip with 1–2 sentence explanation pulled from the existing README docs

**Files to change:** `frontend/index.html`

---

### 005 — No time/quantity metrics shown during or after jobs

**Reported:** No elapsed time, total file count, estimated completion time, or transfer rate is visible during a running job. Completed jobs show only "X moved / X errors" in the history row with no duration or data summary.

**Scope:**

*During a running job (active card):*
- Live elapsed timer (`HH:MM:SS`) next to the status pill
- Total files to process (parsed from scan output — script already prints `Keeping: N`)
- Real progress bar (not indeterminate) once total is known: `processed / total`
- Estimated time remaining based on rolling 60s rate average
- Files/sec or MB/s rate indicator

*On job completion:*
- Summary banner at top of "View Logs" panel: duration, files processed, data moved, files filtered, start→end timestamps
- Duration shown in history row (calculated from `created_at` → `updated_at`)

*Dashboard:*
- Optional: aggregate "Total Data Processed" stat card

**Implementation notes:**
- Elapsed timer: pure frontend `setInterval`, no backend needed
- Total files: needs new `total_files` stat key captured in `extract_stats` by matching the script's `Keeping: N` line
- Progress bar: frontend calculates `processed / total_files` once both are known
- ETA: `(elapsed_seconds / processed) × (total_files - processed)`, use rolling 60s window for stability
- Job summary banner: all data already in DB (`created_at`, `updated_at`, `stats`) — frontend-only change

**Files to change:** `backend/job_runner.py` (add `total_files` stat extraction), `frontend/index.html`

---

### 009 — DDO job stats always read 0 (extract_stats regex mismatch)

**Reported:** During a Disk Drill Organizer job the SCANNED, PROCESSED, MOVED, and ERRORS counters all stay at 0 throughout the job. Phase also stays stuck on "Scanning" even after the scan completes.

**Root cause:** The `extract_stats` and `detect_phase` functions in `job_runner.py` use regexes tuned for `media_organizer.py` output. DDO prints different formats that none of the patterns match:

| What DDO actually prints | What the regex expects | Result |
|--------------------------|------------------------|--------|
| `Found 1234 files.` | `N files found/scanned` (reversed) | no match → SCANNED stays 0 |
| `  ... scanned 1000/5000` | `N files scanned` | no match |
| `[DONE] 500 files moved. 2 errors.` | `N moved` (no "files" between) | no match → MOVED stays 0 |
| `Breakdown by destination folder:` | none of the phase keywords | phase stuck at "scanning" |
| `  Videos/... 2 files` | none | no match |

The `[DONE]` errors pattern (`\d+\s+errors`) would match DDO's `2 errors.` but only fires at job end, and MOVED is never captured at all.

**Fix needed (`backend/job_runner.py`):**
- Add DDO-specific patterns to `extract_stats`:
  - `r'Found\s+(\d[\d,]*)\s+files'` → `scanned`
  - `r'(\d[\d,]*)\s+files\s+(?:moved|copied)'` → `moved`
  - `r'\.\s+(\d[\d,]*)\s+errors'` → `errors` (from `[DONE]` line)
  - `r'\.\.\.\s+(\d[\d,]*)/(\d[\d,]*)$'` → `scanned` progress updates
- Add `'breakdown'`, `'keeping'`, `'filtered'` to phase keywords to advance past scanning

**Files to change:** `backend/job_runner.py`

---

### 010 — Log timestamps display in UTC instead of local time

**Reported:** Timestamps shown in the live log stream and job history appear in Zulu/UTC time rather than the machine's local timezone (Eastern). e.g. `01:35:16` shows when local time is ~9:35 PM Eastern.

**Root cause:** `job_runner.py` uses `datetime.utcnow().isoformat()` to generate timestamps, which stores UTC strings in the DB. The frontend displays them as-is with no timezone conversion.

**Fix options:**
- **Preferred — frontend conversion:** UTC storage is correct practice. Parse the ISO timestamp string in JS and call `.toLocaleTimeString()` (or similar) so timestamps render in the browser's local timezone. No backend change, no rebuild needed for backend.
- **Alternative — store local time:** Switch `datetime.utcnow()` → `datetime.now()` in `job_runner.py`. Simpler but loses timezone awareness if the app ever runs on a server in a different timezone.

**Recommended fix:** Frontend-only — convert UTC ISO strings to local time at display. Affects the live log stream (`appendLiveLog`), the "View Logs" panel, and any timestamp shown in job cards.

**Files to change:** `frontend/index.html` (frontend conversion approach) or `backend/job_runner.py` (store local time approach) — requires rebuild either way.

### 011 — All scripts: logs and stats delayed (Python stdout buffering)

**Reported:** During any job (DDO, Media Organizer, Duplicate Finder), the log pane stays blank and stats remain at 0 for an extended period after the job starts ("long pending"). Output eventually appears in a burst when Python's internal stdout buffer fills (~8 KB) rather than line-by-line.

**Root cause:** When Python is run as a subprocess with `stdout=PIPE`, it switches from line-buffered to fully-buffered mode. The runner reads lines with `async for raw in proc.stdout` but no lines arrive until the buffer flushes, causing all stats and log updates to be delayed or arrive in a single burst at job end.

**Fix applied:** Added `-u` (unbuffered) flag to all three `python` invocations in `build_args` in `backend/routers/jobs.py`. This forces Python to write stdout byte-by-byte (effectively line-by-line for print statements), so every output line reaches the runner and the WebSocket immediately.

**Files changed:** `backend/routers/jobs.py`

---

## In Review

| ID | Summary |
|----|---------|
| 009 | DDO job stats always read 0 — extract_stats regex mismatch |
| 010 | Log timestamps display in UTC instead of local time |
| 011 | All scripts: logs and stats delayed — Python stdout buffering when piped |

---

## Resolved

### 008 — SIGTERM handler hangs when ThreadPoolExecutor is active (disk_drill_organizer, duplicate_finder)

**Reported:** Stopping the container while a disk_drill_organizer or duplicate_finder job was running caused the backend process to hang indefinitely, requiring a full machine reboot.

**Root cause:** The SIGTERM signal handlers in all three scripts called `sys.exit(0)`, which raises `SystemExit`. When called from inside an active `ThreadPoolExecutor` `with` block, Python's `ThreadPoolExecutor.__exit__` intercepts `SystemExit` and calls `shutdown(wait=True)` — blocking until every worker thread finishes. With hundreds or thousands of files being hashed/classified in parallel, this could take minutes or never complete. Docker's SIGKILL would eventually fire, but the uvicorn process was already wedged.

**Fix applied:**
- Changed `sys.exit(0)` → `os._exit(0)` in the `_handle_sigterm` function in all three scripts
- `os._exit()` bypasses Python's cleanup machinery entirely and terminates the process immediately — threads, executors, and atexit handlers are never consulted
- Added `import os` to `media_organizer.py` and `duplicate_finder.py` (was already present in `disk_drill_organizer.py`)
- DB state cleanup is handled by `interrupt_stale_jobs()` on next startup, so no data is lost

**Files changed:** `backend/scripts/disk_drill_organizer.py`, `backend/scripts/media_organizer.py`, `backend/scripts/duplicate_finder.py`

---

### 006 — Execute mode crashes with EOFError (media_organizer + duplicate_finder archive)

*(confirmed resolved)*

---

### 007 — Graceful shutdown + zombie job cleanup

**Confirmed resolved:** On startup the backend correctly detects and marks stale `running`/`pending` jobs as `interrupted`. Verified in logs: `[startup] Marked 1 stale job(s) as interrupted from previous run`.

**Fix applied:**
- **Startup zombie cleanup** (`db.py` `interrupt_stale_jobs`): on startup, any job still in `running`/`pending` is marked `interrupted` and a WARN log is appended
- **SIGTERM handlers** in all three scripts: on Docker stop signal, script prints `[INTERRUPTED]` message and exits cleanly
- **Lifespan shutdown hook** (`main.py`): cancels running jobs and waits up to 30s for graceful exit before container stops
- **Docker stop grace period** (`docker-compose.yml`): `stop_grace_period: 60s` gives Docker 60s before SIGKILL
- **Uvicorn graceful shutdown** (`Dockerfile`): `--timeout-graceful-shutdown 30`
- **Frontend `interrupted` status** (`index.html`): amber pill; `interrupted` jobs appear in Job History; Re-run button restores config to the Scripts tab

**Files changed:** `db.py`, `main.py`, `Dockerfile`, `docker-compose.yml`, `backend/scripts/media_organizer.py`, `backend/scripts/disk_drill_organizer.py`, `backend/scripts/duplicate_finder.py`, `frontend/index.html`

---

### 001 — Jobs real-time tracker stats don't populate

**Root cause:** WebSocket path mismatch in nginx (`/ws/` proxied to `/ws/` instead of `/api/ws/`), causing 403 on every WS connection. Secondary: stats fallback read `job.stats` (DB snapshot zeros) instead of WS-cached values.

**Fix applied:** nginx.conf `proxy_pass` corrected to `http://backend:8000/api/ws/`; `activeJobCard` reads `state.jobStats[job.id] || job.stats || {}`.

**Files changed:** `frontend/nginx.conf`, `frontend/index.html`

---

### 002 — Logs do not stream in real time

**Root cause:** Same nginx WS 403 as 001. Secondary: log line CSS class was `log-line INFO` but selectors defined `.level-INFO`.

**Fix applied:** nginx fix from 001; CSS class corrected to `log-line level-${level}` in `activeJobCard` and `appendLiveLog`.

**Files changed:** `frontend/nginx.conf`, `frontend/index.html`

---

### 003 — Dry run HTML report is not clickable in Explorer

**Fix applied:** New `GET /api/file?path=...` backend endpoint; Explorer detects `.html`/`.htm` and opens via `/api/file`.

**Files changed:** `backend/routers/filesystem.py`, `frontend/index.html`
