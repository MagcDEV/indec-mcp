# indec-mcp (working name)

An AI and tooling layer over the official **[Catálogo API-INDEC](https://api.indec.gob.ar)**, the API that Argentina's national statistics institute launched on 2026-11-02 (in beta). The goal is to let anyone "chat with INDEC data" (inflation, basic baskets, wages, economic activity, poverty, employment, trade, population) from an AI assistant, with correct figures and proper citations.

> Unofficial. Not affiliated with INDEC. Data: **Fuente: INDEC**.

## Status

- ✅ Research ([docs/api-research.md](docs/api-research.md))
- ✅ **Sync job**: mirrors all 19 datasets (about 2 M rows) into one DuckDB file
- ⏳ Local MCP server (stdio) over the mirror
- ⏳ Hosted MCP on Railway with rate limits

## Sync

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'   # or: uv sync
echo 'INDEC_TOKEN=...' > .env                                # git ignores this file

.venv/bin/indec-sync                       # full refresh -> data/indec.duckdb (about 15 min, about 2,000 requests)
.venv/bin/indec-sync --only ipc_nacional   # refresh just one dataset; the rest are carried over
.venv/bin/python -m pytest                 # tests use a fake PostgREST server, no network
```

How it works:
- **Full refresh every run.** INDEC revises provisional figures, so appending only new periods would keep outdated values.
- **Stable pagination.** Each table is ordered by its full primary key, so pages never skip or repeat rows.
- **No half-written files.** Each run builds `indec.duckdb.tmp` and then replaces the old file in one step.
- **Partial failures are survivable.** If one dataset fails, its previous table is kept and marked `stale`. An expired token aborts the run and leaves the mirror untouched.
- **Normalized names.** All column names are lowercase.
- **Built-in validation.** Row counts are checked against the API's reported total, and duplicate primary keys are flagged.
- **Metadata for the MCP layer.** The `_datasets` table holds descriptions, primary keys, row counts, latest period and status. The `_columns` table holds each column's type and its description from the API spec.

```sql
-- duckdb data/indec.duckdb
SELECT table_name, row_count, latest_periodo, status FROM _datasets ORDER BY row_count DESC;
```

- [docs/api-research.md](docs/api-research.md): how the API works (auth, PostgREST queries, limits, data quirks, terms) and what is still unknown
- [docs/datasets.md](docs/datasets.md): all 19 datasets with their columns and primary keys (generated from the spec)
- [docs/ideas.md](docs/ideas.md): project ideas, the recommended architecture for a hosted MCP that resists abuse, and a roadmap
- [spec/indec-openapi.json](spec/indec-openapi.json): a snapshot of the public OpenAPI 3.1 spec

## Other scripts

```bash
scripts/fetch-spec.sh                 # refresh the spec snapshot and regenerate docs/datasets.md (no token needed)
INDEC_TOKEN=... scripts/probe.sh      # row counts, latency and rate-limit headers for each dataset
```

To get a token, sign up at <https://api.indec.gob.ar>, verify with the OTP, and generate it from your profile. It is shown once. The FAQ says it lasts 12 months, but the one we got expires after 6. Keep it in `.env`, which git ignores; `.env.example` shows the format.
