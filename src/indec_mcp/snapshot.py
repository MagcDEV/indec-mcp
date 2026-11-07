"""Daily snapshot of the mirror, so a local install needs no INDEC token and no 15-minute sync.

Server side (hosted): `SnapshotPublisher` keeps a gzip of the current mirror file (31 MB -> ~12 MB),
rebuilt once per file version, served at /snapshot/indec.duckdb.gz with an ETag.

Client side (local stdio server): `SnapshotUpdater` downloads it on first start and re-checks it
every few hours with If-None-Match, so an unchanged mirror costs a 304. A download is validated by
opening it with DuckDB before it replaces the local file.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path

import duckdb
import httpx

log = logging.getLogger("indec_mcp.snapshot")

DEFAULT_URL = "https://indec-mcp-production.up.railway.app/snapshot/indec.duckdb.gz"
CHECK_EVERY_S = 6 * 3600


# -- server side -------------------------------------------------------------------------------


class SnapshotPublisher:
    def __init__(self, db_path: Path, out_dir: Path | None = None):
        self.db_path = Path(db_path)
        self.out_dir = Path(out_dir or self.db_path.parent / "snapshot")
        self._lock = threading.Lock()

    @property
    def gz_path(self) -> Path:
        return self.out_dir / "indec.duckdb.gz"

    def _etag_path(self) -> Path:
        return self.out_dir / "indec.duckdb.gz.etag"

    def current(self) -> tuple[Path, str]:
        """Return (gzip path, etag) for the current mirror file, rebuilding it if the file changed.

        Raises FileNotFoundError while there is no mirror yet.
        """
        with self._lock:
            # Open first and fstat the open file: `indec-sync` may replace the path at any moment,
            # and the etag must describe exactly the bytes we compress.
            with open(self.db_path, "rb") as src:
                st = os.fstat(src.fileno())
                etag = hashlib.sha256(f"{st.st_ino}:{st.st_mtime_ns}:{st.st_size}".encode()).hexdigest()[:20]
                try:
                    if self._etag_path().read_text() == etag and self.gz_path.exists():
                        return self.gz_path, etag
                except FileNotFoundError:
                    pass
                self.out_dir.mkdir(parents=True, exist_ok=True)
                tmp = self.gz_path.with_suffix(".gz.tmp")
                t0 = time.monotonic()
                with gzip.open(tmp, "wb", compresslevel=6) as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
                os.replace(tmp, self.gz_path)
                self._etag_path().write_text(etag)
                log.info("snapshot rebuilt: %s bytes in %.1fs", self.gz_path.stat().st_size, time.monotonic() - t0)
                return self.gz_path, etag


# -- client side -------------------------------------------------------------------------------


def validate(path: Path) -> int:
    """Open a downloaded mirror read-only; return its dataset count or raise."""
    con = duckdb.connect(str(path), read_only=True)
    try:
        n = con.execute("SELECT count(*) FROM _datasets").fetchone()[0]
    finally:
        con.close()
    if not n:
        raise ValueError("snapshot has no datasets")
    return n


class SnapshotUpdater:
    def __init__(self, db_path: Path, url: str = DEFAULT_URL, check_every_s: float = CHECK_EVERY_S,
                 client: httpx.Client | None = None):
        self.db_path = Path(db_path)
        self.url = url
        self.check_every_s = check_every_s
        self._client = client
        self.first_check = threading.Event()
        self._stop = threading.Event()

    @property
    def meta_path(self) -> Path:
        return self.db_path.with_name(self.db_path.name + ".snapshot.json")

    @property
    def pending_path(self) -> Path:
        return self.db_path.with_name(self.db_path.name + ".pending")

    def _meta(self) -> dict:
        try:
            return json.loads(self.meta_path.read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def _save_meta(self, **kw) -> None:
        meta = self._meta() | kw
        self.meta_path.write_text(json.dumps(meta))

    def apply_pending(self) -> bool:
        """Install a download that couldn't replace the file last time (Windows keeps it locked)."""
        if not self.pending_path.exists():
            return False
        try:
            os.replace(self.pending_path, self.db_path)
            log.info("installed pending snapshot")
            return True
        except OSError as e:
            log.warning("pending snapshot still can't be installed: %s", e)
            return False

    def due(self, now: float | None = None) -> bool:
        if not self.db_path.exists():
            return True
        checked = self._meta().get("checked_at", 0)
        return (now or time.time()) - checked >= self.check_every_s

    def check(self) -> str:
        """One conditional download. Returns 'updated', 'unchanged' or 'pending'."""
        meta = self._meta()
        headers = {"user-agent": "indec-mcp-local"}
        if meta.get("etag") and self.db_path.exists():
            headers["if-none-match"] = meta["etag"]
        client = self._client or httpx.Client(timeout=httpx.Timeout(30, read=120), follow_redirects=True)
        try:
            with client.stream("GET", self.url, headers=headers) as r:
                if r.status_code == 304:
                    self._save_meta(checked_at=time.time())
                    return "unchanged"
                r.raise_for_status()
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
                gz_tmp = self.db_path.with_name(self.db_path.name + ".download.gz")
                with open(gz_tmp, "wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
                etag = r.headers.get("etag")
        finally:
            if self._client is None:
                client.close()

        tmp = self.db_path.with_name(self.db_path.name + ".download")
        try:
            with gzip.open(gz_tmp, "rb") as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            n = validate(tmp)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        finally:
            gz_tmp.unlink(missing_ok=True)
        try:
            os.replace(tmp, self.db_path)
            result = "updated"
        except OSError:  # Windows: the running server has the file open
            os.replace(tmp, self.pending_path)
            result = "pending"
        self._save_meta(etag=etag, checked_at=time.time(), url=self.url)
        log.info("snapshot %s (%s datasets) -> %s", result, n, self.db_path)
        return result

    def run_once(self) -> None:
        try:
            if self.due():
                self.check()
        except Exception as e:  # network down, 429, corrupt download: keep serving what we have
            log.warning("snapshot download failed (%s): %s", self.url, e)
        finally:
            self.first_check.set()

    def start(self) -> threading.Thread:
        self.apply_pending()

        def loop():
            while not self._stop.is_set():
                self.run_once()
                self._stop.wait(min(self.check_every_s, 3600))

        t = threading.Thread(target=loop, name="indec-snapshot", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()


def updater_from_env(db_path: Path) -> SnapshotUpdater | None:
    """Auto-download unless the user manages the file: INDEC_DB set, or INDEC_SNAPSHOT_URL=''."""
    url = os.environ.get("INDEC_SNAPSHOT_URL")
    if url is None:
        if os.environ.get("INDEC_DB"):
            return None
        url = DEFAULT_URL
    if not url:
        return None
    return SnapshotUpdater(db_path, url)
