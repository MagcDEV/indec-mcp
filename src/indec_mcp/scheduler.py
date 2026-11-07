"""Keep the mirror fresh inside the hosted service.

The sync runs as a subprocess (`python -m indec_mcp.sync`), not in-process: it opens DuckDB
for writing while the server holds read-only connections, its ~200 MB peak memory is released
when it exits, and a crash in it can't take the server down. The server picks up the new file
on its own (Mirror reopens when the file is atomically replaced).
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("indec_mcp.scheduler")


def seconds_until(hour_utc: int, now: datetime) -> float:
    target = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


class SyncScheduler(threading.Thread):
    def __init__(
        self,
        db_path: Path,
        *,
        hour_utc: int = 22,  # 19:00 Argentina, after INDEC's 16:00 releases
        max_age_hours: float = 26,
        work_dir: Path | None = None,
        timeout_s: float = 3600,
    ):
        super().__init__(name="indec-sync-scheduler", daemon=True)
        self.db_path = Path(db_path)
        self.hour_utc = hour_utc
        self.max_age_hours = max_age_hours
        self.work_dir = work_dir
        self.timeout_s = timeout_s
        self.stop_event = threading.Event()
        self.last_result: dict | None = None

    def needs_sync(self) -> bool:
        if not self.db_path.exists():
            return True
        age_h = (time.time() - self.db_path.stat().st_mtime) / 3600
        return age_h > self.max_age_hours

    def command(self) -> list[str]:
        cmd = [sys.executable, "-m", "indec_mcp.sync", "--db", str(self.db_path)]
        if self.work_dir:
            cmd += ["--work-dir", str(self.work_dir)]
        return cmd

    def sync_once(self) -> int:
        started = datetime.now(timezone.utc)
        log.info("sync starting")
        try:
            proc = subprocess.run(
                self.command(), capture_output=True, text=True, timeout=self.timeout_s
            )
            rc = proc.returncode
            tail = (proc.stdout + proc.stderr).strip().splitlines()[-25:]
        except subprocess.TimeoutExpired:
            rc, tail = -1, [f"timed out after {self.timeout_s}s"]
        level = logging.INFO if rc == 0 else logging.ERROR
        log.log(level, "sync finished rc=%s\n%s", rc, "\n".join(tail))
        self.last_result = {"started_at": started.isoformat(), "returncode": rc}
        return rc

    def run(self) -> None:
        if self.needs_sync():
            self.sync_once()
        while not self.stop_event.is_set():
            wait = seconds_until(self.hour_utc, datetime.now(timezone.utc))
            log.info("next sync in %.1fh", wait / 3600)
            if self.stop_event.wait(wait):
                return
            self.sync_once()
