#!/usr/bin/env bash
# Refresh the OpenAPI snapshot (no token needed) and regenerate docs/datasets.md.
set -euo pipefail
cd "$(dirname "$0")/.."
curl -fsS -m 30 "https://api.indec.gob.ar/v1/" -o spec/indec-openapi.json.tmp
python3 -m json.tool spec/indec-openapi.json.tmp > /dev/null
mv spec/indec-openapi.json.tmp spec/indec-openapi.json
python3 scripts/gen_datasets_md.py spec/indec-openapi.json > docs/datasets.md
git --no-pager diff --stat -- spec docs/datasets.md || true
