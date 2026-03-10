from fastapi import APIRouter, Query, HTTPException
from db import get_logs, cleanup_old_logs, get_setting, set_setting

router = APIRouter()


@router.get("/logs")
async def query_logs(
    job_id: str = None,
    level: str = None,
    search: str = None,
    limit: int = Query(default=200, le=1000),
    offset: int = 0
):
    return await get_logs(job_id=job_id, level=level, search=search, limit=limit, offset=offset)


@router.get("/settings")
async def get_all_settings():
    keys = ['log_retention_days', 'max_jobs', 'security_mode', 'extra_paths']
    result = {}
    for k in keys:
        result[k] = await get_setting(k)
    return result


@router.put("/settings/{key}")
async def update_setting(key: str, body: dict):
    allowed = ['log_retention_days', 'max_jobs', 'security_mode', 'extra_paths']
    if key not in allowed:
        raise HTTPException(400, f"Unknown setting: {key}")
    await set_setting(key, str(body.get('value', '')))
    return {"ok": True}


@router.post("/cleanup")
async def run_cleanup():
    days = int(await get_setting('log_retention_days') or 30)
    await cleanup_old_logs(days)
    return {"ok": True}
