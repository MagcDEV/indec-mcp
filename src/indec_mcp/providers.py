"""Recognize traffic from hosted AI clients (Claude.ai, ChatGPT) by source IP.

Hosted clients call MCP servers from their providers' servers, not from the user's device: in
production, one Claude.ai user's connector setup arrived from 6 different Anthropic IPs. So for
that traffic an IP is neither a user nor a fair rate-limit key. We classify it by the providers'
*published* egress ranges (the source IP is set by our platform's edge and can't be forged,
unlike client names or User-Agents) and give each provider its own, larger budget.

Sources (checked 2026-11-07):
- Anthropic: https://platform.claude.com/docs/en/api/ip-addresses ("Outbound IP addresses";
  stable, "will not change without notice").
- OpenAI: https://openai.com/chatgpt-connectors.json (ChatGPT integrations: connectors, apps,
  GPT actions). OpenAI says it changes; we refresh it daily and fall back to a bundled snapshot.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import threading
from pathlib import Path

import httpx

log = logging.getLogger("indec_mcp.providers")

ANTHROPIC_OUTBOUND = ("160.79.104.0/21", "153.61.208.0/20")
OPENAI_URL = "https://openai.com/chatgpt-connectors.json"
_BUNDLED_OPENAI = Path(__file__).resolve().parent / "provider_ranges" / "openai-chatgpt-connectors.json"

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def _parse_openai(doc: dict) -> list[Network]:
    nets = []
    for p in doc.get("prefixes", []):
        cidr = p.get("ipv4Prefix") or p.get("ipv6Prefix")
        if cidr:
            nets.append(ipaddress.ip_network(cidr, strict=False))
    if not nets:
        raise ValueError("no prefixes in OpenAI ranges document")
    return nets


class ProviderRanges:
    def __init__(self, cache_dir: Path | None = None, *, openai_doc: dict | None = None):
        self.cache = (Path(cache_dir) / "openai-chatgpt-connectors.json") if cache_dir else None
        self._lock = threading.Lock()
        self._nets: dict[str, list[Network]] = {
            "anthropic": [ipaddress.ip_network(c) for c in ANTHROPIC_OUTBOUND],
            "openai": _parse_openai(openai_doc) if openai_doc else self._load_openai_offline(),
        }

    def _load_openai_offline(self) -> list[Network]:
        for path in (self.cache, _BUNDLED_OPENAI):
            if path and path.exists():
                try:
                    return _parse_openai(json.loads(path.read_text()))
                except (ValueError, OSError) as e:
                    log.warning("bad OpenAI ranges file %s: %s", path, e)
        return []

    def refresh_openai(self, client: httpx.Client | None = None) -> int:
        """Fetch OpenAI's current list; keep the old one on any failure. Returns prefix count."""
        try:
            c = client or httpx.Client(timeout=20, headers={"User-Agent": "indec-mcp/0.1"})
            resp = c.get(OPENAI_URL)
            resp.raise_for_status()
            doc = resp.json()
            nets = _parse_openai(doc)
        except Exception as e:  # noqa: BLE001
            log.warning("OpenAI ranges refresh failed, keeping %d known prefixes: %s",
                        len(self._nets["openai"]), e)
            return len(self._nets["openai"])
        with self._lock:
            self._nets["openai"] = nets
        if self.cache:
            try:
                self.cache.parent.mkdir(parents=True, exist_ok=True)
                self.cache.write_text(json.dumps(doc))
            except OSError as e:
                log.warning("could not cache OpenAI ranges: %s", e)
        log.info("OpenAI ranges refreshed: %d prefixes (creationTime %s)", len(nets), doc.get("creationTime"))
        return len(nets)

    def start_daily_refresh(self, interval_s: float = 86_400) -> threading.Thread:
        def loop():
            self.refresh_openai()
            while not stop.wait(interval_s):
                self.refresh_openai()

        stop = threading.Event()
        t = threading.Thread(target=loop, name="provider-ranges", daemon=True)
        t.stop = stop  # type: ignore[attr-defined]
        t.start()
        return t

    def classify(self, ip: str) -> str | None:
        """'anthropic', 'openai' or None (a direct client)."""
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return None
        with self._lock:
            for name, nets in self._nets.items():
                if any(addr in n for n in nets):
                    return name
        return None

    def counts(self) -> dict[str, int]:
        return {k: len(v) for k, v in self._nets.items()}
