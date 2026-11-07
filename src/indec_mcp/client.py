"""Polite HTTP client for the INDEC API (PostgREST): auth, pagination, retries."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator

import httpx

from .catalog import Dataset

log = logging.getLogger(__name__)

BASE_URL = "https://api.indec.gob.ar/v1"
PAGE_SIZE = 1000  # server-side cap; larger `limit` values are silently clamped
USER_AGENT = "indec-mcp-sync/0.1"
_RETRY_STATUS = {429, 500, 502, 503, 504}


class AuthError(RuntimeError):
    """Token missing, invalid, expired (they last ~6 months) or lacking permission."""


class IndecClient:
    def __init__(
        self,
        token: str | None,
        *,
        base_url: str = BASE_URL,
        delay: float = 0.2,
        max_retries: int = 5,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
    ):
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.Client(
            base_url=base_url, headers=headers, timeout=60, transport=transport
        )
        self.delay = delay
        self.max_retries = max_retries
        self._sleep = sleep
        self.requests_made = 0

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def get(self, path: str, params: dict | None = None, headers: dict | None = None) -> httpx.Response:
        for attempt in range(self.max_retries + 1):
            if self.requests_made:
                self._sleep(self.delay)
            self.requests_made += 1
            try:
                resp = self._http.get(path, params=params, headers=headers)
            except httpx.TransportError as e:
                if attempt == self.max_retries:
                    raise
                wait = 2**attempt
                log.warning("%s: %s; retrying in %ss", path, e, wait)
                self._sleep(wait)
                continue
            if resp.status_code in (401, 403):
                raise AuthError(f"{resp.status_code} on {path}: {resp.text[:200]}")
            if resp.status_code in _RETRY_STATUS and attempt < self.max_retries:
                wait = _retry_after(resp) or 2**attempt
                log.warning("%s: HTTP %s; retrying in %ss", path, resp.status_code, wait)
                self._sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        raise AssertionError("unreachable")

    def spec(self) -> dict:
        """Live OpenAPI spec; public, no token needed."""
        return self.get("/").json()

    def iter_pages(self, ds: Dataset) -> Iterator[tuple[list[dict], int | None]]:
        """Yield (rows, total) per page, ordered by the full primary key so pages are stable.

        `total` comes from Content-Range on the first page (Prefer: count=exact) and is None
        afterwards, so the expensive count runs once per dataset.
        """
        order = ",".join(ds.primary_key)
        offset = 0
        total = None
        while True:
            headers = {"Prefer": "count=exact"} if offset == 0 else None
            resp = self.get(
                ds.path,
                params={"order": order, "limit": PAGE_SIZE, "offset": offset},
                headers=headers,
            )
            rows = resp.json()
            if offset == 0:
                total = _content_range_total(resp.headers.get("content-range"))
            yield rows, (total if offset == 0 else None)
            offset += len(rows)
            if not rows or len(rows) < PAGE_SIZE or (total is not None and offset >= total):
                return


def _content_range_total(value: str | None) -> int | None:
    m = re.search(r"/(\d+)$", value or "")
    return int(m.group(1)) if m else None


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return float(resp.headers["retry-after"])
    except (KeyError, ValueError):
        return None
