#!/usr/bin/env python3
"""
Quality Scanner
Scans a directory of video files using ffprobe and outputs a quality report,
surfacing low-quality, webcam-recorded, and bad-audio files.

Usage:
  python quality_scanner.py --source <dir> [options]

Options:
  --source DIR         Directory to scan (required)
  --recursive          Scan subdirectories
  --min-quality N      Flag files below this height in pixels (default: 720)
  --report FILE        Write HTML report to this path
  --dry-run            Scan and report only (no files are modified either way)

Examples:
  python quality_scanner.py --source "M:\\Movies"
  python quality_scanner.py --source "M:\\Movies" --recursive --min-quality 1080
  python quality_scanner.py --source "M:\\Movies" --report "M:\\quality_report.html"
"""

import argparse
import html as html_mod
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# ffprobe discovery (same pattern as duplicate_finder.py)
# ---------------------------------------------------------------------------

def _find_ffprobe() -> Optional[str]:
    script_dir = Path(__file__).resolve().parent
    for candidate in sorted(script_dir.glob("ffmpeg*/bin/ffprobe.exe")):
        if candidate.is_file():
            return str(candidate)
    try:
        subprocess.run(["ffprobe", "-version"], capture_output=True, timeout=5)
        return "ffprobe"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


FFPROBE_BIN = _find_ffprobe()
FFPROBE_AVAILABLE = FFPROBE_BIN is not None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".m4v", ".wmv", ".flv", ".mpg",
    ".mpeg", ".ts", ".m2ts", ".webm", ".rm", ".rmvb", ".divx",
}

WEBCAM_KEYWORDS = re.compile(
    r'\b(?:webcam|obs|capture|zoom|meet|teams|logi|logitech|cam)\b',
    re.IGNORECASE,
)

# Codecs that are characteristic of webcam/capture output
WEBCAM_CODECS = {"mjpeg", "rawvideo"}

# Audio codecs that indicate raw/unprocessed audio
BAD_AUDIO_CODECS = {"none", "pcm_u8", "pcm_u4", "pcm_alaw", "pcm_mulaw"}

