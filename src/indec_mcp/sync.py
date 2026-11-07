"""Mirror every INDEC dataset into a single DuckDB file.

Design:
- Full refresh, not incremental: INDEC revises past (provisional) figures, so appending only
  new periods would silently keep stale values. A full pass is ~2,000 requests.
- Build into `<db>.tmp`, then atomically replace `<db>`: readers (the MCP server) never see a
  half-written file, and a crashed run leaves the previous mirror untouched.
- If one dataset fails, its previous table is carried over and marked `stale`, so one flaky
  endpoint doesn't take the rest down. An auth error aborts the whole run instead.
- Column names are lowercased (the API mixes `Anio`/`anio`); metadata tables `_datasets` and
  `_columns` describe everything for the MCP layer.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from .catalog import Dataset, column_name, load_snapshot, parse_spec
from .client import AuthError, IndecClient

log = logging.getLogger("indec_mcp.sync")

DEFAULT_DB = Path("data/indec.duckdb")

_META_DDL = """
CREATE TABLE _datasets (
    table_name     VARCHAR PRIMARY KEY,
    path           VARCHAR NOT NULL,
    summary        VARCHAR,
    description    VARCHAR,
    directorate    VARCHAR,
    primary_key    VARCHAR[],   -- normalized (lowercase) column names
    row_count      BIGINT,
    api_total      BIGINT,      -- Content-Range total reported by the API at fetch time
    latest_periodo VARCHAR,
    synced_at      TIMESTAMPTZ,
    status         VARCHAR,     -- ok | stale | empty
    warning        VARCHAR
);
CREATE TABLE _columns (
    table_name  VARCHAR,
    column_name VARCHAR,
    api_name    VARCHAR,
    data_type   VARCHAR,
    description VARCHAR,
    PRIMARY KEY (table_name, column_name)
);
"""


@dataclass
class Outcome:
    table: str
    status: str  # ok | stale | empty | failed | carried
    rows: int = 0
    seconds: float = 0.0
    warning: str | None = None


@dataclass
class SyncReport:
    outcomes: list[Outcome] = field(default_factory=list)
    requests: int = 0

    @property
    def ok(self) -> bool:
        return all(o.status in ("ok", "carried", "empty") for o in self.outcomes)


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def sync(
    db_path: Path,
    client: IndecClient,
    spec: dict,
    *,
    only: set[str] | None = None,
    now: datetime | None = None,
) -> SyncReport:
    datasets = parse_spec(spec)
    if only:
        known = {d.table for d in datasets} | {d.path.strip("/") for d in datasets}
        unknown = only - known
        if unknown:
            raise SystemExit(f"unknown dataset(s): {', '.join(sorted(unknown))}")
    now = now or datetime.now(timezone.utc)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = db_path.with_name(db_path.name + ".tmp")
    tmp_path.unlink(missing_ok=True)

    report = SyncReport()
    con = duckdb.connect(str(tmp_path))
    try:
        con.execute(_META_DDL)
        has_old = db_path.exists()
        if has_old:
            con.execute(f"ATTACH {_lit(str(db_path))} AS old (READ_ONLY)")
        old_tables = _old_tables(con) if has_old else set()

        with tempfile.TemporaryDirectory(prefix="indec-sync-") as work:
            for ds in datasets:
                selected = not only or ds.table in only or ds.path.strip("/") in only
                if not selected:
                    if ds.table in old_tables:
                        _carry_over(con, ds.table, status=None, warning=None)
                        report.outcomes.append(Outcome(ds.table, "carried"))
                    continue
                t0 = time.monotonic()
                try:
                    outcome = _sync_one(con, client, ds, Path(work), now)
                except AuthError:
                    raise
                except Exception as e:  # noqa: BLE001 - one bad endpoint must not kill the run
                    log.exception("%s failed", ds.table)
                    con.execute(f"DROP TABLE IF EXISTS {_q(ds.table)}")
                    con.execute("DELETE FROM _datasets WHERE table_name = ?", [ds.table])
                    con.execute("DELETE FROM _columns WHERE table_name = ?", [ds.table])
                    if ds.table in old_tables:
                        _carry_over(con, ds.table, status="stale", warning=f"last sync failed: {e}")
                        outcome = Outcome(ds.table, "stale", warning=str(e))
                    else:
                        outcome = Outcome(ds.table, "failed", warning=str(e))
                outcome.seconds = time.monotonic() - t0
                report.outcomes.append(outcome)
        if has_old:
            con.execute("DETACH old")
        con.execute("CHECKPOINT")
    except BaseException:
        con.close()
        tmp_path.unlink(missing_ok=True)
        raise
    con.close()
    os.replace(tmp_path, db_path)
    report.requests = client.requests_made
    return report


def _old_tables(con: duckdb.DuckDBPyConnection) -> set[str]:
    rows = con.execute(
        "SELECT table_name FROM duckdb_tables() WHERE database_name = 'old' AND schema_name = 'main'"
    ).fetchall()
    return {r[0] for r in rows} - {"_datasets", "_columns"}


def _carry_over(con, table: str, *, status: str | None, warning: str | None) -> None:
    con.execute(f"CREATE TABLE {_q(table)} AS SELECT * FROM old.main.{_q(table)}")
    meta_exists = con.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE database_name='old' AND table_name='_datasets'"
    ).fetchone()[0]
    if not meta_exists:
        return
    con.execute("INSERT INTO _datasets SELECT * FROM old.main._datasets WHERE table_name = ?", [table])
    con.execute("INSERT INTO _columns SELECT * FROM old.main._columns WHERE table_name = ?", [table])
    if status:
        con.execute(
            "UPDATE _datasets SET status = ?, warning = ? WHERE table_name = ?", [status, warning, table]
        )


def _sync_one(con, client: IndecClient, ds: Dataset, work: Path, now: datetime) -> Outcome:
    ndjson = work / f"{ds.table}.ndjson"
    n = 0
    api_total = None
    with ndjson.open("w", encoding="utf-8") as f:
        for rows, total in client.iter_pages(ds):
            if total is not None:
                api_total = total
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False))
                f.write("\n")
            n += len(rows)
            if api_total:
                log.debug("%s: %d/%d", ds.table, n, api_total)
    log.info("%s: fetched %d rows", ds.table, n)

    warnings = []
    pk = [column_name(c) for c in ds.primary_key]
    if n == 0:
        con.execute(
            "INSERT INTO _datasets VALUES (?,?,?,?,?,?,0,?,NULL,?,'empty',?)",
            [ds.table, ds.path, ds.summary, ds.description, ds.directorate, pk, api_total, now,
             "API returned no rows"],
        )
        return Outcome(ds.table, "empty", warning="API returned no rows")

    con.execute(
        f"CREATE OR REPLACE TEMP VIEW _stg AS SELECT * FROM read_json({_lit(str(ndjson))}, "
        "format='newline_delimited', sample_size=-1)"
    )
    cols = con.execute("DESCRIBE _stg").fetchall()
    select, api_names = [], {}
    for name, dtype, *_ in cols:
        norm = column_name(name)
        if norm in api_names:
            raise ValueError(f"column collision after lowercasing: {api_names[norm]!r} vs {name!r}")
        api_names[norm] = name
        # All-null columns (null-reason siblings, empty `observaciones`) infer as JSON/NULL.
        # DuckDB spells the all-null type with quotes: '"NULL"'.
        if dtype.strip('"') in ("JSON", "NULL") or name.endswith("_null") or norm == "observaciones":
            select.append(f"CAST({_q(name)} AS VARCHAR) AS {_q(norm)}")
        else:
            select.append(f"{_q(name)} AS {_q(norm)}")
    con.execute(f"CREATE TABLE {_q(ds.table)} AS SELECT {', '.join(select)} FROM _stg")
    con.execute("DROP VIEW _stg")

    row_count = con.execute(f"SELECT count(*) FROM {_q(ds.table)}").fetchone()[0]
    if api_total is not None and row_count != api_total:
        warnings.append(f"row count {row_count} != API total {api_total}")
    missing_pk = [c for c in pk if c not in api_names]
    if missing_pk:
        warnings.append(f"primary key columns missing from data: {missing_pk}")
    else:
        distinct = con.execute(
            f"SELECT count(*) FROM (SELECT DISTINCT {', '.join(_q(c) for c in pk)} FROM {_q(ds.table)})"
        ).fetchone()[0]
        if distinct != row_count:
            warnings.append(f"{row_count - distinct} duplicate primary keys")

    period_col = next((c for c in ("periodo", "fecha") if c in api_names), None)
    latest = None
    if period_col:
        latest = con.execute(
            f"SELECT max(CAST({_q(period_col)} AS VARCHAR)) FROM {_q(ds.table)}"
        ).fetchone()[0]

    warning = "; ".join(warnings) or None
    if warning:
        log.warning("%s: %s", ds.table, warning)
    con.execute(
        "INSERT INTO _datasets VALUES (?,?,?,?,?,?,?,?,?,?,'ok',?)",
        [ds.table, ds.path, ds.summary, ds.description, ds.directorate, pk, row_count, api_total,
         latest, now, warning],
    )
    for norm, dtype in con.execute(
        "SELECT column_name, data_type FROM duckdb_columns() WHERE table_name = ? "
        "AND database_name = current_database() ORDER BY column_index",
        [ds.table],
    ).fetchall():
        api = api_names[norm]
        doc = ds.column_docs.get(api)
        if doc is None and api.endswith("_null"):
            doc = f"Razón de nulo / signo convencional INDEC para `{column_name(api[:-5])}`"
        con.execute("INSERT INTO _columns VALUES (?,?,?,?,?)", [ds.table, norm, api, dtype, doc])
    return Outcome(ds.table, "ok", rows=row_count, warning=warning)


def _load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Mirror the INDEC API into DuckDB.")
    p.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"output file (default {DEFAULT_DB})")
    p.add_argument("--only", help="comma-separated table names or API paths to refresh; others are carried over")
    p.add_argument("--delay", type=float, default=0.2, help="seconds between requests (default 0.2)")
    p.add_argument("--spec", choices=["live", "snapshot"], default="live",
                   help="use the live OpenAPI spec (default) or spec/indec-openapi.json")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    _load_dotenv()
    token = os.environ.get("INDEC_TOKEN")
    if not token:
        print("INDEC_TOKEN is not set (env or .env). Get one at https://api.indec.gob.ar", file=sys.stderr)
        return 2

    only = {s.strip().strip("/") for s in args.only.split(",")} if args.only else None
    t0 = time.monotonic()
    with IndecClient(token, delay=args.delay) as client:
        spec = None
        if args.spec == "live":
            try:
                spec = client.spec()
                if spec.get("paths", {}).keys() != load_snapshot().get("paths", {}).keys():
                    log.warning("live spec has different datasets than spec/ snapshot; run scripts/fetch-spec.sh")
            except Exception as e:  # noqa: BLE001
                log.warning("could not fetch live spec (%s); using snapshot", e)
        spec = spec or load_snapshot()
        try:
            report = sync(args.db, client, spec, only=only)
        except AuthError as e:
            print(f"Auth failed, mirror left untouched. Token expired or revoked? {e}", file=sys.stderr)
            return 3

    print(f"\n{'table':<58} {'status':<8} {'rows':>9} {'secs':>6}  warning")
    for o in report.outcomes:
        print(f"{o.table:<58} {o.status:<8} {o.rows:>9} {o.seconds:>6.1f}  {o.warning or ''}")
    print(f"\n{report.requests} requests in {time.monotonic() - t0:.0f}s -> {args.db}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
