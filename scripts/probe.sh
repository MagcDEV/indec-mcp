#!/usr/bin/env bash
# Probe every dataset with a real token: row count (Content-Range), latency, rate-limit headers.
# Usage: INDEC_TOKEN=... scripts/probe.sh
# Fills the "still unknown" gaps in docs/api-research.md. Sends one small request per dataset.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${INDEC_TOKEN:?set INDEC_TOKEN (get one at https://api.indec.gob.ar)}"
BASE="https://api.indec.gob.ar/v1"
paths=$(python3 -c 'import json;print("\n".join(json.load(open("spec/indec-openapi.json"))["paths"]))')
printf "%-60s %-6s %-22s %s\n" dataset status content-range time_s
for p in $paths; do
  hdr=$(mktemp)
  t=$(curl -sS -m 30 -o /dev/null -D "$hdr" -w '%{http_code} %{time_total}' \
        -H "Authorization: Bearer $INDEC_TOKEN" -H "Prefer: count=exact" \
        "$BASE$p?limit=1")
  cr=$(grep -i '^content-range' "$hdr" | tr -d '\r' | awk '{print $2}')
  printf "%-60s %-6s %-22s %s\n" "$p" "${t% *}" "${cr:--}" "${t#* }"
  grep -iE '^(x-)?ratelimit|^retry-after' "$hdr" | tr -d '\r' | sed 's/^/    /' || true
  rm -f "$hdr"
  sleep 1
done
