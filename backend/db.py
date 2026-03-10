import aiosqlite
import json
import uuid
import os
from datetime import datetime

DB_PATH = os.path.join(os.environ.get('DATA_DIR', '/data'), 'media_manager.db')

_db: aiosqlite.Connection = None


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        _db = await aiosqlite.connect(DB_PATH)
        _db.row_factory = aiosqlite.Row
        await _db.execute("PRAGMA journal_mode=WAL")
        await _db.execute("PRAGMA foreign_keys=ON")
    return _db


async def init_db():
    db = await get_db()
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            script TEXT NOT NULL,
            config TEXT NOT NULL,
            args TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            stats TEXT,
            exit_code INTEGER
        );

        CREATE TABLE IF NOT EXISTS job_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            level TEXT NOT NULL,
            message TEXT NOT NULL,
            FOREIGN KEY (job_id) REFERENCES jobs(id)
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_job_logs_job_id ON job_logs(job_id);
        CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
        CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at);
    """)

    # Default settings
    defaults = [
        ('log_retention_days', '30'),
        ('max_jobs', '500'),
        ('security_mode', 'false'),
        ('extra_paths', '[]'),
    ]
    for key, value in defaults:
        await db.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, value)
        )
    await db.commit()


async def create_job(script: str, config: dict, args: list) -> str:
    db = await get_db()
    job_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    await db.execute(
        """INSERT INTO jobs (id, script, config, args, status, created_at, updated_at, stats)
           VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)""",
        (job_id, script, json.dumps(config), json.dumps(args), now, now,
         json.dumps({'scanned': 0, 'processed': 0, 'moved': 0, 'errors': 0, 'skipped': 0,
                     'phase': 'pending', 'current_file': ''}))
    )
    await db.commit()
    return job_id


async def update_job_status(job_id: str, status: str, exit_code: int = None):
    db = await get_db()
    now = datetime.utcnow().isoformat()
    if exit_code is not None:
        await db.execute(
            "UPDATE jobs SET status=?, exit_code=?, updated_at=? WHERE id=?",
            (status, exit_code, now, job_id)
        )
    else:
        await db.execute(
            "UPDATE jobs SET status=?, updated_at=? WHERE id=?",
            (status, now, job_id)
        )
    await db.commit()


async def update_job_stats(job_id: str, stats_dict: dict):
    db = await get_db()
    now = datetime.utcnow().isoformat()
    await db.execute(
        "UPDATE jobs SET stats=?, updated_at=? WHERE id=?",
        (json.dumps(stats_dict), now, job_id)
    )
    await db.commit()


async def add_log(job_id: str, timestamp: str, level: str, message: str):
    db = await get_db()
    await db.execute(
        "INSERT INTO job_logs (job_id, timestamp, level, message) VALUES (?, ?, ?, ?)",
        (job_id, timestamp, level, message)
    )
    await db.commit()


async def get_jobs(limit: int = 50, offset: int = 0, status_filter: str = None) -> list:
    db = await get_db()
    if status_filter:
        cursor = await db.execute(
            "SELECT * FROM jobs WHERE status=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (status_filter, limit, offset)
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset)
        )
    rows = await cursor.fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d['config'] = json.loads(d['config']) if d['config'] else {}
        d['args'] = json.loads(d['args']) if d['args'] else []
        d['stats'] = json.loads(d['stats']) if d['stats'] else {}
        result.append(d)
    return result


async def get_job(job_id: str) -> dict:
    db = await get_db()
    cursor = await db.execute("SELECT * FROM jobs WHERE id=?", (job_id,))
    row = await cursor.fetchone()
    if not row:
        return None
    d = dict(row)
    d['config'] = json.loads(d['config']) if d['config'] else {}
    d['args'] = json.loads(d['args']) if d['args'] else []
    d['stats'] = json.loads(d['stats']) if d['stats'] else {}
    return d


async def get_logs(job_id: str = None, level: str = None, search: str = None,
                   limit: int = 200, offset: int = 0) -> list:
    db = await get_db()
    conditions = []
    params = []

    if job_id:
        conditions.append("jl.job_id = ?")
        params.append(job_id)
    if level:
        conditions.append("jl.level = ?")
        params.append(level)
    if search:
        conditions.append("jl.message LIKE ?")
        params.append(f"%{search}%")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    query = f"""
        SELECT jl.id, jl.job_id, jl.timestamp, jl.level, jl.message, j.script
        FROM job_logs jl
        LEFT JOIN jobs j ON j.id = jl.job_id
        {where}
        ORDER BY jl.id DESC
        LIMIT ? OFFSET ?
    """
    params += [limit, offset]
    cursor = await db.execute(query, params)
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_setting(key: str) -> str:
    db = await get_db()
    cursor = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = await cursor.fetchone()
    return row['value'] if row else None


async def set_setting(key: str, value: str):
    db = await get_db()
    await db.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (key, value)
    )
    await db.commit()


async def cleanup_old_logs(retention_days: int):
    db = await get_db()

    # Get max_jobs setting
    max_jobs_str = await get_setting('max_jobs')
    max_jobs = int(max_jobs_str) if max_jobs_str else 500

    # Delete logs for jobs older than retention_days
    cutoff = datetime.utcnow()
    from datetime import timedelta
    cutoff = cutoff - timedelta(days=retention_days)
    cutoff_str = cutoff.isoformat()

    await db.execute(
        """DELETE FROM job_logs WHERE job_id IN (
               SELECT id FROM jobs WHERE created_at < ?
           )""",
        (cutoff_str,)
    )

    # Delete old jobs beyond max_jobs (keep the most recent max_jobs)
    await db.execute(
        """DELETE FROM job_logs WHERE job_id IN (
               SELECT id FROM jobs ORDER BY created_at DESC LIMIT -1 OFFSET ?
           )""",
        (max_jobs,)
    )
    await db.execute(
        "DELETE FROM jobs WHERE id NOT IN (SELECT id FROM jobs ORDER BY created_at DESC LIMIT ?)",
        (max_jobs,)
    )

    await db.commit()
