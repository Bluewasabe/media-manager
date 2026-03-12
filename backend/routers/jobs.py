from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
import asyncio
import json
from datetime import datetime
from job_runner import runner
from db import create_job, get_jobs, get_job, update_job_status, get_setting

router = APIRouter()


class JobCreate(BaseModel):
    script: str
    config: dict


def build_args(script: str, config: dict) -> tuple[list[str], bool]:
    """Returns (args_list, needs_confirmation)"""
    needs_conf = False

    if script == 'media_organizer':
        args = ['python', '-u', '/app/scripts/media_organizer.py']
        args.append(config['mode'])
        if config.get('path'):
            args.append(config['path'])
        if config.get('execute'):
            args.append('--execute')
            needs_conf = True

    elif script == 'disk_drill':
        args = ['python', '-u', '/app/scripts/disk_drill_organizer.py']
        args.append(config['input_dir'])
        args.append(config['output_dir'])
        if config.get('execute'):
            args.append('--execute')
        if config.get('move'):
            args.append('--move')
            needs_conf = True
        if config.get('include_filtered'):
            args.append('--include-filtered')
        if config.get('skip_video_meta'):
            args.append('--skip-video-meta')
        if config.get('min_photo_px', 800) != 800:
            args += ['--min-photo-px', str(config['min_photo_px'])]
        if config.get('min_video_sec', 30) != 30:
            args += ['--min-video-sec', str(config['min_video_sec'])]
        if config.get('workers', 8) != 8:
            args += ['--workers', str(config['workers'])]
        if config.get('report'):
            args += ['--report', config['report']]

    elif script == 'duplicate_finder':
        args = ['python', '-u', '/app/scripts/duplicate_finder.py']
        sources = config.get('sources', [])
        if sources:
            args += ['--sources'] + sources
        args += ['--output', config['output']]
        if config.get('execute'):
            args.append('--execute')
        action = config.get('action', 'archive')
        args += ['--action', action]
        if config.get('execute') and action in ('delete', 'archive'):
            needs_conf = True
        if action == 'archive' and config.get('archive'):
            args += ['--archive', config['archive']]
        types = config.get('types', [])
        if types and types != ['all']:
            args += ['--types'] + types
        if config.get('perceptual'):
            args.append('--perceptual')
        if config.get('skip_video_meta'):
            args.append('--skip-video-meta')
        if config.get('hash', 'sha256') != 'sha256':
            args += ['--hash', config['hash']]
        if config.get('workers', 8) != 8:
            args += ['--workers', str(config['workers'])]
        if config.get('min_size', 4096) != 4096:
            args += ['--min-size', str(config['min_size'])]
        if config.get('report'):
            args += ['--report', config['report']]

    else:
        raise ValueError(f"Unknown script: {script}")

    return args, needs_conf


@router.post("/jobs")
async def create_and_run_job(body: JobCreate):
    try:
        args, needs_conf = build_args(body.script, body.config)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e))

    job_id = await create_job(body.script, body.config, args)
    asyncio.create_task(runner.run(job_id, args, needs_conf))
    return {"job_id": job_id}


@router.get("/jobs")
async def list_jobs(limit: int = 50, offset: int = 0, status: str = None):
    return await get_jobs(limit=limit, offset=offset, status_filter=status)


@router.get("/jobs/{job_id}")
async def get_job_detail(job_id: str):
    job = await get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@router.delete("/jobs/{job_id}")
async def cancel_job(job_id: str):
    await runner.cancel(job_id)
    return {"ok": True}


@router.websocket("/ws/{job_id}")
async def ws_job(websocket: WebSocket, job_id: str):
    await websocket.accept()
    q = await runner.subscribe(job_id)

    # Send current job state immediately
    job = await get_job(job_id)
    if job:
        await websocket.send_json({'type': 'init', 'job': job})

    try:
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=30)
                await websocket.send_json(msg)
                if msg.get('type') == 'done':
                    break
            except asyncio.TimeoutError:
                # Heartbeat
                await websocket.send_json({'type': 'ping'})
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        runner.unsubscribe(job_id, q)
