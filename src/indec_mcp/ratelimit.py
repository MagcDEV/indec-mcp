"""In-memory token-bucket rate limiting as ASGI middleware, for a single-instance deployment.

Layers, all checked per request to the MCP endpoint:
- per client IP, short window (bursts) and long window (daily cap);
- global, so a flood from many IPs still can't exceed what one small instance can serve.

Client IP, in order of preference:
- `ip_header` (e.g. `x-real-ip`): a single header that the platform's edge sets to the real
  client IP. On Railway this is the documented header, and we verified in production that a
  client-supplied value is overwritten (docs/deploy.md).
- otherwise X-Forwarded-For, counting `proxy_hops` entries from the right (for proxies that
  append); the leftmost entries can be forged by the client.
- otherwise the socket peer.
If we ever scale past one instance, move buckets to Redis.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger("indec_mcp.ratelimit")


@dataclass(frozen=True)
class Rule:
    capacity: float  # burst size
    per_seconds: float  # time to refill `capacity` tokens

    @property
    def rate(self) -> float:
        return self.capacity / self.per_seconds


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    def __init__(
        self,
        per_ip: list[Rule],
        global_rule: Rule | None,
        *,
        max_tracked_ips: int = 50_000,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.per_ip = per_ip
        self.global_rule = global_rule
        self.max_tracked_ips = max_tracked_ips
        self._clock = clock
        self._ips: OrderedDict[str, list[_Bucket]] = OrderedDict()
        self._global = _Bucket(global_rule.capacity, clock()) if global_rule else None

    def _refill(self, b: _Bucket, rule: Rule, now: float) -> None:
        b.tokens = min(rule.capacity, b.tokens + (now - b.updated) * rule.rate)
        b.updated = now

    def check(self, ip: str) -> float:
        """0.0 if allowed (and consumes a token everywhere), else seconds until retry."""
        now = self._clock()
        buckets = self._ips.get(ip)
        if buckets is None:
            buckets = [_Bucket(r.capacity, now) for r in self.per_ip]
            self._ips[ip] = buckets
            if len(self._ips) > self.max_tracked_ips:
                self._ips.popitem(last=False)  # evict least recently seen
        else:
            self._ips.move_to_end(ip)

        pairs = list(zip(buckets, self.per_ip))
        if self._global is not None:
            pairs.append((self._global, self.global_rule))
        wait = 0.0
        for b, rule in pairs:
            self._refill(b, rule, now)
            if b.tokens < 1:
                wait = max(wait, (1 - b.tokens) / rule.rate)
        if wait:
            return wait
        for b, _ in pairs:
            b.tokens -= 1
        return 0.0


def client_ip(scope: dict, proxy_hops: int, ip_header: str | None = None) -> str:
    if ip_header:
        wanted = ip_header.lower().encode("latin-1")
        for name, value in scope.get("headers", []):
            if name == wanted:
                ip = value.decode("latin-1").strip()
                if ip:
                    return ip
                break
    if proxy_hops > 0:
        for name, value in scope.get("headers", []):
            if name == b"x-forwarded-for":
                parts = [p.strip() for p in value.decode("latin-1").split(",") if p.strip()]
                if len(parts) >= proxy_hops:
                    return parts[-proxy_hops]
                break
    client = scope.get("client")
    return client[0] if client else "unknown"


def _anon(ip: str) -> str:
    """Log a short hash, not the IP itself."""
    return hashlib.sha256(ip.encode()).hexdigest()[:10]


class RateLimitMiddleware:
    def __init__(
        self,
        app,
        limiter: RateLimiter,
        *,
        path_prefix: str = "/mcp",
        proxy_hops: int = 1,
        ip_header: str | None = None,
    ):
        self.app = app
        self.limiter = limiter
        self.path_prefix = path_prefix
        self.proxy_hops = proxy_hops
        self.ip_header = ip_header

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or not scope["path"].startswith(self.path_prefix)
            or scope["method"] == "OPTIONS"
        ):
            return await self.app(scope, receive, send)
        ip = client_ip(scope, self.proxy_hops, self.ip_header)
        wait = self.limiter.check(ip)
        if not wait:
            return await self.app(scope, receive, send)

        retry = max(1, math.ceil(wait))
        log.info("rate limited client=%s retry_after=%ss", _anon(ip), retry)
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": None,
            "error": {
                "code": -32000,
                "message": f"Rate limit exceeded. Retry in {retry}s. "
                           "Límite de uso alcanzado: reintentá más tarde.",
            },
        }).encode()
        await send({
            "type": "http.response.start",
            "status": 429,
            "headers": [
                (b"content-type", b"application/json"),
                (b"retry-after", str(retry).encode()),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})
