"""Read-only, sandboxed access to the DuckDB mirror built by `indec-sync`.

Everything an MCP tool needs lives here, so it can be tested without MCP. Safety model for
untrusted SQL coming from an LLM (or from anyone, once hosted):
- the database is opened READ_ONLY;
- `enable_external_access=false` blocks reading/writing files, ATTACH, URLs, extension installs;
- `lock_configuration=true` stops a query from flipping those settings back;
- one SELECT statement per call, a wall-clock timeout (interrupt), and a row cap.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

SOURCE = "Fuente: INDEC (Catálogo API-INDEC). Mirror no oficial."

IPC_MEASURES = {
    "Indice": "Índice, base diciembre 2016 = 100",
    "v_m": "Variación % mensual",
    "v_ia": "Variación % interanual",
    "v_acum": "Variación % acumulada en el año",
    "Incidencia_m": "Incidencia en la variación mensual del nivel general (p.p.)",
    "Incidencia_ia": "Incidencia en la variación interanual del nivel general (p.p.)",
    "Incidencia_acum": "Incidencia en la variación acumulada del nivel general (p.p.)",
}
IPC_REGIONS = ("Nacional", "GBA", "Pampeana", "Noreste", "Noroeste", "Cuyo", "Patagonia")


_INDEX_KINDS = {"ipim": "IPIM", "ipib": "IPIB", "ipp": "IPP", "icc": "ICC", "ipi": "IPI_MANUFACTURERO",
                "ipi_manufacturero": "IPI_MANUFACTURERO", "ipi_minero": "IPI_MINERO", "emae_sector": "EMAE_SECTORES"}
_INDEX_LABELS = {"IPIM": "IPIM", "IPIB": "IPIB", "IPP": "IPP", "ICC": "Costo construcción",
                 "IPI_MANUFACTURERO": "IPI manufacturero", "IPI_MINERO": "IPI minero", "EMAE_SECTORES": "EMAE"}


class QueryError(ValueError):
    """A user-facing error: bad SQL, unknown dataset, ambiguous filter. Message is safe to show."""


@dataclass(frozen=True)
class Limits:
    max_rows: int = 500
    timeout_s: float = 5.0
    threads: int = 2  # DuckDB threads per query
    memory_limit: str = "1GB"  # shared by all queries
    max_concurrent: int = 4  # queries running at once; more wait up to `queue_timeout_s`
    queue_timeout_s: float = 3.0


def format_period(p: Any) -> str | None:
    """'202609' -> '2026-09', '20261S' -> '2026 S1', '20262T' -> '2026 T2'; else unchanged."""
    if p is None:
        return None
    s = str(p)
    if m := re.fullmatch(r"(\d{4})(\d{2})", s):
        return f"{m[1]}-{m[2]}"
    if m := re.fullmatch(r"(\d{4})([12])S", s):
        return f"{m[1]} S{m[2]}"
    if m := re.fullmatch(r"(\d{4})([1-4])T", s):
        return f"{m[1]} T{m[2]}"
    return s


def parse_period(p: str | None) -> str | None:
    """Accept '2026-09', '202609', '2026/09' -> '202609'. Semester/quarter codes pass through."""
    if p is None or p == "":
        return None
    s = str(p).strip()
    if m := re.fullmatch(r"(\d{4})[-/]?(\d{1,2})", s):
        month = int(m[2])
        if 1 <= month <= 12:
            return f"{m[1]}{month:02d}"
    if re.fullmatch(r"\d{4}[12]S|\d{4}[1-4]T|\d{4}", s):
        return s
    raise QueryError(f"Período inválido {p!r}. Usá 'AAAA-MM' (ej. '2025-01').")


def _jsonable(v: Any) -> Any:
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if isinstance(v, float) and v != v:  # NaN
        return None
    return v


class ResultCache:
    """LRU cache of tool results, valid for one version of the mirror file.

    The data only changes when `indec-sync` replaces the file (daily), so a result is valid until
    then: the cache empties itself when the file version changes. Bounded by total JSON size.
    Exceptions are never cached.
    """

    def __init__(self, max_bytes: int = 64 * 1024 * 1024, max_entry_bytes: int = 4 * 1024 * 1024):
        from collections import OrderedDict

        self.max_bytes = max_bytes
        self.max_entry_bytes = max_entry_bytes
        self._d: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self._version: object = None
        self._bytes = 0
        self.hits = self.misses = 0

    def get_or_compute(self, version: object, key: str, compute):
        with self._lock:
            if version != self._version:
                self._d.clear()
                self._bytes, self._version = 0, version
            if key in self._d:
                self._d.move_to_end(key)
                self.hits += 1
                return self._d[key][0]
            self.misses += 1
        value = compute()  # outside the lock: slow queries don't block cache hits
        size = len(json.dumps(value, default=str, ensure_ascii=False))
        if size <= self.max_entry_bytes:
            with self._lock:
                if self._version == version and key not in self._d:
                    self._d[key] = (value, size)
                    self._bytes += size
                    while self._bytes > self.max_bytes and self._d:
                        _, (_, sz) = self._d.popitem(last=False)
                        self._bytes -= sz
        return value

    def stats(self) -> dict:
        with self._lock:
            total = self.hits + self.misses
            return {"entries": len(self._d), "mb": round(self._bytes / 1e6, 2), "hits": self.hits,
                    "misses": self.misses, "hit_rate_pct": round(100 * self.hits / total, 1) if total else None}


class Mirror:
    def __init__(self, db_path: str | os.PathLike, limits: Limits = Limits()):
        self.db_path = Path(db_path)
        self.limits = limits
        self._swap_lock = threading.Lock()  # guards connection (re)open only
        self._slots = threading.BoundedSemaphore(limits.max_concurrent)
        self._con: duckdb.DuckDBPyConnection | None = None
        self._version: tuple[int, int] | None = None
        self._dims_cache: dict[str, dict] = {}
        self.cache = ResultCache()

    # -- connection --------------------------------------------------------------------------

    def _file_version(self) -> tuple[int, int]:
        st = self.db_path.stat()
        return (st.st_ino, st.st_mtime_ns)

    def _connection(self) -> duckdb.DuckDBPyConnection:
        """Reopen when `indec-sync` has atomically replaced the file.

        The previous connection is not closed explicitly: cursors from it may still be running.
        It is released once they finish (the replaced file's inode lives until then).
        """
        try:
            version = self._file_version()
        except FileNotFoundError:
            raise QueryError(
                "Los datos todavía no están disponibles (primera sincronización en curso). "
                "Reintentá en unos minutos."
            ) from None
        with self._swap_lock:
            if self._con is not None and version == self._version:
                return self._con
            # A fresh in-memory instance per file version, with the mirror ATTACHed: duckdb.connect()
            # on the same path would return the cached instance still bound to the replaced file.
            con = duckdb.connect(
                ":memory:", config={"threads": self.limits.threads, "memory_limit": self.limits.memory_limit}
            )
            path = str(self.db_path).replace("'", "''")
            con.execute(f"ATTACH '{path}' AS mirror (READ_ONLY)")
            con.execute("SET enable_external_access = false")
            con.execute("SET lock_configuration = true")
            self._con, self._version = con, version
            self._dims_cache = {}
            return con

    def _cursor(self) -> duckdb.DuckDBPyConnection:
        cur = self._connection().cursor()
        cur.execute("USE mirror")  # cursors don't inherit the parent's USE
        return cur

    def cached(self, name: str, args: dict, compute):
        """Run `compute()` once per (tool, args) per mirror version."""
        try:
            version = self._file_version()
        except FileNotFoundError:
            return compute()  # raises the friendly "not available yet" error
        key = name + ":" + json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)
        return self.cache.get_or_compute(version, key, compute)

    def close(self) -> None:
        with self._swap_lock:
            if self._con is not None:
                self._con.close()
                self._con = None

    def _run(self, sql: str, params: list | None = None, max_rows: int | None = None) -> dict:
        cap = max_rows or self.limits.max_rows
        if not self._slots.acquire(timeout=self.limits.queue_timeout_s):
            raise QueryError("El servidor está ocupado. Reintentá en unos segundos.")
        try:
            # One cursor per query: cursors run in parallel, and interrupt only their own query.
            cur = self._cursor()
            timer = threading.Timer(self.limits.timeout_s, cur.interrupt)
            timer.start()
            try:
                rel = cur.execute(sql, params or [])
                columns = [d[0] for d in rel.description]
                rows = rel.fetchmany(cap + 1)
            except duckdb.InterruptException:
                raise QueryError(
                    f"La consulta superó el límite de {self.limits.timeout_s:g}s. Agregá filtros o agregaciones."
                ) from None
            except duckdb.Error as e:
                raise QueryError(f"Error de SQL: {e}") from None
            finally:
                timer.cancel()
                cur.close()
        finally:
            self._slots.release()
        truncated = len(rows) > cap
        rows = [[_jsonable(v) for v in r] for r in rows[:cap]]
        return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated}

    def _records(self, sql: str, params: list | None = None, max_rows: int | None = None) -> list[dict]:
        res = self._run(sql, params, max_rows)
        return [dict(zip(res["columns"], r)) for r in res["rows"]]

    # -- catalog -----------------------------------------------------------------------------

    def datasets(self) -> list[dict]:
        rows = self._records(
            "SELECT table_name, summary, directorate, row_count, latest_periodo, status, "
            "CAST(synced_at AS VARCHAR) AS synced_at "
            "FROM _datasets ORDER BY directorate, table_name",
            max_rows=1000,
        )
        for r in rows:
            r["latest_period"] = format_period(r.pop("latest_periodo"))
        return rows

    def _dataset_meta(self, table: str) -> dict:
        rows = self._records(
            "SELECT * REPLACE (CAST(synced_at AS VARCHAR) AS synced_at) FROM _datasets WHERE table_name = ?",
            [table],
        )
        if not rows:
            names = [d["table_name"] for d in self.datasets()]
            raise QueryError(f"Dataset desconocido {table!r}. Disponibles: {', '.join(names)}")
        return rows[0]

    def describe(self, table: str, max_values: int = 60) -> dict:
        meta = self._dataset_meta(table)
        columns = self._records(
            "SELECT column_name, data_type, description FROM _columns WHERE table_name = ? "
            "AND column_name NOT LIKE '%\\_null' ESCAPE '\\'",
            [table],
            max_rows=1000,
        )
        dims = self._dimension_values(table, columns, max_values)
        for c in columns:
            if c["column_name"] in dims:
                c["values"] = dims[c["column_name"]]
        visible = ", ".join(f'"{c["column_name"]}"' for c in columns)
        sample = self._run(f'SELECT {visible} FROM "{table}" LIMIT 3')
        return {
            "table": table,
            "summary": meta["summary"],
            "description": meta["description"],
            "directorate": meta["directorate"],
            "row_count": meta["row_count"],
            "latest_period": format_period(meta["latest_periodo"]),
            "primary_key": meta["primary_key"],
            "status": meta["status"],
            "synced_at": _jsonable(meta["synced_at"]),
            "columns": columns,
            "sample_rows": sample,
            "notes": [
                "Cada columna de valor tiene una gemela `<col>_null` con la razón de nulo "
                "(signos convencionales INDEC: * provisorio, ** preliminar, e estimado, ...).",
                "`observaciones` puede decir 'Dato provisorio': mencionalo al citar la cifra.",
                "Preferí las variaciones publicadas (filas v_m, v_ia, ...) antes que calcularlas: "
                "INDEC advierte diferencias por redondeo.",
            ],
            "source": SOURCE,
        }

    def _dimension_values(self, table: str, columns: list[dict], max_values: int) -> dict[str, list]:
        """Distinct values of low-cardinality text columns, so filters can be written exactly."""
        key = f"{table}:{max_values}"
        if key in self._dims_cache:
            return self._dims_cache[key]
        out = {}
        for c in columns:
            name = c["column_name"]
            if c["data_type"] != "VARCHAR" or name in ("periodo", "observaciones"):
                continue
            res = self._run(
                f'SELECT DISTINCT "{name}" FROM "{table}" WHERE "{name}" IS NOT NULL ORDER BY 1 LIMIT ?',
                [max_values + 1],
                max_rows=max_values + 1,
            )
            values = [r[0] for r in res["rows"]]
            if 0 < len(values) <= max_values:
                out[name] = values
        self._dims_cache[key] = out
        return out

    # -- free-form SQL -----------------------------------------------------------------------

    def query(self, sql: str) -> dict:
        sql = sql.strip().rstrip(";")
        try:
            cur = self._cursor()
            try:
                statements = cur.extract_statements(sql)
            finally:
                cur.close()
        except duckdb.Error as e:
            raise QueryError(f"Error de SQL: {e}") from None
        if len(statements) != 1:
            raise QueryError("Enviá exactamente una consulta SELECT por llamada.")
        if statements[0].type != duckdb.StatementType.SELECT:
            raise QueryError(f"Solo se permiten consultas SELECT (recibido: {statements[0].type.name}).")
        res = self._run(sql)
        res["source"] = SOURCE
        if res["truncated"]:
            res["note"] = f"Resultado truncado a {self.limits.max_rows} filas. Agregá filtros o agregaciones."
        return res

    # -- intent-level helpers ----------------------------------------------------------------

    def ipc_categories(self, region: str = "Nacional") -> list[dict]:
        return self._records(
            "SELECT DISTINCT nivel, codigo, descripcion FROM ipc_nacional "
            "WHERE region = ? AND nivel IN ('Nivel General', 'Categoría', 'División') "
            "ORDER BY nivel DESC, codigo",
            [region],
            max_rows=200,
        )

    def _resolve_ipc_category(self, category: str, region: str) -> dict:
        """Match by exact name/code, then substring, among categories published for `region`.

        INDEC publishes Grupo/Clase/Producto detail only for the regions, not for Nacional, so
        matching across all regions would resolve 'alimentos' to a series that is empty there.
        """
        cat = category.strip()
        base = (
            "SELECT DISTINCT clasificador, nivel, codigo, descripcion FROM ipc_nacional "
            "WHERE region = ? AND nivel <> 'Producto' AND "
        )
        exact = self._records(base + "(lower(descripcion) = lower(?) OR codigo = ?)", [region, cat, cat])
        if len(exact) == 1:
            return exact[0]
        candidates = exact or self._records(
            base + "descripcion ILIKE ? ORDER BY nivel, codigo LIMIT 25", [region, f"%{cat}%"]
        )
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            main = ", ".join(c["descripcion"] for c in self.ipc_categories(region))
            raise QueryError(
                f"No encontré la categoría de IPC {category!r} para la región {region}. "
                f"Disponibles: {main}. (Grupos y clases solo se publican por región, no para Nacional.)"
            )
        opts = "; ".join(f"{c['descripcion']} ({c['nivel']}, código {c['codigo']})" for c in candidates)
        raise QueryError(f"{category!r} es ambiguo en {region}. Opciones: {opts}")

    def ipc_series(
        self,
        category: str = "Nivel general",
        region: str = "Nacional",
        measures: list[str] | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> dict:
        measures = measures or ["v_m", "v_ia"]
        bad = [m for m in measures if m not in IPC_MEASURES]
        if bad:
            raise QueryError(f"Medidas inválidas {bad}. Válidas: {', '.join(IPC_MEASURES)}")
        reg = next((r for r in IPC_REGIONS if r.lower() == region.strip().lower()), None)
        if reg is None:
            raise QueryError(f"Región inválida {region!r}. Válidas: {', '.join(IPC_REGIONS)}")
        cat = self._resolve_ipc_category(category, reg)
        start, end = parse_period(start), parse_period(end)

        where = ["clasificador = ?", "nivel = ?", "codigo = ?", "region = ?", "list_contains(?, tipo_de_indicador)"]
        params: list = [cat["clasificador"], cat["nivel"], cat["codigo"], reg, measures]
        if start:
            where.append("periodo >= ?")
            params.append(start)
        if end:
            where.append("periodo <= ?")
            params.append(end)
        if not start and not end:  # default: last 13 months
            where.append(
                "periodo >= (SELECT strftime(strptime(max(periodo), '%Y%m') - INTERVAL 12 MONTH, '%Y%m') "
                "FROM ipc_nacional)"
            )
        pivot_cols = ", ".join(
            f"max(valor) FILTER (WHERE tipo_de_indicador = '{m}') AS \"{m}\"" for m in measures
        )
        rows = self._records(
            f"SELECT periodo, {pivot_cols}, "
            "string_agg(DISTINCT observaciones, '; ') AS observaciones "
            f"FROM ipc_nacional WHERE {' AND '.join(where)} GROUP BY periodo ORDER BY periodo",
            params,
        )
        for r in rows:
            r["periodo"] = format_period(r["periodo"])
        return {
            "category": cat["descripcion"],
            "level": cat["nivel"],
            "region": reg,
            "measures": {m: IPC_MEASURES[m] for m in measures},
            "rows": rows,
            "source": f"{SOURCE} Índice de Precios al Consumidor (ipc_nacional).",
        }

    # -- comparable series ------------------------------------------------------------------

    def _series(self, spec: str) -> dict:
        """Resolve a series id to {label, table, notes, data: {periodo: {index, v_m, v_ia, obs}}}.

        Ids: 'ipc[:categoría][@región]', 'salarios[:sector]', 'cbt', 'cba', 'emae'.
        `index` is a level usable for rebasing (IPC/salarios/EMAE index; CBT/CBA in pesos).
        """
        raw = spec.strip()
        kind, _, rest = raw.partition(":")
        kind = kind.strip().lower()
        if "@" in kind:  # 'ipc@GBA'
            kind, _, reg_part = kind.partition("@")
            rest = f"Nivel general@{reg_part}"
        if kind == "emae" and rest:  # 'emae:Construcción' -> sector series
            kind = "emae_sector"
        notes: list[str] = []

        if kind == "ipc":
            cat_part, _, reg_part = (rest or "Nivel general").partition("@")
            region = next((r for r in IPC_REGIONS if r.lower() == (reg_part or "Nacional").strip().lower()), None)
            if region is None:
                raise QueryError(f"Región inválida en {spec!r}. Válidas: {', '.join(IPC_REGIONS)}")
            cat = self._resolve_ipc_category(cat_part or "Nivel general", region)
            label = f"IPC {cat['descripcion']} ({region})"
            rows = self._records(
                "SELECT periodo, tipo_de_indicador AS t, valor, observaciones FROM ipc_nacional "
                "WHERE clasificador = ? AND nivel = ? AND codigo = ? AND region = ? "
                "AND tipo_de_indicador IN ('Indice', 'v_m', 'v_ia')",
                [cat["clasificador"], cat["nivel"], cat["codigo"], region], max_rows=10_000,
            )
            mapping, table = {"Indice": "index", "v_m": "v_m", "v_ia": "v_ia"}, "ipc_nacional"

        elif kind in ("salarios", "salario", "wages"):
            code = self._resolve_wage_code(rest or "Nivel general")
            desc = self._records("SELECT DISTINCT descripcion FROM indice_salarios WHERE codigo = ?", [code])[0]["descripcion"]
            label = f"Salarios: {desc}"
            if code == "200":
                notes.append("Salarios no registrados: INDEC los publica con ~5 meses de rezago respecto del período.")
            rows = self._records(
                "SELECT periodo, tipo_de_indicador AS t, valor, observaciones FROM indice_salarios "
                "WHERE codigo = ? AND tipo_de_indicador IN ('Indice', 'v_m', 'v_ia')",
                [code], max_rows=10_000,
            )
            mapping, table = {"Indice": "index", "v_m": "v_m", "v_ia": "v_ia"}, "indice_salarios"

        elif kind in ("cbt", "cba"):
            desc = "Canasta Basica Total" if kind == "cbt" else "Canasta Basica Alimentaria"
            label = f"{kind.upper()} por adulto equivalente (GBA, $)"
            rows = self._records(
                "SELECT periodo, tipo_de_indicador AS t, valor, NULL AS observaciones "
                "FROM valorizacion_canastas_cba_cbt WHERE descripcion = ? AND tipo_de_indicador IN (?, 'v_m', 'v_ia')",
                [desc, kind.upper()], max_rows=10_000,
            )
            mapping, table = {kind.upper(): "index", "v_m": "v_m", "v_ia": "v_ia"}, "valorizacion_canastas_cba_cbt"

        elif kind == "emae":
            label = "EMAE nivel general"
            notes.append("EMAE: v_m e índice usan la serie desestacionalizada; v_ia, la serie original.")
            rows = self._records(
                "SELECT periodo, CASE tipo_de_indicador_descripcion "
                "  WHEN 'Índice serie desestacionalizada' THEN 'index' "
                "  WHEN 'Var % mensual serie desestacionalizada' THEN 'v_m' "
                "  WHEN 'Var % interanual serie original' THEN 'v_ia' END AS t, valor, observaciones "
                "FROM estimador_mensual_de_actividad_economica_nivel_general",
                max_rows=10_000,
            )
            mapping, table = {"index": "index", "v_m": "v_m", "v_ia": "v_ia"}, \
                "estimador_mensual_de_actividad_economica_nivel_general"
        elif kind in _INDEX_KINDS:
            from . import indices  # local import: indices imports this module

            ds = indices.DATASETS[_INDEX_KINDS[kind]]
            cat = indices._resolve(self, ds, rest or None)
            is_ipi = ds.key.startswith("IPI_")
            idx = ds.measures["indice_desestacionalizado" if is_ipi else "indice"].sql
            vm = ds.measures["v_m"].sql if "v_m" in ds.measures else "FALSE"  # EMAE sectors: no monthly change
            desc = "Nivel general" if "nivel general" in cat["descripcion"].lower() else cat["descripcion"]
            label = f"{_INDEX_LABELS[ds.key]}: {desc}"
            if is_ipi:
                notes.append(f"{_INDEX_LABELS[ds.key]}: índice y v_m de la serie desestacionalizada; v_ia, de la original.")
            if ds.key == "EMAE_SECTORES":
                notes.append("EMAE por sector: solo serie original (índice y v_ia); no hay variación mensual.")
            obs = "observaciones" if ds.has_observaciones else "NULL"
            rows = self._records(
                f"SELECT periodo, CASE WHEN {idx} THEN 'index' WHEN {vm} THEN 'v_m' "
                f"WHEN {ds.measures['v_ia'].sql} THEN 'v_ia' END AS t, valor, {obs} AS observaciones "
                f"FROM {ds.table} WHERE {ds.scope_sql} AND " + " AND ".join(f"{c} = ?" for c in ds.id_cols),
                [cat[c] for c in ds.id_cols], max_rows=10_000,
            )
            mapping, table = {"index": "index", "v_m": "v_m", "v_ia": "v_ia"}, ds.table
        else:
            raise QueryError(
                f"Serie desconocida {spec!r}. Usá: 'ipc[:categoría][@región]' (ej. 'ipc:Alimentos@GBA'), "
                "'salarios[:registrado|no registrado|privado registrado|público]', 'cbt', 'cba', 'emae', "
                "'ipim', 'ipib', 'ipp', 'icc[:categoría]' (ej. 'icc:Mano de obra'), 'ipi[:rama]', 'ipi_minero'."
            )

        data: dict[str, dict] = {}
        for r in rows:
            key = mapping.get(r["t"])
            if key is None:
                continue
            d = data.setdefault(r["periodo"], {"index": None, "v_m": None, "v_ia": None, "obs": None})
            d[key] = r["valor"]
            if r["observaciones"]:
                d["obs"] = r["observaciones"]
        if not data:
            raise QueryError(f"La serie {spec!r} no tiene datos.")
        return {"id": raw, "label": label, "table": table, "notes": notes, "data": data}

    def _resolve_wage_code(self, name: str) -> str:
        aliases = {
            "nivel general": "0", "general": "0", "total": "0",
            "registrado": "100", "registrados": "100", "total registrado": "100",
            "no registrado": "200", "no registrados": "200", "informal": "200", "total no registrado": "200",
            "privado": "110", "privado registrado": "110", "sector privado registrado": "110",
            "publico": "120", "público": "120", "sector público": "120",
            "publico nacional": "121", "público nacional": "121",
            "publico provincial": "122", "público provincial": "122",
        }
        key = name.strip().lower()
        if key in aliases:
            return aliases[key]
        rows = self._records(
            "SELECT DISTINCT codigo, descripcion FROM indice_salarios WHERE codigo = ? OR descripcion ILIKE ?",
            [name.strip(), f"%{name.strip()}%"],
        )
        if len(rows) == 1:
            return rows[0]["codigo"]
        opts = self._records("SELECT DISTINCT codigo, descripcion FROM indice_salarios ORDER BY codigo")
        raise QueryError(
            f"Sector de salarios {name!r} no encontrado o ambiguo. Opciones: "
            + "; ".join(f"{o['descripcion']} ({o['codigo']})" for o in opts)
        )

    def compare(
        self,
        series: list[str],
        measure: str = "v_ia",
        start: str | None = None,
        end: str | None = None,
    ) -> dict:
        if not 2 <= len(series) <= 4:
            raise QueryError("Compará entre 2 y 4 series.")
        if measure not in ("v_ia", "v_m", "base100"):
            raise QueryError("measure debe ser 'v_ia' (interanual), 'v_m' (mensual) o 'base100' (índice rebasado).")
        frames = [self._series(s) for s in series]
        start, end = parse_period(start), parse_period(end)
        for p in (start, end):
            if p is not None and not re.fullmatch(r"\d{6}", p):
                raise QueryError("Para comparar series mensuales usá períodos 'AAAA-MM'.")

        common = sorted(set.intersection(*(set(f["data"]) for f in frames)))
        if end:
            common = [p for p in common if p <= end]
        if start:
            common = [p for p in common if p >= start]
        elif common:
            common = common[-13:]  # default: last 13 common months
        if not common:
            raise QueryError("Las series no tienen meses en común en ese rango.")

        def val(f, p, key):
            return f["data"][p][key]

        rows = []
        for p in common:
            row: dict[str, Any] = {"periodo": format_period(p)}
            for f in frames:
                if measure == "base100":
                    base = val(f, common[0], "index")
                    cur = val(f, p, "index")
                    row[f["label"]] = round(cur / base * 100, 2) if base and cur is not None else None
                else:
                    v = val(f, p, measure)
                    row[f["label"]] = round(v, 2) if isinstance(v, float) else v
            flags = sorted({val(f, p, "obs") for f in frames if val(f, p, "obs")})
            if flags:
                row["observaciones"] = "; ".join(flags)
            rows.append(row)

        first, last = common[0], common[-1]
        changes = {}
        for f in frames:
            a, b = val(f, first, "index"), val(f, last, "index")
            changes[f["label"]] = round((b / a - 1) * 100, 1) if a and b is not None else None
        summary: dict[str, Any] = {
            "desde": format_period(first),
            "hasta": format_period(last),
            "variacion_entre_niveles_pct": changes,
            "nota": (
                f"CALCULADO por este servicio a partir de niveles publicados: variación entre el nivel de "
                f"{format_period(first)} y el de {format_period(last)} (no incluye la variación del propio "
                f"mes inicial). No es una cifra oficial; puede diferir por redondeo."
            ),
        }
        missing = [k for k, v in changes.items() if v is None]
        if missing:
            summary["sin_nivel"] = f"Sin índice publicado para: {', '.join(missing)} (solo variaciones v_m / v_ia)."
        labels = [f["label"] for f in frames]
        if len(frames) == 2 and None not in changes.values():
            a, b = changes[labels[0]], changes[labels[1]]
            summary["relativo_pct"] = round(((1 + a / 100) / (1 + b / 100) - 1) * 100, 1)
            summary["relativo_explicacion"] = (
                f"Cuánto varió '{labels[0]}' en términos de '{labels[1]}' "
                f"(ej. salario real si la segunda serie es el IPC). Calculado."
            )

        return {
            "measure": {"v_ia": "Variación % interanual (publicada)", "v_m": "Variación % mensual (publicada)",
                        "base100": f"Nivel rebasado a {format_period(first)} = 100 (calculado)"}[measure],
            "series": [
                {"id": f["id"], "label": f["label"], "dataset": f["table"],
                 "ultimo_periodo": format_period(max(f["data"]))}
                for f in frames
            ],
            "rows": rows,
            "summary": summary,
            "notes": [n for f in frames for n in f["notes"]]
                     + ["Solo se muestran los meses que todas las series tienen en común."],
            "source": SOURCE,
        }

    def snapshot(self) -> dict:
        """Latest headline figures across datasets, each with its own period."""
        out: dict[str, Any] = {}

        ipc = self._records(
            "SELECT periodo, descripcion, tipo_de_indicador, valor FROM ipc_nacional "
            "WHERE region = 'Nacional' AND nivel IN ('Nivel General', 'Categoría') "
            "AND descripcion IN ('Nivel general', 'Núcleo') AND tipo_de_indicador IN ('v_m','v_ia','v_acum') "
            "AND periodo = (SELECT max(periodo) FROM ipc_nacional)"
        )
        if ipc:
            def pick(desc, m):
                return next((r["valor"] for r in ipc if r["descripcion"] == desc and r["tipo_de_indicador"] == m), None)

            out["inflacion_ipc_nacional"] = {
                "periodo": format_period(ipc[0]["periodo"]),
                "mensual_pct": pick("Nivel general", "v_m"),
                "interanual_pct": pick("Nivel general", "v_ia"),
                "acumulada_anio_pct": pick("Nivel general", "v_acum"),
                "nucleo_mensual_pct": pick("Núcleo", "v_m"),
                "nucleo_interanual_pct": pick("Núcleo", "v_ia"),
            }

        emae = self._records(
            "SELECT periodo, tipo_de_indicador_descripcion AS d, valor "
            "FROM estimador_mensual_de_actividad_economica_nivel_general "
            "WHERE periodo = (SELECT max(periodo) FROM estimador_mensual_de_actividad_economica_nivel_general)"
        )
        if emae:
            by = {r["d"]: r["valor"] for r in emae}
            out["actividad_emae"] = {
                "periodo": format_period(emae[0]["periodo"]),
                "interanual_pct": _round(by.get("Var % interanual serie original")),
                "mensual_desestacionalizada_pct": _round(by.get("Var % mensual serie desestacionalizada")),
            }

        sal = self._records(
            "SELECT periodo, tipo_de_indicador, valor FROM indice_salarios WHERE codigo = '0' "
            "AND tipo_de_indicador IN ('v_m','v_ia') AND periodo = (SELECT max(periodo) FROM indice_salarios)"
        )
        if sal:
            by = {r["tipo_de_indicador"]: r["valor"] for r in sal}
            out["salarios_nivel_general"] = {
                "periodo": format_period(sal[0]["periodo"]),
                "mensual_pct": by.get("v_m"),
                "interanual_pct": by.get("v_ia"),
            }

        can = self._records(
            "SELECT periodo, descripcion, tipo_de_indicador, valor FROM valorizacion_canastas_cba_cbt "
            "WHERE periodo = (SELECT max(periodo) FROM valorizacion_canastas_cba_cbt) "
            "AND (tipo_de_indicador IN ('CBA','CBT') OR descripcion = 'Canasta Basica Total, Hogar 2' "
            "OR (descripcion = 'Canasta Basica Total' AND tipo_de_indicador = 'v_ia'))"
        )
        if can:
            def cpick(desc, t):
                return next((r["valor"] for r in can if r["descripcion"] == desc and r["tipo_de_indicador"] == t), None)

            out["canastas_gba"] = {
                "periodo": format_period(can[0]["periodo"]),
                "cba_adulto_equivalente_ars": cpick("Canasta Basica Alimentaria", "CBA"),
                "cbt_adulto_equivalente_ars": cpick("Canasta Basica Total", "CBT"),
                "cbt_hogar_tipo_2_ars": cpick("Canasta Basica Total, Hogar 2", "Hogar 2"),
                "cbt_interanual_pct": cpick("Canasta Basica Total", "v_ia"),
                "nota": "Hogar 2: 4 integrantes (pareja con 2 hijos). Línea de pobreza = CBT.",
            }

        pob = self._records("SELECT * FROM pobreza_eph ORDER BY periodo DESC LIMIT 1")
        if pob:
            p = pob[0]
            out["pobreza_31_aglomerados"] = {
                "periodo": format_period(p["periodo"]),
                "pobreza_personas_pct": p["tasa_pobreza_personas"],
                "indigencia_personas_pct": p["tasa_indigencia_personas"],
                "pobreza_hogares_pct": p["tasa_pobreza_hogares"],
                "observaciones": p["observaciones"],
            }

        tra = self._records("SELECT * FROM mercado_trabajo_eph ORDER BY periodo DESC LIMIT 1")
        if tra:
            t = tra[0]
            out["mercado_de_trabajo_31_aglomerados"] = {
                "periodo": format_period(t["periodo"]),
                "desocupacion_pct": t["tasa_de_desocupacion"],
                "actividad_pct": t["tasa_de_actividad"],
                "empleo_pct": t["tasa_de_empleo"],
                "empleo_informal_pct": t["tasa_de_empleo_informal"],
                "observaciones": t["observaciones"],
            }

        from . import indices  # local import: indices imports this module

        for key, label, wanted in (
            ("IPIM", "precios_mayoristas_ipim", ("v_m", "v_ia")),
            ("ICC", "costo_construccion_icc_gba", ("v_m", "v_ia")),
            ("IPI_MANUFACTURERO", "industria_ipi_manufacturero", ("v_ia", "v_m")),
            ("IPI_MINERO", "mineria_ipi_minero", ("v_ia", "v_m")),
        ):
            try:
                last = indices.series(self, key, None, list(wanted))["rows"][-1]
            except QueryError:
                continue
            names = {"v_m": "mensual_pct", "v_ia": "interanual_pct"}
            out[label] = {"periodo": last["periodo"], **{names[w]: last.get(w) for w in wanted}}
            if last.get("observaciones"):
                out[label]["observaciones"] = last["observaciones"]
            if key.startswith("IPI_"):
                out[label]["nota"] = "interanual: serie original; mensual: serie desestacionalizada"

        synced = self._records("SELECT CAST(max(synced_at) AS VARCHAR) AS s FROM _datasets")
        out["synced_at"] = synced[0]["s"] if synced else None
        out["source"] = SOURCE
        return out


def _round(v: float | None, n: int = 1) -> float | None:
    return None if v is None else round(v, n)
