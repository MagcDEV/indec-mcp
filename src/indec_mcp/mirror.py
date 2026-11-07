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


class QueryError(ValueError):
    """A user-facing error: bad SQL, unknown dataset, ambiguous filter. Message is safe to show."""


@dataclass(frozen=True)
class Limits:
    max_rows: int = 500
    timeout_s: float = 5.0
    threads: int = 2
    memory_limit: str = "1GB"


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


class Mirror:
    def __init__(self, db_path: str | os.PathLike, limits: Limits = Limits()):
        self.db_path = Path(db_path)
        self.limits = limits
        self._lock = threading.Lock()
        self._con: duckdb.DuckDBPyConnection | None = None
        self._version: tuple[int, int] | None = None
        self._dims_cache: dict[str, dict] = {}

    # -- connection --------------------------------------------------------------------------

    def _file_version(self) -> tuple[int, int]:
        st = self.db_path.stat()
        return (st.st_ino, st.st_mtime_ns)

    def _connection(self) -> duckdb.DuckDBPyConnection:
        """Reopen when `indec-sync` has atomically replaced the file."""
        if not self.db_path.exists():
            raise QueryError(f"No mirror at {self.db_path}. Run `indec-sync` first.")
        version = self._file_version()
        if self._con is None or version != self._version:
            if self._con is not None:
                self._con.close()
            con = duckdb.connect(
                str(self.db_path),
                read_only=True,
                config={
                    "enable_external_access": False,
                    "threads": self.limits.threads,
                    "memory_limit": self.limits.memory_limit,
                },
            )
            con.execute("SET lock_configuration = true")
            self._con, self._version = con, version
            self._dims_cache.clear()
        return self._con

    def close(self) -> None:
        with self._lock:
            if self._con is not None:
                self._con.close()
                self._con = None

    def _run(self, sql: str, params: list | None = None, max_rows: int | None = None) -> dict:
        cap = max_rows or self.limits.max_rows
        with self._lock:
            cur = self._connection().cursor()
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
            with self._lock:
                statements = self._connection().extract_statements(sql)
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

        synced = self._records("SELECT CAST(max(synced_at) AS VARCHAR) AS s FROM _datasets")
        out["synced_at"] = synced[0]["s"] if synced else None
        out["source"] = SOURCE
        return out


def _round(v: float | None, n: int = 1) -> float | None:
    return None if v is None else round(v, n)
