# indec-mcp (working name)

An AI and tooling layer over the official **[Catálogo API-INDEC](https://api.indec.gob.ar)**, the API that Argentina's national statistics institute launched on 2026-11-02 (in beta). The goal is to let anyone "chat with INDEC data" (inflation, basic baskets, wages, economic activity, poverty, employment, trade, population) from an AI assistant, with correct figures and proper citations.

> Unofficial. Not affiliated with INDEC. Data: **Fuente: INDEC**.

## Status

Research phase. Nothing runs yet beyond helper scripts.

- [docs/api-research.md](docs/api-research.md): how the API works (auth, PostgREST queries, limits, data quirks, terms) and what is still unknown
- [docs/datasets.md](docs/datasets.md): all 19 datasets with their columns and primary keys (generated from the spec)
- [docs/ideas.md](docs/ideas.md): project ideas, the recommended architecture for a hosted MCP that resists abuse, and a roadmap
- [spec/indec-openapi.json](spec/indec-openapi.json): a snapshot of the public OpenAPI 3.1 spec

## Scripts

```bash
scripts/fetch-spec.sh                 # refresh the spec snapshot and regenerate docs/datasets.md (no token needed)
INDEC_TOKEN=... scripts/probe.sh      # row counts, latency and rate-limit headers for each dataset
```

To get a token, sign up at <https://api.indec.gob.ar>, verify with the OTP, and generate it from your profile. It is shown once. The FAQ says it lasts 12 months, but the one we got expires after 6. Keep it in `.env`, which git ignores; `.env.example` shows the format.
