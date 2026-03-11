from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import FileResponse
import os
import json
from pathlib import Path
from datetime import datetime

router = APIRouter()

DRIVES_DIR = os.environ.get('DRIVES_DIR', '/mnt/drives')
DATA_DIR   = os.environ.get('DATA_DIR', '/data')


def load_drives_meta() -> dict:
    """Read drives-meta.json written by setup.ps1. Returns {} if missing."""
    meta_path = os.path.join(DATA_DIR, 'drives-meta.json')
    try:
        with open(meta_path) as f:
            return json.load(f)
    except Exception:
        return {}


def container_to_display(path: str) -> str:
    """Convert /mnt/drives/d/Movies to D:\\Movies"""
    if path.startswith(DRIVES_DIR + '/'):
        rest = path[len(DRIVES_DIR) + 1:]
        parts = rest.split('/', 1)
        drive = parts[0].upper() + ':\\'
        if len(parts) > 1:
            return drive + parts[1].replace('/', '\\')
        return drive
    return path


def format_size(n: int) -> str:
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


@router.get("/drives")
async def list_drives():
    from db import get_setting
    drives = []
    meta = load_drives_meta()

    if os.path.isdir(DRIVES_DIR):
        for entry in sorted(os.scandir(DRIVES_DIR), key=lambda e: e.name):
            if entry.is_dir():
                path = entry.path
                letter = entry.name.upper()
                letter_lower = entry.name.lower()
                try:
                    st = os.statvfs(path)
                    total = st.f_blocks * st.f_frsize
                    free = st.f_bavail * st.f_frsize
                    accessible = True
                except Exception:
                    total = free = 0
                    accessible = False

                drive_meta  = meta.get(letter_lower, {})
                drive_type  = drive_meta.get('type', 'Local')
                volume_name = drive_meta.get('volume_name', '')
                label = f"{letter}: {volume_name}".strip() if volume_name else f"Drive {letter}:"

                drives.append({
                    "letter":        letter,
                    "label":         label,
                    "path":          path,
                    "display":       f"{letter}:\\",
                    "drive_type":    drive_type,
                    "accessible":    accessible,
                    "free_bytes":    free,
                    "total_bytes":   total,
                    "free_display":  format_size(free) if accessible else "N/A",
                    "total_display": format_size(total) if accessible else "N/A",
                })

    # Add extra paths from settings (always shown as Network)
    extra_raw = await get_setting('extra_paths') or '[]'
    for ep in json.loads(extra_raw):
        accessible = os.path.isdir(ep)
        drives.append({
            "letter":        None,
            "label":         ep,
            "path":          ep,
            "display":       ep,
            "drive_type":    "Network",
            "accessible":    accessible,
            "free_bytes":    0,
            "total_bytes":   0,
            "free_display":  "N/A",
            "total_display": "N/A",
        })

    return drives


@router.get("/browse")
async def browse(path: str = Query(...)):
    from db import get_setting

    # Security check
    security_mode = await get_setting('security_mode') or 'false'
    if security_mode == 'true':
        import json as _json
        extra_raw = await get_setting('extra_paths') or '[]'
        extra = _json.loads(extra_raw)
        allowed = [DRIVES_DIR] + extra
        if not any(
            os.path.abspath(path).startswith(os.path.abspath(a))
            for a in allowed
        ):
            raise HTTPException(403, "Path not allowed in security mode")

    if not os.path.isdir(path):
        raise HTTPException(404, f"Directory not found: {path}")

    items = []
    try:
        with os.scandir(path) as it:
            for entry in sorted(it, key=lambda e: (not e.is_dir(), e.name.lower())):
                try:
                    is_dir = entry.is_dir(follow_symlinks=True)
                    info = entry.stat()
                    items.append({
                        "name":         entry.name,
                        "path":         entry.path,
                        "type":         "dir" if is_dir else "file",
                        "size":         info.st_size if not is_dir else None,
                        "size_display": format_size(info.st_size) if not is_dir else None,
                        "modified":     datetime.fromtimestamp(info.st_mtime).isoformat(),
                        "hidden":       entry.name.startswith('.'),
                    })
                except (PermissionError, OSError):
                    items.append({
                        "name":         entry.name,
                        "path":         entry.path,
                        "type":         "dir" if entry.is_dir() else "file",
                        "size":         None,
                        "size_display": None,
                        "modified":     None,
                        "hidden":       False,
                        "inaccessible": True,
                    })
    except PermissionError:
        raise HTTPException(403, "Permission denied")

    # Build parent path
    parent = str(Path(path).parent) if path != DRIVES_DIR and path != '/' else None

    return {
        "path":    path,
        "display": container_to_display(path),
        "parent":  parent,
        "items":   items,
    }


@router.get("/file")
async def serve_file(path: str = Query(...)):
    abs_path = os.path.abspath(path)
    allowed = [os.path.abspath(DRIVES_DIR), os.path.abspath(DATA_DIR)]
    if not any(abs_path.startswith(a) for a in allowed):
        raise HTTPException(403, "Access denied")
    if not os.path.isfile(abs_path):
        raise HTTPException(404, "File not found")
    return FileResponse(abs_path)
