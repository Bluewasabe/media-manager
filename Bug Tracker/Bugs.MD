# Bug Tracker — Media Manager

Active, in-review, and resolved bugs for this project.
See [CLAUDE.md](../CLAUDE.md) for the workflow.

---

## Open

| ID | Summary | Type |
|----|---------|------|
| 004 | Config options have no info bubbles/tooltips | Enhancement |
| 005 | No time/quantity metrics shown during or after jobs | Enhancement |

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

## In Review

Bugs below have been fixed in code. Awaiting your confirmation that they work after a rebuild.

| ID | Summary |
|----|---------|
| 006 | Execute mode crashes with EOFError (media_organizer + duplicate_finder archive) |
| 007 | Graceful shutdown — zombie cleanup, SIGTERM handlers, Re-run button |

### 006 — Execute mode crashes with EOFError (media_organizer + duplicate_finder archive)

*(moved to In Review — fix applied, needs rebuild to confirm)*

See Open section above for full detail.

---

### 007 — Graceful shutdown + zombie job cleanup

**Reported:** Restarting/rebuilding the container while a job is running leaves the job stuck in `running` status forever with no recovery path.

**Fix applied:**
- **Startup zombie cleanup** (`db.py` `interrupt_stale_jobs`): on startup, any job still in `running`/`pending` is marked `interrupted` and a WARN log is appended
- **SIGTERM handlers** in all three scripts: on Docker stop signal, script prints `[INTERRUPTED]` message and exits cleanly
- **Lifespan shutdown hook** (`main.py`): cancels running jobs and waits up to 30s for graceful exit before container stops
- **Docker stop grace period** (`docker-compose.yml`): `stop_grace_period: 60s` gives Docker 60s before SIGKILL
- **Uvicorn graceful shutdown** (`Dockerfile`): `--timeout-graceful-shutdown 30`
- **Frontend `interrupted` status** (`index.html`): amber pill; `interrupted` jobs appear in Job History; Re-run button restores config to the Scripts tab

**Files changed:** `db.py`, `main.py`, `Dockerfile`, `docker-compose.yml`, `backend/scripts/media_organizer.py`, `backend/scripts/disk_drill_organizer.py`, `backend/scripts/duplicate_finder.py`, `frontend/index.html`

---

## Resolved

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
