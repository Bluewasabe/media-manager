from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import asyncio
from db import init_db, cleanup_old_logs, get_setting, interrupt_stale_jobs
from routers import filesystem, jobs, logs


async def scheduled_cleanup():
    while True:
        await asyncio.sleep(86400)  # daily
        try:
            days = int(await get_setting('log_retention_days') or 30)
            await cleanup_old_logs(days)
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Init DB tables first, then clean up any jobs orphaned by a previous crash
    await init_db()
    stale = await interrupt_stale_jobs()
    if stale:
        print(f"[startup] Marked {stale} stale job(s) as interrupted from previous run", flush=True)

    asyncio.create_task(scheduled_cleanup())
    yield

    # Graceful shutdown: signal all running subprocesses and wait for them to exit
    from job_runner import runner
    running_ids = list(runner._processes.keys())
    if running_ids:
        print(f"[shutdown] Stopping {len(running_ids)} running job(s)...", flush=True)
        for job_id in running_ids:
            await runner.cancel(job_id)
        # Wait up to 30 s for processes to finish cleanly
        for _ in range(30):
            await asyncio.sleep(1)
            if not runner._processes:
                break
        # Force-kill anything still alive
        for job_id in list(runner._processes.keys()):
            proc = runner._processes.get(job_id)
            if proc:
                try:
                    proc.kill()
                except Exception:
                    pass
        print("[shutdown] All jobs stopped.", flush=True)


app = FastAPI(title="Media Manager API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(filesystem.router, prefix="/api")
app.include_router(jobs.router, prefix="/api")
app.include_router(logs.router, prefix="/api")
