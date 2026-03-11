# CLAUDE.md — Media Manager

Instructions for Claude Code when working in this project.

---

## What This Is

A containerized web app (FastAPI + vanilla JS + Nginx) that runs three Python file management scripts through a browser UI. No CLI required. Runs at **http://localhost:45681** as part of the JARVIS home stack.

Part of the home lab stack at `c:/Code`. See `c:/Code/CLAUDE.md` for shared Git, Docker, and network standards that apply here too.

---

## Repository

| Field | Value |
|-------|-------|
| Local path | `c:/Code/media-manager` |
| GitHub | `github.com/Bluewasabe/media-manager` |
| Default branch | `main` |

---

## Project Layout

```
media-manager/
├── setup.ps1                    Detects drives, writes docker-compose.override.yml
├── docker-compose.yml           Base service definitions
├── docker-compose.override.yml  Per-machine drive mounts (gitignored, generated)
├── backend/
│   ├── main.py                  FastAPI entry point
│   ├── db.py                    SQLite via aiosqlite (WAL mode)
│   ├── job_runner.py            Async subprocess + WebSocket pub/sub
│   ├── routers/
│   │   ├── filesystem.py        /api/drives, /api/browse, /api/file
│   │   ├── jobs.py              /api/jobs, /ws/{job_id}
│   │   └── logs.py              /api/logs, /api/settings
│   └── scripts/                 Live-mounted — edits take effect on next job run
│       ├── media_organizer.py
│       ├── disk_drill_organizer.py
│       └── duplicate_finder.py
├── frontend/
│   ├── index.html               Full SPA (~1900 lines, vanilla JS + Tailwind)
│   └── nginx.conf               Serves static; proxies /api/ and /ws/ to backend
├── data/                        Docker volume — SQLite DB + generated HTML reports
├── docs/                        Script-level documentation
└── Bug Tracker/
    └── Bugs.MD                  Active, in-review, and resolved bug log
```

---

## Architecture Notes

- **Backend**: Python FastAPI, single SQLite DB (`/data/media.db`), WAL mode, aiosqlite
- **Frontend**: Single `index.html` — no build step, no npm, no bundler
- **Scripts**: Live-mounted at `/app/scripts` — only this folder reloads without a rebuild
- **WebSocket**: One WS per active job; messages: `init`, `log`, `stats`, `status`, `done`, `ping`
- **Reports**: Dry-run HTML reports are written to the user's chosen output dir and served via `/api/file?path=...`

---

## What Requires a Rebuild

| Change | Rebuild needed? |
|--------|----------------|
| `backend/scripts/*.py` | No — live-mounted |
| `backend/*.py` or `backend/routers/*.py` | Yes — `docker-compose up -d --build` |
| `frontend/index.html` or `nginx.conf` | Yes — `docker-compose up -d --build` |
| `docker-compose.yml` | Yes — `docker-compose up -d` |

**Never restart or rebuild while a job is running.** Jobs are in-memory subprocesses; a container restart kills them mid-run.

---

## Bug Tracking

Active bugs are tracked in [Bug Tracker/Bugs.MD](Bug%20Tracker/Bugs.MD).

Workflow:
1. New bugs go in the **Open** section
2. After a code fix is written, move to **In Review** (awaiting user confirmation)
3. After the user confirms it works, move to **Resolved**

---

## Git Rules (project-specific)

Never commit:
- `docker-compose.override.yml` (machine-specific drive mounts)
- `data/` contents (SQLite DB, generated reports)
- `.env` files

Safe to commit: `docker-compose.yml`, `backend/**/*.py`, `frontend/index.html`, `setup.ps1`, `docs/`, `Bug Tracker/Bugs.MD`

### GitHub Push Policy — Always Use Pull Requests

**Never push directly to `main`.** All changes must go through a feature branch and PR for full observability.

```bash
# Create a branch, commit, then open a PR
git checkout -b feature/describe-change
git add <specific files>
git commit -m "Imperative description"
git push -u origin feature/describe-change
gh pr create --title "Short description" --body "What changed and why"
```

Share the PR URL with the user. Do not merge — leave merging to the user.
