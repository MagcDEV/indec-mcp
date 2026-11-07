# Hosted INDEC MCP server. The mirror lives on a volume mounted at /data; the first boot
# runs a full sync (~12 min) and the service answers "sincronización en curso" until then.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    INDEC_DB=/data/indec.duckdb \
    PORT=8000

WORKDIR /app

# Dependencies first for layer caching.
COPY pyproject.toml README.md ./
COPY src ./src
COPY spec ./spec
RUN pip install .

# Run as an unprivileged user. Railway mounts volumes as root, so the entrypoint fixes
# ownership of /data and then drops privileges.
RUN useradd --system --uid 10001 --home /app indec \
 && mkdir -p /data && chown indec /data \
 && apt-get update && apt-get install -y --no-install-recommends gosu && rm -rf /var/lib/apt/lists/*

EXPOSE 8000
CMD ["sh", "-c", "chown -R indec /data 2>/dev/null; exec gosu indec indec-mcp-http"]
