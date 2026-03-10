from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import asyncio
from db import init_db, cleanup_old_logs, get_setting
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
    await init_db()
    asyncio.create_task(scheduled_cleanup())
    yield


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
