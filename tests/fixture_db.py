"""Build a tiny DuckDB mirror with the same shapes as the real one (see docs/api-research.md)."""

from __future__ import annotations

import functools
import shutil
import tempfile
from pathlib import Path

import duckdb

from indec_mcp.sync import _META_DDL


def build(path: Path) -> Path:
    """Copy of a fixture mirror at `path` (built once per test session, then copied)."""
    shutil.copyfile(_template(), path)
    return path


@functools.cache
def _template() -> Path:
    path = Path(tempfile.mkdtemp(prefix="indec-fixture-")) / "template.duckdb"
    con = duckdb.connect(str(path))
    con.execute(_META_DDL)

    con.execute("""
        CREATE TABLE ipc_nacional (clasificador VARCHAR, nivel VARCHAR, codigo VARCHAR,
            descripcion VARCHAR, periodo VARCHAR, region VARCHAR, tipo_de_indicador VARCHAR,
            valor DOUBLE, valor_null VARCHAR, observaciones VARCHAR)
    """)
    cats = [
        ("IPC", "Nivel General", "0", "Nivel general", ["Nacional", "GBA"]),
        ("Estacional, Núcleo y Regulados", "Categoría", "N", "Núcleo", ["Nacional", "GBA"]),
        ("COICOP Argentina 2019", "División", "01", "Alimentos y bebidas no alcohólicas", ["Nacional", "GBA"]),
        ("COICOP Argentina 2019", "División", "12", "Bienes y servicios varios", ["Nacional", "GBA"]),
        # Grupo detail exists only for regions, like the real data.
        ("COICOP Argentina 2019", "Grupo", "01.1", "Alimentos", ["GBA"]),
        ("COICOP Argentina 2019", "Grupo", "05.6", "Bienes y servicios para la conservación del hogar", ["GBA"]),
    ]
    rows = []
    months = [f"{y}{m:02d}" for y in (2025, 2026) for m in range(1, 13) if f"{y}{m:02d}" <= "202609"]
    for i, per in enumerate(months):
        for clas, nivel, cod, desc, regions in cats:
            for reg in regions:
                obs = "Dato provisorio" if per == "202609" else None
                rows += [
                    (clas, nivel, cod, desc, per, reg, "v_m", 2.0 + i / 100, None, obs),
                    (clas, nivel, cod, desc, per, reg, "v_ia", 30.0 + i, None, obs),
                    (clas, nivel, cod, desc, per, reg, "v_acum", 10.0 + i, None, obs),
                    (clas, nivel, cod, desc, per, reg, "Indice", 1000.0 + i, None, obs),
                ]
    con.executemany("INSERT INTO ipc_nacional VALUES (?,?,?,?,?,?,?,?,?,?)", rows)

    con.execute("""CREATE TABLE estimador_mensual_de_actividad_economica_nivel_general AS
        SELECT * FROM (VALUES ('202608', 'Var % interanual serie original', -1.744),
                              ('202608', 'Var % mensual serie desestacionalizada', 0.066),
                              ('202607', 'Var % interanual serie original', 2.0))
        t(periodo, tipo_de_indicador_descripcion, valor)""")
    con.execute("""CREATE TABLE indice_salarios AS
        SELECT * FROM (VALUES ('202608', '0', 'v_m', 2.73), ('202608', '0', 'v_ia', 35.61),
                              ('202608', '100', 'v_m', 9.9))
        t(periodo, codigo, tipo_de_indicador, valor)""")
    con.execute("""CREATE TABLE valorizacion_canastas_cba_cbt AS
        SELECT * FROM (VALUES
            ('202609', 'Canasta Basica Alimentaria', 'CBA', 239058.22),
            ('202609', 'Canasta Basica Total', 'CBT', 528318.67),
            ('202609', 'Canasta Basica Total', 'v_ia', 38.7),
            ('202609', 'Canasta Basica Total, Hogar 2', 'Hogar 2', 1632504.69))
        t(periodo, descripcion, tipo_de_indicador, valor)""")
    con.execute("""CREATE TABLE pobreza_eph AS
        SELECT * FROM (VALUES ('T_31_A', '20261S', 24.4, 32.3, 7.5, 'Dato provisorio', CAST(NULL AS VARCHAR)),
                              ('T_31_A', '20252S', 21.0, 28.2, 6.3, NULL, NULL))
        t(area_geografica_nom, periodo, tasa_pobreza_hogares, tasa_pobreza_personas,
          tasa_indigencia_personas, observaciones, tasa_pobreza_personas_null)""")
    con.execute("""CREATE TABLE mercado_trabajo_eph AS
        SELECT * FROM (VALUES ('T_31_A', '20262T', 48.9, 45.0, 20.2, 7.9, 'Dato provisorio'))
        t(area_geografica_nom, periodo, tasa_de_actividad, tasa_de_empleo, tasa_de_empleo_informal,
          tasa_de_desocupacion, observaciones)""")

    for table, summary in [
        ("ipc_nacional", "Índice de Precios al Consumidor (IPC)"),
        ("estimador_mensual_de_actividad_economica_nivel_general", "EMAE nivel general"),
        ("indice_salarios", "Índice de salarios"),
        ("valorizacion_canastas_cba_cbt", "Canastas básicas"),
        ("pobreza_eph", "Pobreza"),
        ("mercado_trabajo_eph", "Mercado de trabajo"),
    ]:
        n = con.execute(f"SELECT count(*), max(periodo) FROM {table}").fetchone()
        con.execute(
            "INSERT INTO _datasets VALUES (?, ?, ?, 'desc', 'Dir', ['periodo'], ?, ?, ?, "
            "TIMESTAMPTZ '2026-11-07 13:36:00-03', 'ok', NULL)",
            [table, "/" + table, summary, n[0], n[0], n[1]],
        )
        for name, dtype in con.execute(
            "SELECT column_name, data_type FROM duckdb_columns() WHERE table_name = ?", [table]
        ).fetchall():
            con.execute("INSERT INTO _columns VALUES (?, ?, ?, ?, ?)", [table, name, name, dtype, f"doc {name}"])
    con.close()
    return path