# Maximum bitrate (kbps) that counts as "suspiciously low" at each resolution tier.
# A real movie at 1080p is typically 4000–20000 kbps; below 1000 suggests webcam.
WEBCAM_BITRATE_THRESHOLDS = {
    2160: 2000,
    1080: 1000,
    720:  500,
    480:  300,
    0:    200,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_webcam_bitrate_threshold(height: int) -> int:
    for res in sorted(WEBCAM_BITRATE_THRESHOLDS.keys(), reverse=True):
        if height >= res:
            return WEBCAM_BITRATE_THRESHOLDS[res]
    return WEBCAM_BITRATE_THRESHOLDS[0]


def quality_tier(height: Optional[int]) -> str:
    if height is None:
        return "Unknown"
    if height >= 2160:
        return "4K"
    if height >= 1080:
        return "1080p"
    if height >= 720:
        return "720p"
    if height >= 540:
        return "540p"
    if height >= 480:
        return "480p"
    return "SD"


# ---------------------------------------------------------------------------
# ffprobe
# ---------------------------------------------------------------------------

def probe_file(path: Path) -> dict:
    """Run ffprobe on a file and return a normalised info dict."""
    result = {
        "width": None, "height": None, "codec": None,
        "bitrate_kbps": None, "duration_sec": None, "pix_fmt": None,
        "audio_codec": None, "audio_bitrate_kbps": None, "audio_channels": None,
        "has_audio": False,
    }
    if not FFPROBE_AVAILABLE:
        return result
    try:
        cmd = [
            FFPROBE_BIN, "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=30)
        data = json.loads(proc.stdout)
    except Exception:
        return result

    fmt = data.get("format", {})
    try:
        result["bitrate_kbps"] = int(fmt.get("bit_rate", 0)) // 1000
    except (TypeError, ValueError):
        pass
    try:
        result["duration_sec"] = float(fmt.get("duration", 0))
    except (TypeError, ValueError):
        pass

    for stream in data.get("streams", []):
        ctype = stream.get("codec_type", "")
        if ctype == "video" and result["width"] is None:
            result["width"] = stream.get("width")
            result["height"] = stream.get("height")
            result["codec"] = stream.get("codec_name")
            result["pix_fmt"] = stream.get("pix_fmt")
        elif ctype == "audio":
            result["has_audio"] = True
            result["audio_codec"] = stream.get("codec_name")
            result["audio_channels"] = stream.get("channels")
            try:
                abr = int(stream.get("bit_rate") or 0) // 1000
                if abr > 0:
                    result["audio_bitrate_kbps"] = abr
            except (TypeError, ValueError):
                pass

    return result


# ---------------------------------------------------------------------------
# Detection logic
# ---------------------------------------------------------------------------

def check_webcam(path: Path, info: dict) -> list[str]:
    """Return a list of reasons this file looks like webcam footage (empty = not webcam)."""
    reasons = []

    if WEBCAM_KEYWORDS.search(path.stem):
        reasons.append("filename keyword match")

    dur = info.get("duration_sec") or 0
    if 0 < dur < 10:
        reasons.append(f"very short clip ({dur:.1f}s)")

    codec = (info.get("codec") or "").lower()
    pix_fmt = (info.get("pix_fmt") or "").lower()
    if codec in WEBCAM_CODECS:
        reasons.append(f"webcam codec ({codec})")
    elif not codec and "yuv420p" in pix_fmt:
        reasons.append("raw pixel format with no codec name")

    w = info.get("width") or 0
    h = info.get("height") or 0
    if w > 0 and h > 0 and w <= h:
        reasons.append(f"portrait/square aspect ratio ({w}x{h})")

    bitrate = info.get("bitrate_kbps") or 0
    if h > 0 and bitrate > 0:
        threshold = get_webcam_bitrate_threshold(h)
        if bitrate < threshold:
            reasons.append(
                f"very low bitrate ({bitrate} kbps at {h}p, threshold {threshold} kbps)"
            )

    return reasons


def check_bad_audio(path: Path, info: dict, min_quality: int) -> list[str]:
    """Return a list of reasons the audio is bad/missing (empty = audio OK)."""
    reasons = []

    if not info.get("has_audio"):
        reasons.append("no audio stream")
        return reasons

    audio_codec = (info.get("audio_codec") or "none").lower()
    if audio_codec in BAD_AUDIO_CODECS:
        reasons.append(f"raw/unusual audio codec ({audio_codec})")

    abr = info.get("audio_bitrate_kbps")
    if abr is not None and 0 < abr < 64:
        reasons.append(f"very low audio bitrate ({abr} kbps)")

    channels = info.get("audio_channels") or 0
    dur = info.get("duration_sec") or 0
    if channels == 1 and dur > 1200:  # mono + > 20 min → likely a full movie
        reasons.append(f"mono audio on long file ({dur / 60:.0f} min)")

    return reasons


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------

def write_html_report(report_path: Path, results: list[dict], source: Path, min_quality: int):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total     = len(results)
    low_q     = sum(1 for r in results if r["low_quality"])
    webcam    = sum(1 for r in results if r["webcam"])
    bad_audio = sum(1 for r in results if r["bad_audio"])

    tier_colors = {
        "4K": "#00FF88", "1080p": "#00D4FF", "720p": "#4488FF",
        "540p": "#FFB800", "480p": "#FF8800", "SD": "#FF4444", "Unknown": "#8892A4",
    }

    def badge(label, color):
        return (
            f'<span style="padding:2px 7px;border-radius:4px;font-size:0.72rem;font-weight:700;'
            f'background:{color}22;color:{color};border:1px solid {color}44">{label}</span>'
        )

    rows = []
    # Sort: flagged files first, then alphabetically
    for r in sorted(results, key=lambda x: (not (x["low_quality"] or x["webcam"] or x["bad_audio"]), x.get("rel", x["path"]))):
        badges = ""
        if r["low_quality"]:
            badges += badge("LOW QUALITY", "#FFB800") + " "
        if r["webcam"]:
            badges += badge("WEBCAM", "#FF4444") + " "
        if r["bad_audio"]:
            badges += badge("BAD AUDIO", "#AA44FF") + " "

        reasons = r.get("reasons", [])
        reasons_html = ""
        if reasons:
            reasons_html = (
                '<div style="font-size:0.7rem;color:#8892A4;margin-top:3px">'
                + " &middot; ".join(html_mod.escape(s) for s in reasons)
                + "</div>"
            )

        nfo = r.get("info", {})
        tier_color = tier_colors.get(r["tier"], "#8892A4")
        codec_str = html_mod.escape(nfo.get("codec") or "—")
        bitrate_str = str(nfo.get("bitrate_kbps") or "—")

        rows.append(
            f"  <tr>\n"
            f"    <td style='font-family:monospace;font-size:0.77rem;word-break:break-all'>"
            f"{html_mod.escape(r.get('rel', r['path']))}{reasons_html}</td>\n"
            f"    <td style='text-align:center'><span style='color:{tier_color};font-weight:700'>{r['tier']}</span></td>\n"
            f"    <td style='text-align:center;font-size:0.8rem'>{bitrate_str}</td>\n"
            f"    <td style='text-align:center;font-size:0.8rem'>{codec_str}</td>\n"
            f"    <td>{badges}</td>\n"
            f"  </tr>"
        )

    no_issues = (
        "  <tr><td colspan='5' style='text-align:center;padding:32px;color:#8892A4'>"
        "No issues found — all files look good!</td></tr>"
    )

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Quality Scanner Report</title>
<style>
body{{font-family:system-ui,sans-serif;background:#0A0F1E;color:#F0F4FF;margin:0;padding:24px}}
h1{{font-size:1.4rem;margin:0 0 4px}}
.meta{{color:#8892A4;font-size:0.82rem;margin-bottom:24px}}
.stats{{display:flex;gap:20px;margin-bottom:28px;flex-wrap:wrap}}
.stat{{background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);border-radius:10px;padding:16px 20px;min-width:120px;text-align:center}}
.stat-val{{font-size:2rem;font-weight:700}}
.stat-lbl{{font-size:0.7rem;text-transform:uppercase;letter-spacing:0.1em;color:#8892A4;margin-top:4px}}
table{{width:100%;border-collapse:collapse;background:rgba(255,255,255,0.02);border-radius:10px;overflow:hidden}}
th{{padding:10px 14px;background:rgba(0,0,0,0.3);font-size:0.72rem;text-transform:uppercase;letter-spacing:0.08em;color:#8892A4;text-align:left}}
td{{padding:8px 14px;border-bottom:1px solid rgba(255,255,255,0.04);vertical-align:top}}
tr:hover td{{background:rgba(255,255,255,0.02)}}
</style>
</head>
<body>
<h1>Quality Scanner Report</h1>
<div class="meta">Source: {html_mod.escape(str(source))} &nbsp;&middot;&nbsp; Generated: {ts} &nbsp;&middot;&nbsp; Min quality: {min_quality}p</div>
<div class="stats">
  <div class="stat"><div class="stat-val" style="color:#00D4FF">{total}</div><div class="stat-lbl">Scanned</div></div>
  <div class="stat"><div class="stat-val" style="color:#FFB800">{low_q}</div><div class="stat-lbl">Low Quality</div></div>
  <div class="stat"><div class="stat-val" style="color:#FF4444">{webcam}</div><div class="stat-lbl">Webcam</div></div>
  <div class="stat"><div class="stat-val" style="color:#AA44FF">{bad_audio}</div><div class="stat-lbl">Bad Audio</div></div>
</div>
<table>
  <thead>
    <tr>
      <th>File</th>
      <th>Quality</th>
      <th>Bitrate (kbps)</th>
      <th>Codec</th>
      <th>Flags</th>
    </tr>
  </thead>
  <tbody>
{"".join(rows) or no_issues}
  </tbody>
</table>
</body>
</html>""",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# JSON results output
# ---------------------------------------------------------------------------

def write_results_json(json_path: Path, results: list, source: Path, scan_date: str) -> None:
    """Write machine-readable flagged-file list alongside the HTML report."""
    flagged = [
        {
            "path":        r["path"],  # absolute path
            "low_quality": r["low_quality"],
            "webcam":      r["webcam"],
            "bad_audio":   r["bad_audio"],
            "reasons":     r["reasons"],
            "resolution":  f"{r['info']['width']}x{r['info']['height']}" if r["info"].get("width") and r["info"].get("height") else "",
            "bitrate_kbps": r["info"].get("bitrate_kbps") or 0,
        }
        for r in results
        if r["low_quality"] or r["webcam"] or r["bad_audio"]
    ]
    payload = {
        "scan_date":  scan_date,
        "source_dir": str(source),
        "flagged":    flagged,
    }
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------

def scan(args):
    source = Path(args.source)
    if not source.exists():
        print(f"ERROR: Source directory does not exist: {source}", file=sys.stderr)
        sys.exit(1)

    # Validate the report path BEFORE starting the scan so we don't waste time
    # scanning thousands of files only to crash at the very end.
    if args.report:
        report_path = Path(args.report)
        if report_path.is_dir():
            report_path = report_path / f"quality_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
            print(f"INFO: Report path is a folder — saving report as: {report_path}")
            args.report = str(report_path)
        elif report_path.suffix.lower() not in (".html", ".htm"):
            print(
                f"WARN: Report path does not end in .html — the file may not open correctly in a browser: {report_path}"
            )

    dry_run = args.dry_run
    prefix  = "DRY RUN: " if dry_run else ""

    if not FFPROBE_AVAILABLE:
        print("WARN: ffprobe not found — codec/bitrate/duration checks will be skipped")

    print("=" * 60)
    print(f"{prefix}QUALITY SCANNER")
    print("=" * 60)
    print(f"Source:      {source}")
    print(f"Recursive:   {args.recursive}")
    print(f"Min quality: {args.min_quality}p")
    if dry_run:
        print("[DRY RUN] Reporting only — no files will be modified")
    print("=" * 60)

    # Collect video files
    if args.recursive:
        files = sorted(p for p in source.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTS)
    else:
        files = sorted(p for p in source.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS)

    total = len(files)
    print(f"Found {total} video file(s) to scan\n", flush=True)
    # Emit immediately so the UI shows the total file count as soon as scanning is done
    print(f"STATS: scanned={total} low_quality=0 webcam=0 bad_audio=0", flush=True)

    count_low_quality = 0
    count_webcam      = 0
    count_bad_audio   = 0
    results           = []

    for i, path in enumerate(files, 1):
        try:
            rel  = path.relative_to(source)
        except ValueError:
            rel  = path

        info    = probe_file(path)
        h       = info.get("height")
        tier    = quality_tier(h)
        flags   = []
        reasons = []

        # --- Low quality ---
        low_quality = h is not None and h < args.min_quality
        if low_quality:
            flags.append("LOW_QUALITY")
            reasons.append(f"resolution {tier} below {args.min_quality}p threshold")
            count_low_quality += 1

        # --- Webcam ---
        webcam_reasons = check_webcam(path, info)
        webcam = bool(webcam_reasons)
        if webcam:
            flags.append("WEBCAM")
            reasons.extend(webcam_reasons)
            count_webcam += 1

        # --- Bad audio ---
        audio_reasons = check_bad_audio(path, info, args.min_quality)
        bad_audio = bool(audio_reasons)
        if bad_audio:
            flags.append("BAD_AUDIO")
            reasons.extend(audio_reasons)
            count_bad_audio += 1

        # --- Level ---
        if not flags:
            level = "INFO"
        elif len(flags) >= 2 or (bad_audio and not info.get("has_audio")):
            level = "ERROR"
        else:
            level = "WARN"

        # --- Output line ---
        tier_tag   = f"[{tier}]"
        flags_str  = "".join(f"[{f}]" for f in flags)
        reason_str = f" — {'; '.join(reasons)}" if reasons else ""
        print(f"{prefix}{level}: {tier_tag}{flags_str} {rel}{reason_str}")

        results.append({
            "path":        str(path),
            "rel":         str(rel),
            "tier":        tier,
            "low_quality": low_quality,
            "webcam":      webcam,
            "bad_audio":   bad_audio,
            "info":        info,
            "reasons":     reasons,
        })

        # Emit running totals every 50 files so the UI subcategory counts fill in live
        if i % 50 == 0:
            print(f"STATS: scanned={total} low_quality={count_low_quality} webcam={count_webcam} bad_audio={count_bad_audio}", flush=True)

    # --- Summary ---
    print(f"\n{'=' * 60}")
    print(f"{prefix}SUMMARY")
    print(f"{'=' * 60}")
    print(f"Total scanned:    {total}")
    print(f"Low quality:      {count_low_quality}")
    print(f"Webcam detected:  {count_webcam}")
    print(f"Bad audio:        {count_bad_audio}")
    print(f"STATS: scanned={total} low_quality={count_low_quality} webcam={count_webcam} bad_audio={count_bad_audio}")

    if args.report:
        report_path = Path(args.report)
        try:
            write_html_report(report_path, results, source, args.min_quality)
            print(f"INFO: Report saved to: {report_path}")
            print(f"REPORT_PATH: {report_path}", flush=True)
            # Write machine-readable results for the replacement workflow
            json_path = report_path.with_suffix('.json')
            try:
                scan_date = datetime.now().isoformat(timespec='seconds')
                write_results_json(json_path, results, source, scan_date)
                print(f"INFO: Results JSON saved to: {json_path}")
            except OSError as e:
                print(f"WARN: Could not write results JSON: {e}", file=sys.stderr)
        except IsADirectoryError:
            print(
                f"ERROR: Could not save the report — '{report_path}' is a folder, not a file.\n"
                f"       The scan finished successfully but the report was not written.\n"
                f"       To fix this, add a filename at the end, like:\n"
                f"       {report_path / 'quality_report.html'}",
                file=sys.stderr,
            )
        except PermissionError:
            print(
                f"ERROR: Could not save the report — permission denied writing to '{report_path}'.\n"
                f"       Check that the folder exists and you have write access to it.",
                file=sys.stderr,
            )
        except OSError as e:
            print(
                f"ERROR: Could not save the report to '{report_path}'.\n"
                f"       Reason: {e}\n"
                f"       The scan finished successfully — only the report file is missing.",
                file=sys.stderr,
            )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Scan video files and report quality issues",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--source",      required=True,  help="Directory to scan")
    parser.add_argument("--recursive",   action="store_true", help="Scan subdirectories")
    parser.add_argument("--min-quality", type=int, default=720, metavar="HEIGHT",
                        help="Flag files below this height in pixels (default: 720)")
    parser.add_argument("--report",      metavar="FILE", help="Write HTML report to this path")
    parser.add_argument("--dry-run",     action="store_true",
                        help="Reporting mode — no files are modified either way")
    args = parser.parse_args()
    scan(args)


if __name__ == "__main__":
    import signal
    def _handle_sigterm(signum, frame):
        print("\n[INTERRUPTED] Shutdown signal received — stopping.", flush=True)
        os._exit(0)
    signal.signal(signal.SIGTERM, _handle_sigterm)
    main()
