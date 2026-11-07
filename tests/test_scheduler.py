import os
import sys
import time
from datetime import datetime, timezone

import pytest

from indec_mcp.scheduler import SyncScheduler, seconds_until


@pytest.mark.parametrize(
    "now, expected_h",
    [
        (datetime(2026, 11, 7, 21, 0, tzinfo=timezone.utc), 1),
        (datetime(2026, 11, 7, 22, 0, tzinfo=timezone.utc), 24),  # exactly at the hour -> tomorrow
        (datetime(2026, 11, 7, 23, 0, tzinfo=timezone.utc), 23),
    ],
)
def test_seconds_until_next_run(now, expected_h):
    assert seconds_until(22, now) == expected_h * 3600


def test_needs_sync_when_missing_or_old(tmp_path):
    db = tmp_path / "indec.duckdb"
    s = SyncScheduler(db, max_age_hours=26)
    assert s.needs_sync()
    db.write_bytes(b"x")
    assert not s.needs_sync()
    old = time.time() - 27 * 3600
    os.utime(db, (old, old))
    assert s.needs_sync()


def test_sync_runs_as_subprocess_and_records_result(tmp_path, monkeypatch):
    s = SyncScheduler(tmp_path / "indec.duckdb", work_dir=tmp_path)
    assert s.command()[:3] == [sys.executable, "-m", "indec_mcp.sync"]
    assert "--work-dir" in s.command()
    monkeypatch.setattr(s, "command", lambda: [sys.executable, "-c", "import sys; print('done'); sys.exit(1)"])
    assert s.sync_once() == 1
    assert s.last_result["returncode"] == 1
