import re
import asyncio
from datetime import datetime

ANSI_RE = re.compile(r'\x1b\[[0-9;]*[mGKH]')


def clean_line(raw: bytes) -> str:
    text = raw.decode('utf-8', errors='replace').rstrip('\r\n')
    return ANSI_RE.sub('', text).strip()


def detect_level(line: str) -> str:
    lower = line.lower()
    if any(w in lower for w in ['error', 'failed', 'exception', 'traceback', 'permission denied']):
        return 'ERROR'
    if any(w in lower for w in ['warning', 'warn', 'skip', 'skipping', 'already exists']):
        return 'WARN'
    if any(w in lower for w in ['dry run', 'would move', 'would copy', 'would delete', '[dry']):
        return 'DRY'
    return 'INFO'


def detect_phase(line: str, current: str) -> str:
    if current in ('complete', 'failed', 'cancelled'):
        return current
    lower = line.lower()
    if any(w in lower for w in ['scanning', 'indexing', 'found', 'discovered', 'hashing', 'loading']):
        return 'scanning'
    if any(w in lower for w in ['moving', 'copying', 'processing', 'organizing', 'archiving', 'keeping', 'breakdown']):
        return 'processing'
    if any(w in lower for w in ['complete', 'finished', 'done', 'summary', 'total:', 'dry run', 'report written']):
        return 'complete'
    return current or 'scanning'


def extract_stats(line: str, stats: dict):
    patterns = [
        (r'(\d[\d,]*)\s+files?\s+(?:found|scanned|discovered)', 'scanned'),
        (r'Found\s+(\d[\d,]*)\s+files', 'scanned'),          # DDO: "Found N files."
        (r'scanned\s+(\d[\d,]*)/', 'scanned'),               # DDO: "... scanned N/total"
        (r'(\d[\d,]*)\s+(?:moved|archived)', 'moved'),
        (r'(\d[\d,]*)\s+files?\s+(?:moved|copied)', 'moved'), # DDO: "N files moved/copied"
        (r'(\d[\d,]*)\s+(?:error|errors|failed)', 'errors'),
        (r'\b(\d[\d,]*)\s+errors?\.', 'errors'),             # DDO: "N errors." from [DONE]
        (r'(\d[\d,]*)\s+(?:skipped|skip)', 'skipped'),
        (r'processing\s+(\d[\d,]*)', 'processed'),
        (r'(\d[\d,]*)\s+duplicate', 'processed'),
    ]
    for pattern, key in patterns:
        m = re.search(pattern, line, re.IGNORECASE)
        if m:
            stats[key] = int(m.group(1).replace(',', ''))

    # Detect current file being processed
    file_patterns = [
        r'(?:moving|copying|processing):\s*(.+)',
        r'→\s*(.+)',
    ]
    for fp in file_patterns:
        m = re.search(fp, line, re.IGNORECASE)
        if m:
            stats['current_file'] = m.group(1).strip()[:120]
            break


class JobRunner:
    def __init__(self):
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._subscribers: dict[str, list[asyncio.Queue]] = {}

    async def subscribe(self, job_id: str) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=1000)
        self._subscribers.setdefault(job_id, []).append(q)
        return q

    def unsubscribe(self, job_id: str, q: asyncio.Queue):
        subs = self._subscribers.get(job_id, [])
        try:
            subs.remove(q)
        except ValueError:
            pass

    async def _broadcast(self, job_id: str, msg: dict):
        for q in list(self._subscribers.get(job_id, [])):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    async def run(self, job_id: str, args: list[str], needs_confirmation: bool = False):
        from db import update_job_status, update_job_stats, add_log

        await update_job_status(job_id, 'running')
        stats = {
            'scanned': 0,
            'processed': 0,
            'moved': 0,
            'errors': 0,
            'skipped': 0,
            'phase': 'scanning',
            'current_file': ''
        }

        proc = None
        status = 'failed'
        exit_code = -1

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.PIPE if needs_confirmation else None,
                cwd='/app/scripts'
            )
            self._processes[job_id] = proc

            if needs_confirmation and proc.stdin:
                proc.stdin.write(b'YES\n')
                await proc.stdin.drain()
                proc.stdin.close()

            async for raw in proc.stdout:
                line = clean_line(raw)
                if not line:
                    continue

                level = detect_level(line)
                stats['phase'] = detect_phase(line, stats.get('phase', 'scanning'))
                extract_stats(line, stats)
                ts = datetime.utcnow().isoformat()

                await add_log(job_id, ts, level, line)
                await update_job_stats(job_id, stats)
                await self._broadcast(job_id, {
                    'type': 'log', 'level': level, 'message': line, 'timestamp': ts
                })
                await self._broadcast(job_id, {'type': 'stats', **stats})

            await proc.wait()
            exit_code = proc.returncode
            stats['phase'] = 'complete' if exit_code == 0 else 'failed'
            status = 'completed' if exit_code == 0 else 'failed'

        except asyncio.CancelledError:
            proc = self._processes.get(job_id)
            if proc:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()
            status, exit_code = 'cancelled', -1
            stats['phase'] = 'cancelled'

        except Exception as e:
            status, exit_code = 'failed', -1
            from db import add_log as _add_log
            await _add_log(job_id, datetime.utcnow().isoformat(), 'ERROR', f'Runner error: {e}')
            stats['phase'] = 'failed'

        finally:
            self._processes.pop(job_id, None)

        await update_job_stats(job_id, stats)
        await update_job_status(job_id, status, exit_code)
        await self._broadcast(job_id, {'type': 'status', 'status': status, 'exit_code': exit_code})
        await self._broadcast(job_id, {'type': 'done'})

    async def cancel(self, job_id: str):
        proc = self._processes.get(job_id)
        if proc:
            proc.terminate()

    def is_running(self, job_id: str) -> bool:
        return job_id in self._processes


runner = JobRunner()
