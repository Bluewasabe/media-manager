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
            "No results.json for this job — scan was run without --report."
        )
    json_path = Path(report_path).with_suffix(".json")
    if not json_path.exists():
        raise HTTPException(
            404,
            f"results.json not found on disk (expected: {json_path}). It may have been deleted."
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
            except Exception as e:
                await log("ERROR", f"Failed to move {repl} → {dest}: {e}")
                errors += 1
                continue
            # Move succeeded — log and delete original
            await log("INFO", f"Moved {repl} → {dest}")
            try:
                orig.unlink()
                await log("INFO", f"Deleted {orig}")
                moved += 1
            except Exception as e:
                await log("ERROR", f"Move succeeded but could not delete original {orig}: {e} — two copies may exist")
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
