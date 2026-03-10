# File Manager — JARVIS

A containerized web application for running three file management scripts through a graphical interface. No command line required. Browse your drives, configure jobs, watch them run in real time, and review logs — all from the browser.

Runs at **http://localhost:45681** as part of the JARVIS home stack.

---

## Table of Contents

1. [User Guide](#user-guide)
   - [First-Time Setup](#first-time-setup)
   - [The Scripts](#the-scripts)
   - [Running a Job](#running-a-job)
   - [Watching Progress](#watching-progress)
   - [Reviewing Logs](#reviewing-logs)
   - [Settings](#settings)
2. [Developer Reference](#developer-reference)
   - [Repository Layout](#repository-layout)
   - [Architecture Decisions](#architecture-decisions)
   - [Backend Deep Dive](#backend-deep-dive)
   - [Frontend Deep Dive](#frontend-deep-dive)
   - [Docker & Networking](#docker--networking)
   - [Data & Retention](#data--retention)
   - [Extending the App](#extending-the-app)

---

# User Guide

## First-Time Setup

**You only need to do this once per machine, and again any time you add or remove drives.**

**1. Detect your drives**

Open PowerShell in the `media-manager` folder and run:

```powershell
.\setup.ps1
```

This scans every drive Windows can see — local, external, and mapped network drives — and writes a `docker-compose.override.yml` file that tells Docker how to mount them into the container. You will see a list of everything found.

**2. Make sure the shared network exists**

```bash
docker network create jarvis-net
```

Skip this if you already run other JARVIS services (mission-control, n8n, etc.) — the network already exists.

**3. Start the application**

```bash
docker-compose up -d --build
```

Open **http://localhost:45681** in your browser.

---

## The Scripts

The app wraps three Python scripts. Each one is safe to preview before committing to any file operations — every script defaults to **Dry Run** mode, which scans and reports but never moves, copies, or deletes anything.

### Media Organizer

Renames and restructures movie and TV show files into the folder layout expected by Plex and Jellyfin.

- **Movies** are grouped into `Movie Title (Year)/Movie Title (Year).mkv` folders. Multi-part films (CD1, Disc2, Part 1) are unified under one folder and renamed `- part1`, `- part2`, etc. Collection folders with mixed titles are split into individual film folders.
- **TV shows** are organized into `Show Name/Season ##/Show Name - S##E##.ext`. Season numbers are inferred from parent folder names when missing from the filename.
- Filename junk (quality tags, codec tags, release group names) is stripped automatically. Title casing is applied with smart rules for articles, prepositions, and Roman numerals.

Full documentation: [docs/media-organizer.md](docs/media-organizer.md)

### Disk Drill Organizer

Parses and reorganizes raw output from Disk Drill recovery software into a clean, human-readable structure.

- Photos are sorted by EXIF device and date into `Photos/Device/Year/Month/` folders.
- Videos are sorted by device or duration into `Videos/Device/Year/Month/`.
- Corrupt, tiny, and short files go into `Manually Review/` subfolders for human triage rather than being silently discarded.
- An HTML report is always generated — even in dry-run — showing what was found, filtered, and where everything landed.

Full documentation: [docs/disk-drill-organizer.md](docs/disk-drill-organizer.md)

### Duplicate Finder

Scans multiple drives, finds exact and near-duplicate files, scores every copy by quality, and consolidates the best version.

- **Exact duplicates** are found by SHA-256 (or MD5) hash — byte-identical files across any source.
- **Near-duplicate video** matches the same title + year (or show + season + episode) with duration within ±5% — catches different encodes of the same content.
- **Near-duplicate photos** (optional) uses perceptual hashing to find the same image at different compression levels or resolutions.
- Quality scoring picks the best copy: for video it weights resolution, codec, bitrate, container, and audio; for photos it weights resolution, format (RAW beats JPEG), and EXIF completeness.
- Sources are listed in priority order — the leftmost source wins all tie-breaks regardless of score.

Full documentation: [docs/duplicate-finder.md](docs/duplicate-finder.md)

---

## Running a Job

**1. Go to the Scripts tab.**

Three cards are shown, one per script. Click **Configure** on the one you want to run.

**2. Fill in the paths.**

Click the folder icon next to any path field to open the file browser. It shows every drive that was found during setup, with free space displayed. Navigate by clicking folders. Click **Select This Folder** when you are in the right place.

For the Duplicate Finder, you can add multiple source folders in priority order. The topmost source wins all tie-breaks.

**3. Set options.**

Each script exposes its full set of options as toggles and number inputs — no flags to memorize. Hover over any option label to see what it does.

**4. Choose Dry Run or Execute.**

The mode toggle at the bottom of every config panel is the most important control:

- **Dry Run** (purple): Scans everything, prints what *would* happen, writes the HTML report. Nothing is touched on disk.
- **Execute** (red): Actually moves, copies, or archives files. The button turns red as a deliberate visual reminder.

Always run a dry run first on a new configuration.

**5. Click Run.**

The app switches to the Jobs tab and your job appears immediately. For destructive operations (Move or Delete), the app sends the required confirmation automatically since you already confirmed in the UI.

---

## Watching Progress

The Jobs tab shows two sections:

**Active Jobs** appear at the top with a live card for each running job:

- **Phase indicator** — four dots showing Scanning → Processing → Moving → Done. The active phase pulses.
- **Stats** — files scanned, processed, moved, and errors update in real time as lines stream from the script.
- **Current file** — the file the script is working on right now.
- **Live log stream** — the last 50 lines of raw script output, color-coded: INFO in gray, WARN in amber, ERROR in red, DRY RUN previews in purple.
- **Cancel** — stops the running process cleanly.

**History** shows completed jobs below, with a quick stat summary and a button to expand the full log.

Updates are delivered over WebSocket — there is no polling delay. If the connection drops (container restart, network hiccup), the frontend reconnects automatically.

---

## Reviewing Logs

The Logs tab gives you a searchable, filterable view of everything every job has ever printed.

- Filter by **level** (INFO / WARN / ERROR / DRY) to focus on problems or dry-run previews.
- Filter by **script** to see only Media Organizer runs, for example.
- **Search** matches anywhere in the log message.
- Each row shows the timestamp, level, which script produced it, and the full message.

Logs are retained for 30 days by default and capped at 500 jobs. Both limits are configurable in Settings.

---

## Settings

| Setting | Default | What it controls |
|---|---|---|
| Log Retention Days | 30 | Job logs older than this are deleted on the nightly cleanup |
| Max Jobs to Keep | 500 | Oldest jobs beyond this count are purged regardless of age |
| Security Mode | Off | When on, the file browser only shows drives mounted via setup.ps1 and any extra paths you add. When off, the browser can navigate the full container filesystem. |
| Extra Paths | — | Additional directories to expose in the file browser (e.g. UNC paths you have mapped as drives) |

Re-run `setup.ps1` and restart the containers any time you want to add or remove drives.

---

# Developer Reference

## Repository Layout

```
media-manager/
│
├── setup.ps1                   Windows drive detection; generates docker-compose.override.yml
├── docker-compose.yml          Base service definitions (no drive mounts — those go in override)
├── .gitignore
│
├── data/                       Docker volume mount target; holds the SQLite database
│   └── .gitkeep
│
├── docs/                       Script-level documentation (pulled from media-organizer repo)
│   ├── media-organizer.md
│   ├── disk-drill-organizer.md
│   └── duplicate-finder.md
│
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py                 FastAPI application entry point; lifespan, middleware, router mounting
│   ├── db.py                   All SQLite access; single connection, WAL mode, all CRUD functions
│   ├── job_runner.py           Async subprocess execution, output parsing, WebSocket pub/sub
│   ├── scripts/                The three Python scripts (version-controlled, live-mounted)
│   │   ├── media_organizer.py
│   │   ├── disk_drill_organizer.py
│   │   └── duplicate_finder.py
│   └── routers/
│       ├── __init__.py
│       ├── filesystem.py       /api/drives and /api/browse endpoints
│       ├── jobs.py             /api/jobs CRUD, /ws/{job_id} WebSocket, CLI argument builder
│       └── logs.py             /api/logs query, /api/settings CRUD, /api/cleanup
│
└── frontend/
    ├── Dockerfile
    ├── nginx.conf              Serves static files; proxies /api/ and /ws/ to backend container
    └── index.html              Complete single-page application (~1900 lines)
```

---

## Architecture Decisions

### Why FastAPI for the backend?

The scripts are Python. The backend needs to run them as subprocesses and stream their output in real time. Python's `asyncio` event loop handles this cleanly with `asyncio.create_subprocess_exec` — the event loop stays unblocked while waiting for lines from the child process. FastAPI is built on that same event loop, so subprocess execution, database writes, and WebSocket broadcasts all cooperate on one thread without any synchronization primitives.

A Node or Go backend would have required an extra IPC layer to manage the Python child processes. Staying in Python eliminates that entirely.

### Why SQLite instead of Postgres?

This application has one writer (the job runner) and a handful of readers (the frontend, the log viewer, settings). SQLite with WAL mode handles this perfectly — WAL allows concurrent readers while a write is in progress, which is exactly the access pattern here. There is no need for a separate database process, no connection pooling to configure, no migration tooling to run. The database file lives in the `data/` volume alongside anything else the scripts generate.

The alternative — Postgres — would have added a third container, required a startup dependency chain, and needed a connection pool. The complexity is not justified for this workload.

### Why WAL mode specifically?

Default SQLite journal mode takes an exclusive write lock that blocks all readers. WAL (Write-Ahead Log) separates reads from writes at the page level. Since the job runner writes log lines continuously while the frontend is reading them over WebSocket, WAL is the correct mode. Without it, a long-running job would cause visible stalls in the log stream.

### Why vanilla JavaScript instead of React or Vue?

Mission-control — the existing JARVIS dashboard this app is visually matched to — is vanilla JS with Tailwind. Consistency in the stack reduces maintenance overhead across the system. There is no build step, no `node_modules`, no bundler to configure. The entire frontend is one HTML file that Nginx serves directly. Modifying the UI means editing one file. Deploying the UI means copying one file.

A framework would have been appropriate if the frontend were complex enough to justify it. A dashboard with six tabs, a file browser, and live charts does not meet that bar.

### Why Nginx in front of the frontend instead of serving from FastAPI?

Separation of concerns. Nginx is purpose-built for serving static files efficiently. It also handles the WebSocket upgrade headers (`Connection: upgrade`, `Upgrade: websocket`) cleanly in its `proxy_pass` configuration. Serving both static files and proxying WebSockets from FastAPI would have required additional middleware and made the WebSocket path more fragile.

The nginx container is also where the port is exposed (45681). The backend is internal to the Docker network — it has no exposed port, which is correct: nothing outside the JARVIS network needs direct API access.

### Why is `docker-compose.override.yml` gitignored?

Drive letters are a per-machine concern. A machine with `C:`, `D:`, and `E:` needs different volume mounts than a machine with `C:` and `M:`. The override file is generated by `setup.ps1` locally and should never be committed — doing so would break every other machine in the LAN.

The base `docker-compose.yml` defines everything except drive mounts. Docker Compose merges the two files at runtime. This keeps the shared configuration clean and lets every machine manage its own drive layout independently.

---

## Backend Deep Dive

### `main.py`

The entry point does three things:

1. **Lifespan context** (`@asynccontextmanager async def lifespan`): Runs `init_db()` before the server starts accepting requests, ensuring the tables exist. Also spawns the nightly cleanup task as a background `asyncio.Task`. The lifespan pattern (replacing the deprecated `on_event` hooks) ties setup and teardown to the ASGI lifecycle correctly.

2. **CORS middleware**: All origins are allowed because the frontend and backend are on the same Docker network — requests come from the Nginx container proxying on behalf of the browser. The `*` wildcard is safe in this context since the backend has no exposed port.

3. **Router mounting**: Each router module is mounted at `/api`. This prefix is added here rather than in each router so that routers remain portable — they can be tested without the `/api` prefix in a different application context.

### `db.py`

The database layer uses a module-level singleton connection (`_db`). A single persistent connection is appropriate for SQLite — opening a new connection per request would be both unnecessary overhead and a source of locking contention.

**Why `row_factory = aiosqlite.Row`?** This makes rows behave like dicts (`row['column_name']`) rather than positional tuples. Every query result is immediately converted to `dict` before returning so callers never depend on SQLite row internals.

**`init_db()`** uses `CREATE TABLE IF NOT EXISTS` and `INSERT OR IGNORE INTO settings` so it is safely re-entrant. Calling it on a database that already has the schema is a no-op. This means no migration system is needed for the current schema.

**`cleanup_old_logs()`** enforces two independent limits:
- Age: logs for jobs older than `retention_days` are deleted first (FK cascade would handle this, but FK constraints are explicitly enabled via `PRAGMA foreign_keys=ON`).
- Count: jobs beyond `max_jobs` are pruned keeping only the most recent. The `LIMIT -1 OFFSET ?` trick selects every row after the Nth one, which is idiomatic SQLite for "all but the first N."

The two limits are applied in the same commit so the database is never left in a partial state.

**Why are stats stored as JSON text?** Stats are a variable-length dict that evolves as the scripts add new counters. Storing it as `TEXT` with `json.dumps/loads` avoids schema migrations when a new stat key is added. The tradeoff is that stats are not queryable by individual field — that is acceptable because no feature needs to filter jobs by stat value.

### `job_runner.py`

The `JobRunner` class is a singleton (`runner = JobRunner()` at module level, imported by `routers/jobs.py`). It owns two dicts:

- `_processes`: maps job ID to the live `asyncio.subprocess.Process`. Used for cancellation.
- `_subscribers`: maps job ID to a list of `asyncio.Queue` objects, one per connected WebSocket client.

**Why asyncio queues instead of direct WebSocket writes from the runner?**

The runner and the WebSocket handler run in separate coroutines. Directly calling `websocket.send_json()` from inside the runner's output loop would create a dependency between the subprocess coroutine and the network coroutine, making error handling significantly more complex — a slow client could block the runner. Queues decouple them: the runner enqueues messages at full speed; each WebSocket handler dequeues and sends at whatever pace the client can accept. `QueueFull` is caught silently (slow clients just miss log lines rather than blocking the runner).

**`clean_line()`**: Scripts may emit ANSI escape codes for terminal color output. These look like `\x1b[32m` and would appear as garbage in the frontend log viewer. The ANSI regex strips them before storage. `rstrip('\r\n')` handles both Unix and Windows line endings from the subprocess.

**`detect_level()`** and **`detect_phase()`**: Simple keyword matching against the lowercase line text. This is intentionally coarse — the scripts were not designed to emit structured log output. If a line contains "error" or "failed", it is tagged ERROR; if it contains "moving" or "copying", it advances the phase to `processing`. False positives (a filename containing "error") will misclassify occasionally, but the overall progress display remains accurate enough for the intended use case.

**`needs_confirmation`**: The `--move` flag in Disk Drill Organizer and the `--action delete` flag in Duplicate Finder both cause the scripts to read `YES` from stdin before proceeding. Rather than surfacing a confirmation prompt in the browser mid-job, the UI collects the intent upfront (the user has already clicked "Move" or "Delete" and seen the warning). `job_runner.py` writes `YES\n` to stdin immediately after process start when `needs_confirmation` is true.

**Cancellation**: `asyncio.CancelledError` is caught to handle the case where the task is cancelled externally (not common in current usage, but defensive). The more typical cancellation path is `runner.cancel(job_id)` from the DELETE endpoint, which calls `proc.terminate()` (SIGTERM). The script is expected to exit cleanly; if it does not within 5 seconds, `proc.kill()` (SIGKILL) follows.

### `routers/filesystem.py`

**`/api/drives`**: Reads the `DRIVES_DIR` environment variable (defaults to `/mnt/drives`). Each subdirectory of that path represents a drive mounted by `setup.ps1`. `os.statvfs()` provides block counts and block size from which free and total bytes are calculated. Drives that cannot be stat'd (unmounted, permissions issue) are reported as inaccessible rather than omitted — the UI can show them grayed out, which is more informative than silence.

Extra paths from the `settings` table are appended to the drive list. These are intended for UNC paths or other directories that are already accessible to the container but were not auto-detected by setup.ps1.

**`container_to_display()`**: The browser internally uses Linux paths (`/mnt/drives/d/Movies`). Every path displayed in the UI is converted to Windows format (`D:\Movies`) using this function. The inverse conversion happens on the backend when building script arguments: the scripts receive the container-internal path (which is what they need to operate on inside the container's filesystem).

**`/api/browse`**: `os.scandir()` is used rather than `os.listdir()` because scandir provides `DirEntry` objects that include `is_dir()` and `stat()` results without a second syscall — important for large directories. Items are sorted folders-first, then alphabetically within each group. Inaccessible entries are included in the result with an `inaccessible: true` flag rather than raising an error, so the UI can display them greyed out.

Security mode enforcement uses `os.path.abspath()` on both the requested path and each allowed root before the `startswith()` comparison. This prevents path traversal via `..` segments.

### `routers/jobs.py`

**`build_args()`**: Translates the JSON config object from the frontend into a list of CLI arguments for the subprocess. Each script has its own branch. Default values are compared explicitly — only non-default values generate CLI flags. This mirrors the script defaults so that a job launched from the UI with default settings produces the same result as running the script with no flags from the command line.

The function also returns `needs_confirmation: bool` so the caller knows whether to feed `YES` to stdin.

**`POST /api/jobs`**: Creates the job record in the database first (status = `pending`), then starts the runner as an `asyncio.Task` in the background. The HTTP response returns immediately with the job ID — the client does not wait for the job to complete. The background task runs until the script exits.

**`/ws/{job_id}`** (WebSocket): Sends the current job state immediately on connect (via `GET /api/jobs/{id}`) so a client that connects after the job has already started sees the current stats rather than waiting for the next update. The heartbeat ping (every 30 seconds) keeps the connection alive through proxies and NAT that would otherwise close idle WebSocket connections.

### `routers/logs.py`

Settings are stored in the `settings` table as key-value text pairs. The allowlist in `PUT /api/settings/{key}` prevents arbitrary key creation — only the four known keys can be written. This is belt-and-suspenders validation on top of the fact that the frontend already constrains which settings it sends.

---

## Frontend Deep Dive

### Why a single HTML file?

The entire frontend is `frontend/index.html`. This matches the mission-control pattern already established in the JARVIS stack. There is no build step, no package manager, no source maps to debug. Nginx copies the file directly into the container image. When you change the UI, you change one file and rebuild.

The tradeoff is that the file is long (~1900 lines). This is manageable because the structure is strictly segmented: CSS variables and component styles at the top, HTML markup in the middle, JavaScript at the bottom, with the JS organized into clearly labeled sections (API client, state, browser, each tab, utilities).

### State management without a framework

State is a plain object (`const state = { ... }`). Tabs are rendered by calling a `render*Tab()` function that writes to the tab panel's `innerHTML`. Updates arrive over WebSocket and call targeted DOM update functions (`updateJobStats()`, `appendJobLog()`) rather than re-rendering the whole tab. This is fast enough for the update frequency involved (tens of log lines per second) without a virtual DOM.

Config panel state (the values in script forms) is saved to `localStorage` on every change so that a page reload restores the last configuration. This is especially useful during development or when the user wants to re-run a job with slightly different settings.

### File browser design

The browser is built as a modal overlay. It is also embedded as the Explorer tab (same component, different container). This reuse was deliberate: the same `browserNavigate()`, `renderDriveGrid()`, and `renderBrowserItems()` functions power both contexts. The distinction is whether clicking "Select This Folder" fills an input field (modal) or does nothing (explorer tab, which is for orientation only).

Path representation in the browser is always the container-internal path (`/mnt/drives/d/Movies`). The `containerToDisplay()` function converts to Windows format (`D:\Movies`) for every label shown to the user. Script arguments are built from the container path. The user never needs to know or type a Linux path.

### WebSocket lifecycle

Each running job gets its own WebSocket connection opened when the job card is rendered. Connections are stored in `state.jobWS` keyed by job ID. When a `done` message arrives, the socket is closed and the key is removed. If the socket closes unexpectedly (container restart, network blip), the frontend attempts one reconnect after a short delay. The reconnected socket will receive the current job state immediately from the server (the `init` message).

### Live log display

Logs are appended to a pre-allocated `<div>` with `insertAdjacentHTML('beforeend', ...)`. This avoids re-rendering the entire log container on each new line. The container is capped at 50 visible lines — older lines are removed from the DOM when the cap is reached to prevent the browser from accumulating unbounded DOM nodes during a long job.

Auto-scrolling is paused if the user scrolls up (detected by comparing `scrollTop` and `scrollHeight - clientHeight`). It resumes when the user scrolls back to the bottom. This is standard behavior for any terminal emulator and prevents the log from snapping away from text the user is reading.

---

## Docker & Networking

### Service graph

```
[ browser ]
     │ HTTP/WS port 45681
     ▼
[ frontend (nginx) ] ──── /api/* ──► [ backend (FastAPI:8000) ]
                     ──── /ws/*  ──►         │
                                           reads/writes
                                              │
                                         [ data/ volume ]
                                         (SQLite + reports)
                                              │
                                    mounts /mnt/drives/*
                                    (generated by setup.ps1)
```

The backend has no exposed port. It is reachable only from the nginx container over the `jarvis-net` Docker bridge network. This is intentional — the backend API has no authentication, so it should not be directly reachable from outside the Docker network.

### Volume strategy

| Mount | Purpose |
|---|---|
| `./data:/data` | Persistent SQLite database and any HTML reports scripts write |
| `./backend/scripts:/app/scripts` | The three Python scripts; live-mounted so edits take effect on the next job without a rebuild |
| `C:/:/mnt/drives/c` etc. | Drive mounts generated by setup.ps1; added to backend only via docker-compose.override.yml |

The scripts volume is read-write from the container's perspective (the scripts may write report files relative to their working directory). The drive mounts are read-write by default because the scripts need to move files. A future enhancement could mount source drives read-only during dry-run mode.

### `setup.ps1` design

`Get-PSDrive -PSProvider FileSystem` returns every filesystem drive PowerShell can see, including mapped network drives. The script builds a `docker-compose.override.yml` that adds volume entries to the `backend` service. Docker Compose automatically merges `docker-compose.yml` and `docker-compose.override.yml` when both are present.

The override file also re-declares the `./data:/data` and `./backend/scripts:/app/scripts` mounts. Docker Compose merge semantics for volumes are additive when keys differ but last-write-wins when keys match, so the re-declaration is explicit rather than relying on merge order.

---

## Data & Retention

### Database schema

```sql
jobs
  id           TEXT    UUID, primary key
  script       TEXT    'media_organizer' | 'disk_drill' | 'duplicate_finder'
  config       TEXT    JSON — the form values submitted by the user
  args         TEXT    JSON — the exact CLI args passed to the subprocess
  status       TEXT    'pending' | 'running' | 'completed' | 'failed' | 'cancelled'
  created_at   TEXT    ISO 8601 UTC
  updated_at   TEXT    ISO 8601 UTC
  stats        TEXT    JSON — {scanned, processed, moved, errors, skipped, phase, current_file}
  exit_code    INTEGER NULL until job finishes

job_logs
  id           INTEGER autoincrement
  job_id       TEXT    FK → jobs.id
  timestamp    TEXT    ISO 8601 UTC
  level        TEXT    'INFO' | 'WARN' | 'ERROR' | 'DRY'
  message      TEXT    one line of script stdout (ANSI stripped)

settings
  key          TEXT    primary key
  value        TEXT    stored as text; parsed by caller
```

**Indexes**: `job_logs(job_id)` is critical — every log query filters by job ID. Without this index, displaying logs for a single job would do a full table scan across potentially millions of rows. `jobs(status)` speeds up the active-jobs query. `jobs(created_at)` speeds up the cleanup query that finds old jobs.

### Retention enforcement

Two limits run together nightly:

- **Age limit**: Any job older than `log_retention_days` days has its logs deleted. The jobs record itself is also removed.
- **Count limit**: If the job table exceeds `max_jobs`, the oldest jobs are removed until it is within the limit. This prevents unbounded growth even if jobs run very frequently.

Cleanup runs as a background `asyncio.Task` started in `main.py`'s lifespan. It sleeps 24 hours between runs. There is no external scheduler — the application manages its own housekeeping, which is appropriate for a single-node deployment.

---

## Extending the App

### Add a new script

1. Drop the Python script into `backend/scripts/`.
2. Add a `build_args()` branch in `routers/jobs.py` that maps the config dict to the script's CLI arguments.
3. Add a config panel in `frontend/index.html` following the pattern of the existing three panels.
4. Add the script name to the `SCRIPT_LABELS` constant at the top of the frontend JS.

### Add a new setting

1. Add `('key', 'default_value')` to the `defaults` list in `db.py`'s `init_db()`.
2. Add the key to the `allowed` list in `routers/logs.py`'s `PUT /api/settings/{key}`.
3. Add a field to the Settings panel in `frontend/index.html`.

### Add a new drive source type

Currently only drives visible to `Get-PSDrive` are auto-detected. To support SMB shares that are not mapped to drive letters:

1. Add the UNC path as an extra path in the Settings UI (it must be accessible from the host and mounted into the container).
2. Alternatively, map the share to a drive letter in Windows before running `setup.ps1`.

### Expose the backend port for local API access

Add a `ports` entry to the `backend` service in `docker-compose.yml`:

```yaml
ports:
  - "45682:8000"
```

FastAPI's automatic docs are then available at `http://localhost:45682/docs`.

---

## Script Documentation

The full documentation for each underlying script lives in the `docs/` folder:

- [Media Organizer](docs/media-organizer.md) — Plex/Jellyfin file renaming and restructuring
- [Disk Drill Organizer](docs/disk-drill-organizer.md) — Recovery output triage and sorting
- [Duplicate Finder](docs/duplicate-finder.md) — Multi-drive deduplication with quality scoring
