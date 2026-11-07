"""`indec-stats`: print the hosted server's usage report.

Usage: indec-stats [--days 30] [--url https://.../stats] [--json]
Reads STATS_TOKEN from the environment or ./.env.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

from .sync import _load_dotenv

DEFAULT_URL = "https://indec-mcp-production.up.railway.app/stats"


def _table(rows: list[dict], cols: list[str]) -> str:
    if not rows:
        return "  (sin datos)"
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    head = "  " + "  ".join(c.ljust(widths[c]) for c in cols)
    sep = "  " + "  ".join("-" * widths[c] for c in cols)
    body = ["  " + "  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols) for r in rows]
    return "\n".join([head, sep, *body])


def render(r: dict) -> str:
    u = r["users"]
    ret = r["retention"]
    out = [
        f"INDEC MCP: uso de los últimos {r['window_days']} días (al {r['generated_for']}, UTC)",
        "",
        f"Usuarios: hoy {u['today']} · 7 días {u['last_7_days']} · 30 días {u['last_30_days']} "
        f"· total guardado {u['all_time_retained']}",
        f"Vuelven: {ret['returning_users'] or 0} usaron el servidor 2 o más días · {ret['one_day_users'] or 0} solo uno",
        f"Pedidos rechazados por límite (429): {r['rate_limited_requests']}",
        f"({u['note']})",
        "",
        "Por día:",
        _table(r["daily"], ["day", "users", "new_users", "tool_calls", "rate_limited", "requests"]),
        "",
        "Clientes (apps):",
        _table(r["clients"], ["client", "via", "users", "requests"]),
        "",
        "Vía Claude.ai / ChatGPT (sus servidores comparten IPs entre usuarios: se mide volumen):",
        _table(r.get("via_providers", []), ["via", "requests", "tool_calls", "active_days", "rate_limited"]),
        "",
        "Herramientas:",
        _table(r["tools"], ["tool", "calls", "users", "error_pct", "avg_ms", "max_ms"]),
        "",
        "Temas más consultados:",
        _table(r["top_topics"], ["tool", "detail", "calls"]),
    ]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Usage report for the hosted INDEC MCP server.")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--url", default=os.environ.get("STATS_URL", DEFAULT_URL))
    p.add_argument("--json", action="store_true", help="print the raw JSON")
    args = p.parse_args(argv)
    _load_dotenv()
    token = os.environ.get("STATS_TOKEN")
    if not token:
        print("STATS_TOKEN is not set (env or .env).", file=sys.stderr)
        return 2
    resp = httpx.get(args.url, params={"days": args.days}, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if resp.status_code != 200:
        print(f"HTTP {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
        return 1
    data = resp.json()
    print(json.dumps(data, ensure_ascii=False, indent=1) if args.json else render(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
