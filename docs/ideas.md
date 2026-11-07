# Ideas for the INDEC API

## Recommendation

Build it in this order: **(1) a mirror → (2) an MCP over the mirror → (3) host it.** The other ideas below then become cheap side projects that reuse the same mirror.

The key insight is that **this data is small and changes about once a month.** A hosted MCP should never pass user traffic through to INDEC. Instead, a scheduled job syncs everything into a local DuckDB/Parquet snapshot, and the MCP serves from that snapshot. This approach:

- puts INDEC under the same tiny load no matter how many people use the MCP (a few hundred requests per sync), which solves the "don't abuse INDEC" problem;
- keeps our INDEC token in CI secrets only, and end users don't need one;
- makes abuse protection *our* problem, on our own infrastructure, where we control it;
- makes responses fast, and keeps us running when the beta API changes or goes down.

---

## 1. Hosted "chat with INDEC" MCP ⭐ (main idea)

This is a remote MCP server (Streamable HTTP) that anyone can add to Claude, ChatGPT, Cursor and other clients without signing up with INDEC.

**Why it's different from the existing MCPs:** they are local and each user brings their own token. Ours is a URL you paste in and it works.

### Tool design

LLMs do badly with raw PostgREST here: the composite keys are long, indicator types are mixed together, and the trade tables contain only codes. So we expose **intent-level tools**, and keep a raw escape hatch:

| Tool | What it does |
|---|---|
| `list_datasets()` | Name, description, coverage dates and update frequency for each dataset |
| `describe_dataset(id)` | Columns, valid values for each dimension (regions, IPC divisions, …), and examples |
| `get_series(dataset, filters, from, to, indicator_type)` | A normalized time series that is always a single indicator type, with a row cap |
| `latest(indicator)` | For example `latest("ipc_mensual_nacional")`: value, period, flags and the publication's own % change |
| `compare(series_a, series_b, from, to)` | For example wages vs. CPI → real wage; CBT vs. wage |
| `trade(flow, country?, sector?, from, to)` | Exports or imports with the code dictionary already joined |
| `query_sql(sql)` | Read-only DuckDB over the mirror for power users, with a timeout and a row cap |

Every response includes **provenance**: dataset, period, `observaciones` flags (provisional/estimated), `"Fuente: INDEC — <dataset>"`, and the sync timestamp. We also add MCP **prompts** such as "Explain this month's inflation" and **resources** for the methodology PDFs.

### Abuse protection and availability

An MCP server **doesn't pay for LLM tokens**, because the user's own client (Claude, ChatGPT) runs the model. Our cost is CPU and bandwidth only, and a DuckDB file of a few MB keeps that tiny. The protections we need:

- **Rate limits on our side:** token bucket per IP, plus a higher tier per API key. We could start at something like 60 requests/min for anonymous users and more for free keys issued through GitHub login.
- **Bounded work per call:** a maximum number of rows returned (say 500), a query timeout (2 s), and in `query_sql`, read-only access with no file or HTTP access (DuckDB `enable_external_access=false`).
- **An edge layer:** Cloudflare in front for DDoS protection and caching. Most answers are cacheable until the next sync.
- **Observability:** log tool name and latency, without user content, to spot abuse patterns.
- **Disclaimer:** state that it's unofficial and that INDEC is the source.

### Hosting options

- **Simple:** one small container (Python + FastMCP + DuckDB) on Railway or Fly. A GitHub Action rebuilds it whenever the data changes.
- **Edge:** Cloudflare Workers + R2 (Parquet) + its built-in rate limiting. Very cheap and global, but harder to run DuckDB on.

## 2. Open mirror / snapshot ⭐ (foundation)

A nightly GitHub Action pages through all 19 endpoints, normalizes them (consistent column casing, decoded trade codes, all data in long format), and publishes them as **Parquet + DuckDB** on GitHub Releases or Hugging Face Datasets.

- Analysts can then `duckdb 'https://…/indec.duckdb'` without signing up.
- The mirror is useful on its own, and it is also the data layer for every other idea here.
- It gives us a history of changes over time. The beta API may revise data silently, and the mirror makes that visible (see #7).

## 3. Release bot

The bot detects new periods after each sync and posts something like "📈 October CPI: +2.1% m/m, +31.4% y/y" with a chart to Telegram, X or Bluesky, and to webhooks. INDEC publishes a release calendar, so the bot can check right after each scheduled release.

## 4. "Mi inflación": a personal inflation calculator

The user sets their own basket weights (rent, food, transport…) and region. The tool weights IPC divisions or subclasses by region to show their personal inflation compared with the official figure. It's a viral web app.

## 5. Salary vs. prices

Using `indice_salarios` against `ipc_nacional` and `valorizacion_canastas_cba_cbt`, it answers "Did my salary beat inflation since X?" and "How many basic baskets (CBT) does my salary buy?"

## 6. Contract adjustment calculator (B2B)

Construction companies and public-works contractors adjust prices using the ICC and IPIB openings under **Decreto 1295/2002**, which `redeterminacion_precios_contratos_obras_publicas` provides directly. A calculator that also produces a PDF report could be **monetizable**. Rent and service contracts indexed to CPI are a similar use case.

## 7. Data-quality monitor

The tool diffs the API against INDEC's published reports and against the previous snapshot, and flags revisions and mismatches. People have already reported discrepancies, and a public changelog of "what INDEC revised" is useful to journalists.

## 8. SDKs

`pip install indec` and `npm i indec` would handle pagination with the correct composite key ordering, typed models, normalized names, and decoding of the null-reason signs. Small to build, useful to the ecosystem, and it helps people find the MCP.

## 9. Visual explorers

- Trade map: exports by destination country over time.
- Population map: estimates by department for 2022–2035, as a choropleth.

## 10. Web chat UI (later, and only if needed)

A public page that does the chat itself. **This one costs LLM money**, so it needs strict limits (per-IP daily caps, a captcha, or "bring your own API key"). Only build it after the MCP proves there is demand.

---

## Suggested roadmap

| Phase | Deliverable | Notes |
|---|---|---|
| 0 | ~~Get a token, run `scripts/probe.sh`~~ ✅ done 2026-11-07. Still to do: email `ces@indec.gob.ar` | The data is about 2.0 M rows (about 2,010 pages of 1000). Ask about rate limits and whether a public mirror is OK |
| 1 | Sync job → normalized DuckDB/Parquet (#2) | GitHub Action cron. Token stored as a secret |
| 2 | Local stdio MCP over the mirror (#1) | Iterate on tool design with Claude Code or Claude Desktop |
| 3 | Hosted remote MCP with rate limits | Railway/Fly + Cloudflare |
| 4 | Release bot, SDKs, calculators (#3–#8) | Each reuses the mirror |

## Open decisions

- **Language:** Python (FastMCP + DuckDB is the most natural fit) or TypeScript (the official MCP SDK, works well on Workers).
- **Docs and tool language:** Spanish, English or both. Most users will ask in Spanish, but the tool descriptions an LLM reads work in either.
- **Name:** `indec-mcp` is already used by two other repos on GitHub. Something like `datos-indec` or `indec-abierto` would be easier to find.
