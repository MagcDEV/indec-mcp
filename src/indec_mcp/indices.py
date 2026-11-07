"""Dedicated series tools for wholesale prices (SIPM), construction costs (ICC) and industrial
production (IPI manufacturero / minero).

Idea adapted from SantinoSpingola/indec-mcp (MIT): one tool per index, so the model doesn't have to
discover each table's quirks through SQL. Each dataset names things differently, so the
differences live in the DATASETS table below; resolution and pivoting are shared.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Any

from .mirror import SOURCE, Mirror, QueryError, format_period, parse_period


def _norm(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn").strip()


_GENERAL = {"", "nivel general", "general", "total", "ng"}


@dataclass(frozen=True)
class Measure:
    label: str
    sql: str  # condition on the row, built only from constants below (never from user input)


@dataclass(frozen=True)
class Spec:
    key: str
    table: str
    title: str
    id_cols: tuple[str, ...]  # columns that identify one category
    code_col: str
    measures: dict[str, Measure]
    default_measures: tuple[str, ...]
    scope_sql: str = "TRUE"  # rows of this index within the table
    level_col: str | None = None
    has_observaciones: bool = True
    notes: tuple[str, ...] = ()
    general_like: str = "nivel general"
    excluded_levels: tuple[str, ...] = field(default_factory=tuple)


def _tipo(t: str) -> str:
    return f"tipo_de_indicador = '{t}'"


_PRICE_MEASURES = {
    "v_m": Measure("Variación % mensual", _tipo("v_m")),
    "v_ia": Measure("Variación % interanual", _tipo("v_ia")),
    "v_acum": Measure("Variación % acumulada en el año", _tipo("v_acum")),
    "indice": Measure("Índice", _tipo("Indice")),
    "incidencia": Measure("Incidencia en la variación mensual del nivel general (p.p.)", _tipo("incidencia")),
}


def _ipi_measures(original: str, desest: str, tc: str) -> dict[str, Measure]:
    return {
        "v_ia": Measure("Variación % interanual (serie original)",
                        f"serie = '{original}' AND tipo_de_indicador = 'Variación interanual'"),
        "v_acum": Measure("Variación % acumulada interanual (serie original)",
                          f"serie = '{original}' AND tipo_de_indicador = 'Variación acumulada interanual'"),
        "v_m": Measure("Variación % mensual (serie desestacionalizada)",
                       f"serie = '{desest}' AND tipo_de_indicador = 'Variación intermensual'"),
        "indice": Measure("Índice (serie original)", f"serie = '{original}' AND tipo_de_indicador = 'Índice'"),
        "indice_desestacionalizado": Measure("Índice (serie desestacionalizada)",
                                             f"serie = '{desest}' AND tipo_de_indicador = 'Índice'"),
        "v_m_tendencia": Measure("Variación % mensual (serie tendencia-ciclo)",
                                 f"serie = '{tc}' AND tipo_de_indicador = 'Variación intermensual'"),
        "indice_tendencia": Measure("Índice (serie tendencia-ciclo)", f"serie = '{tc}' AND tipo_de_indicador = 'Índice'"),
    }


WHOLESALE_INDICES = {
    "IPIM": "Índice de precios internos al por mayor (productos nacionales e importados, con impuestos)",
    "IPIB": "Índice de precios internos básicos al por mayor (sin impuestos)",
    "IPP": "Índice de precios básicos del productor (solo productos nacionales)",
}

_EMAE_GENERAL = {
    "v_ia": Measure("Variación % interanual (serie original)",
                    "tipo_de_indicador_descripcion = 'Var % interanual serie original'"),
    "v_m": Measure("Variación % mensual (serie desestacionalizada)",
                   "tipo_de_indicador_descripcion = 'Var % mensual serie desestacionalizada'"),
    "indice": Measure("Índice base 2004 = 100 (serie original)", "tipo_de_indicador_descripcion = 'Índice serie original'"),
    "indice_desestacionalizado": Measure("Índice base 2004 = 100 (serie desestacionalizada)",
                                         "tipo_de_indicador_descripcion = 'Índice serie desestacionalizada'"),
    "v_m_tendencia": Measure("Variación % mensual (serie tendencia-ciclo)",
                             "tipo_de_indicador_descripcion = 'Var % mensual serie tendencia-ciclo'"),
    "indice_tendencia": Measure("Índice base 2004 = 100 (serie tendencia-ciclo)",
                                "tipo_de_indicador_descripcion = 'Índice serie tendencia-ciclo'"),
}

DATASETS = {
    **{
        k: Spec(
            key=k, table="sipm_precios_mayoristas", title=f"{k}: {desc}",
            id_cols=("clasificador", "nivel", "codigo_apertura", "descripcion"), code_col="codigo_apertura",
            level_col="nivel", scope_sql=f"indicador = '{k}'", measures=_PRICE_MEASURES,
            default_measures=("v_m", "v_ia"),
            notes=("Índice base 1993; nivel dic-2015 = 100. Precios mayoristas, total del país.",),
        )
        for k, desc in WHOLESALE_INDICES.items()
    },
    "ICC": Spec(
        key="ICC", table="icc_precios_construccion",
        title="ICC: Índice del costo de la construcción (Gran Buenos Aires)",
        id_cols=("clasificador", "nivel", "codigo_apertura", "descripcion"), code_col="codigo_apertura",
        level_col="nivel", scope_sql="indicador = 'ICC'", measures=_PRICE_MEASURES, default_measures=("v_m", "v_ia"),
        notes=("Base 1993; nivel ene-2022 = 100. Corresponde a vivienda tipo en el Gran Buenos Aires.",),
    ),
    "SALARIOS": Spec(
        key="SALARIOS", table="indice_salarios", title="Índice de salarios (base oct-2016 = 100)",
        id_cols=("nivel", "codigo", "descripcion"), code_col="codigo", level_col="nivel",
        measures={k: v for k, v in _PRICE_MEASURES.items() if k != "incidencia"}, default_measures=("v_m", "v_ia"),
        notes=("Mide la evolución de los salarios (no su nivel en pesos), aislando horas trabajadas, ausentismo y premios.",
               "Los salarios no registrados se publican con ~5 meses de rezago respecto del período informado.",
               "Los subsectores públicos nacional y provincial no tienen índice publicado, solo variaciones."),
    ),
    "EMAE_GENERAL": Spec(
        key="EMAE_GENERAL", table="estimador_mensual_de_actividad_economica_nivel_general",
        title="EMAE: Estimador mensual de actividad económica, nivel general (base 2004 = 100)",
        id_cols=(), code_col="", measures=_EMAE_GENERAL, default_measures=("v_ia", "v_m"),
        notes=("v_ia viene de la serie original; v_m, de la desestacionalizada. Anticipa la evolución del PIB.",),
    ),
    "EMAE_SECTORES": Spec(
        key="EMAE_SECTORES", table="estimador_mensual_de_actividad_economica_sectores",
        title="EMAE por sector de actividad (base 2004 = 100, serie original)",
        id_cols=("codigo", "descripcion"), code_col="codigo",
        measures={
            "v_ia": Measure("Variación % interanual (serie original)", "tipo_de_indicador = 'Variación porcentual interanual'"),
            "indice": Measure("Índice base 2004 = 100 (serie original)", "tipo_de_indicador = 'Índice base 2004=100'"),
        },
        default_measures=("v_ia",),
        notes=("Por sector solo se publica la serie original (índice y variación interanual), sin desestacionalizar.",),
    ),
    "IPI_MANUFACTURERO": Spec(
        key="IPI_MANUFACTURERO", table="ipi_manufacturero",
        title="IPI manufacturero: Índice de producción industrial manufacturero (base 2004 = 100)",
        id_cols=("codigo_clanae_2004", "descripcion"), code_col="codigo_clanae_2004",
        measures=_ipi_measures("Serie original", "Serie desestacionalizada", "Serie tendencia-ciclo"),
        default_measures=("v_ia", "v_m"), has_observaciones=False,
        notes=("v_ia y v_acum vienen de la serie original; v_m, de la desestacionalizada. Las series "
               "desestacionalizada y tendencia-ciclo solo existen para el nivel general y las divisiones "
               "de 2 dígitos.",),
    ),
    "IPI_MINERO": Spec(
        key="IPI_MINERO", table="ipi_minero",
        title="IPI minero: Índice de producción industrial minero (base 2016 = 100)",
        id_cols=("codigo_clanae_2010", "codigo_cpc_2_1", "descripcion"), code_col="codigo_clanae_2010",
        measures=_ipi_measures("Serie Original", "Serie Desestacionalizada", "Serie Tendencia-Ciclo"),
        default_measures=("v_ia", "v_m"), has_observaciones=False,
        notes=("v_ia y v_acum vienen de la serie original; v_m, de la desestacionalizada, que solo existe para "
               "el nivel general. Las unidades físicas (m³, toneladas) se consultan con query_sql.",),
    ),
}


def _resolve(m: Mirror, spec: Spec, category: str | None) -> dict:
    if not spec.id_cols:  # single-series table (e.g. EMAE nivel general)
        if _norm(category or "") not in _GENERAL:
            raise QueryError(f"{spec.key} no tiene categorías: es un único nivel general.")
        return {"descripcion": "Nivel general"}
    cols = ", ".join(spec.id_cols)
    rows = m._records(
        f"SELECT DISTINCT {cols}{', ' + spec.level_col if spec.level_col and spec.level_col not in spec.id_cols else ''} "
        f"FROM {spec.table} WHERE {spec.scope_sql}",
        max_rows=5000,
    )
    q = _norm(category or "")
    if q in _GENERAL:
        hits = [r for r in rows if spec.general_like in _norm(r["descripcion"])]
    else:
        hits = [r for r in rows if _norm(r["descripcion"]) == q or _norm(str(r[spec.code_col])) == q]
        if not hits:
            hits = [r for r in rows if q in _norm(r["descripcion"])]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        tops = [r["descripcion"] for r in rows if spec.level_col and r.get(spec.level_col) in
                ("Nivel general", "Capitulo", "Sector", "Origen", "Categoría de Tabulación")]
        if not tops:  # IPI: 2-digit divisions
            tops = sorted({r["descripcion"] for r in rows if len(str(r[spec.code_col])) <= 5})[:25]
        raise QueryError(f"No encontré la categoría {category!r} en {spec.key}. Algunas opciones: "
                         + "; ".join(dict.fromkeys(tops)))
    opts = "; ".join(
        f"{r['descripcion']} ({(r.get(spec.level_col) + ', ') if spec.level_col else ''}código {r[spec.code_col]})"
        for r in hits[:15]
    )
    raise QueryError(f"{category!r} es ambiguo en {spec.key}. Opciones (podés pasar el código): {opts}")


def series(
    m: Mirror,
    key: str,
    category: str | None = None,
    measures: list[str] | None = None,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    spec = DATASETS[key]
    measures = measures or list(spec.default_measures)
    bad = [x for x in measures if x not in spec.measures]
    if bad:
        raise QueryError(f"Medidas inválidas {bad}. Válidas para {spec.key}: {', '.join(spec.measures)}")
    cat = _resolve(m, spec, category)
    start, end = parse_period(start), parse_period(end)

    where = [spec.scope_sql] + [f"{c} = ?" for c in spec.id_cols]
    params: list = [cat[c] for c in spec.id_cols]
    if start:
        where.append("periodo >= ?")
        params.append(start if len(start) == 6 else start + "01")
    if end:
        where.append("periodo <= ?")
        params.append(end if len(end) == 6 else end + "12")
    if not start and not end:
        where.append(
            f"periodo >= (SELECT strftime(strptime(max(periodo), '%Y%m') - INTERVAL 12 MONTH, '%Y%m') "
            f"FROM {spec.table} WHERE {spec.scope_sql})"
        )
    pivot = ", ".join(
        f"round(max(valor) FILTER (WHERE {spec.measures[x].sql}), 2) AS \"{x}\"" for x in measures
    )
    obs = ", string_agg(DISTINCT observaciones, '; ') AS observaciones" if spec.has_observaciones else ""
    rows = m._records(
        f"SELECT periodo, {pivot}{obs} FROM {spec.table} WHERE {' AND '.join(where)} "
        f"GROUP BY periodo ORDER BY periodo",
        params,
    )
    rows = [r for r in rows if any(r[x] is not None for x in measures)]
    if not rows:
        raise QueryError(
            f"No hay datos de {', '.join(measures)} para '{cat['descripcion']}' en ese período. "
            + ("Las series desestacionalizadas solo existen para el nivel general y grandes divisiones."
               if any(x not in ("v_ia", "v_acum", "indice") for x in measures) and key.startswith("IPI_") else "")
        )
    notes = list(spec.notes)
    missing = [x for x in measures if all(r[x] is None for r in rows)]
    if missing:
        notes.append(f"Sin datos de {', '.join(missing)} para esta categoría (INDEC no los publica a este "
                     f"nivel de detalle). Probá con la división o el nivel general.")
    for r in rows:
        r["periodo"] = format_period(r["periodo"])
        if spec.has_observaciones and not r.get("observaciones"):
            r.pop("observaciones", None)
    return {
        "indice": spec.title,
        "categoria": cat["descripcion"],
        **({"nivel": cat[spec.level_col]} if spec.level_col else {}),
        **({"codigo": cat[spec.code_col]} if spec.code_col else {}),
        "medidas": {x: spec.measures[x].label for x in measures},
        "rows": rows,
        "notas": notes,
        "source": f"{SOURCE} Dataset {spec.table}.",
    }
