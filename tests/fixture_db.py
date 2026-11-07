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

    def months(first: str, last: str) -> list[str]:
        return [f"{y}{m:02d}" for y in range(2024, 2027) for m in range(1, 13) if first <= f"{y}{m:02d}" <= last]

    # EMAE: monthly to 2026-08 (one month behind IPC, like the real data).
    con.execute("""CREATE TABLE estimador_mensual_de_actividad_economica_nivel_general (
        periodo VARCHAR, tipo_de_indicador_descripcion VARCHAR, valor DOUBLE, observaciones VARCHAR)""")
    emae = []
    for i, per in enumerate(months("202501", "202608")):
        last = per == "202608"
        emae += [
            (per, "Índice serie desestacionalizada", 150.0 + i, None),
            (per, "Var % mensual serie desestacionalizada", 0.066 if last else 0.5, None),
            (per, "Var % interanual serie original", -1.744 if last else 2.0, None),
        ]
    con.executemany("INSERT INTO estimador_mensual_de_actividad_economica_nivel_general VALUES (?,?,?,?)", emae)

    # Wages: monthly to 2026-08. Code 121 has no index level, like the real public subsectors.
    con.execute("""CREATE TABLE indice_salarios (periodo VARCHAR, nivel VARCHAR, codigo VARCHAR, descripcion VARCHAR,
        tipo_de_indicador VARCHAR, valor DOUBLE, observaciones VARCHAR)""")
    sal = []
    for i, per in enumerate(months("202501", "202608")):
        last = per == "202608"
        sal += [
            (per, "0", "0", "Nivel general", "Indice", 2000.0 + 40 * i, None),
            (per, "0", "0", "Nivel general", "v_m", 2.73 if last else 2.0, None),
            (per, "0", "0", "Nivel general", "v_ia", 35.61 if last else 30.0, None),
            (per, "1", "100", "Total registrado", "Indice", 2000.0 + 20 * i, None),
            (per, "1", "100", "Total registrado", "v_m", 9.9 if last else 1.0, None),
            (per, "1", "200", "Total no registrado", "v_ia", 55.36, None),
            (per, "3", "121", "Subsector público nacional", "v_m", 1.5, None),
        ]
    con.executemany("INSERT INTO indice_salarios VALUES (?,?,?,?,?,?,?)", sal)

    # Baskets (GBA): monthly to 2026-09, per adult equivalent and for the three household types.
    con.execute("""CREATE TABLE valorizacion_canastas_cba_cbt (periodo VARCHAR, codigo VARCHAR, descripcion VARCHAR,
        tipo_de_indicador VARCHAR, valor DOUBLE)""")
    can = []
    for i, per in enumerate(months("202501", "202609")):
        last = per == "202609"
        cbt = 528318.67 if last else 400000.0 + 5000 * i
        cba = 239058.22 if last else 180000.0 + 2000 * i
        can += [
            (per, "CBT", "Canasta Basica Total", "CBT", cbt),
            (per, "CBT", "Canasta Basica Total", "v_m", 1.7),
            (per, "CBT", "Canasta Basica Total", "v_ia", 38.7 if last else 35.0),
            (per, "CBA", "Canasta Basica Alimentaria", "CBA", cba),
            (per, "CBA", "Canasta Basica Alimentaria", "v_ia", 40.0),
            (per, "CBT H1", "Canasta Basica Total, Hogar 1", "Hogar 1", round(cbt * 2.46, 2)),
            (per, "CBT H2", "Canasta Basica Total, Hogar 2", "Hogar 2", 1632504.69 if last else round(cbt * 3.09, 2)),
            (per, "CBT H3", "Canasta Basica Total, Hogar 3", "Hogar 3", round(cbt * 3.25, 2)),
        ]
    con.executemany("INSERT INTO valorizacion_canastas_cba_cbt VALUES (?,?,?,?,?)", can)
    con.execute("""CREATE TABLE canasta_crianza AS SELECT * FROM (VALUES
        (2026, 8, '202608', 1.0, 2.0, 3.0, 10.0, 20.0, 30.0, 4.0, 5.0, 9.0, 6.0, 7.0, 13.0),
        (2026, 9, '202609', 184911.53, 385920.04, 570831.58, 238764.09, 441051.48, 679815.57,
         304094.69, 275657.17, 579751.87, 377230.63, 351618.96, 728849.59))
        t(anio, mes, periodo, bys_men1, cuid_men1, tot_men1, bys_1a3, cuid_1a3, tot_1a3,
          bys_4a5, cuid_4a5, tot_4a5, bys_6a12, cuid_6a12, tot_6a12)""")
    con.execute("""CREATE TABLE pobreza_eph AS
        SELECT * FROM (VALUES ('T_31_A', '20261S', 24.4, 32.3, 5.6, 7.5, 'Dato provisorio', CAST(NULL AS VARCHAR)),
                              ('T_31_A', '20252S', 21.0, 28.2, 4.8, 6.3, NULL, NULL),
                              ('T_31_A', '20162S', 21.5, 30.3, 4.5, 6.1, NULL, NULL))
        t(area_geografica_nom, periodo, tasa_pobreza_hogares, tasa_pobreza_personas, tasa_indigencia_hogares,
          tasa_indigencia_personas, observaciones, tasa_pobreza_personas_null)""")
    con.execute("""CREATE TABLE mercado_trabajo_eph AS
        SELECT * FROM (VALUES ('T_31_A', '20262T', 48.9, 45.0, 24.7, 20.2, 7.9, 11.5, 16.9, 'Dato provisorio'),
                              ('T_31_A', '20261T', 48.6, 44.8, 24.9, 19.8, 7.8, 11.1, 15.8, NULL))
        t(area_geografica_nom, periodo, tasa_de_actividad, tasa_de_empleo, tasa_de_empleo_formal,
          tasa_de_empleo_informal, tasa_de_desocupacion, tasa_de_subocupacion, tasa_de_ocupacion_demandante,
          observaciones)""")
    # EMAE by sector: original series only (index and y/y change).
    con.execute("""CREATE TABLE estimador_mensual_de_actividad_economica_sectores AS SELECT * FROM (VALUES
        ('202608', 'F', 'Construcción', 'Variación porcentual interanual', -2.55, CAST(NULL AS VARCHAR)),
        ('202608', 'F', 'Construcción', 'Índice base 2004=100', 140.2, NULL),
        ('202608', 'G', 'Comercio mayorista, minorista y reparaciones', 'Variación porcentual interanual', -3.5, NULL))
        t(periodo, codigo, descripcion, tipo_de_indicador, valor, observaciones)""")

    # Foreign trade: dictionary + exports/imports with confidential ('s') rows, like the real data.
    con.execute("""CREATE TABLE comercio_exterior_nomenclaturas AS SELECT * FROM (VALUES
        ('pais_cod', 'BR', 'Brasil'), ('pais_cod', 'CN', 'China'),
        ('pais_cod', 'HK', 'Hong Kong Región Administrativa Especial de (China)'),
        ('pais_cod', 'US', 'Estados Unidos'),
        ('rubro_cod', '1', 'PRODUCTOS PRIMARIOS'), ('rubro_cod', '2', 'MANUFACTURAS DE ORIGEN AGROPECUARIO (MOA)'),
        ('subrubro_cod', '299', 'Resto de MOA'), ('subrubro_cod', '201', 'Carnes y sus preparados'),
        ('subrubro_cod', '101', 'Cereales'),
        ('uso_economico_cod', '1', 'Bienes de Capital'), ('gce_cod', '410', 'Bienes de capital (excepto transporte)'))
        t(nombre_columna, codigo, codigo_descripcion)""")
    con.execute("""CREATE TABLE comercio_exterior_exportacion_bienes (anio BIGINT, mes BIGINT, periodo VARCHAR,
        rubro_cod VARCHAR, subrubro_cod VARCHAR, pais_cod VARCHAR, valor_fob DOUBLE, valor_fob_null VARCHAR,
        peso_neto DOUBLE, peso_neto_null VARCHAR)""")
    con.execute("""CREATE TABLE comercio_exterior_importacion_bienes (anio BIGINT, mes BIGINT, periodo VARCHAR,
        uso_economico_cod VARCHAR, gce_cod VARCHAR, pais_cod VARCHAR, valor_cif DOUBLE, valor_cif_null VARCHAR,
        peso_neto DOUBLE, peso_neto_null VARCHAR)""")
    expo, impo = [], []
    for per in months("202501", "202609"):
        y, mth = int(per[:4]), int(per[4:])
        expo += [
            (y, mth, per, "2", "201", "BR", 100e6, None, 50e6, None),
            (y, mth, per, "1", "101", "CN", 200e6, None, 400e6, None),
            (y, mth, per, "2", "299", "CN", None, "s", None, "s"),  # confidential
            (y, mth, per, "1", "101", "HK", 1e6, None, 1e6, None),
        ]
        impo += [(y, mth, per, "1", "410", "US", 30e6, None, 2e6, None)]
    con.executemany("INSERT INTO comercio_exterior_exportacion_bienes VALUES (?,?,?,?,?,?,?,?,?,?)", expo)
    con.executemany("INSERT INTO comercio_exterior_importacion_bienes VALUES (?,?,?,?,?,?,?,?,?,?)", impo)

    # Population: national (single ages), provinces (age groups), departments ('San Martín' twice).
    con.execute("""CREATE TABLE proyecciones_poblacion_nacional_2022_2040 (edad BIGINT, sexo_cod VARCHAR,
        sexo_nom VARCHAR, poblacion BIGINT, poblacion_null VARCHAR, fecha BIGINT)""")
    con.executemany(
        "INSERT INTO proyecciones_poblacion_nacional_2022_2040 VALUES (?,?,?,?,NULL,?)",
        [(age, sc, sn, 1000 + age, year) for year in (2026, 2030) for age in (0, 4, 5, 100)
         for sc, sn in (("1", "Mujer"), ("2", "Varon"))],
    )
    con.execute("""CREATE TABLE proyecciones_poblacion_jurisdiccion_2022_2040 (jurisdiccion_cod VARCHAR,
        jurisdiccion_nom VARCHAR, sexo_cod VARCHAR, sexo_nom VARCHAR, edad_cod VARCHAR, edad_nom VARCHAR,
        poblacion BIGINT, poblacion_null VARCHAR, fecha VARCHAR)""")
    con.executemany(
        "INSERT INTO proyecciones_poblacion_jurisdiccion_2022_2040 VALUES (?,?,?,?,?,?,?,NULL,?)",
        [(jc, jn, sc, sn, ec, en, 500, y) for y in ("2026",)
         for jc, jn in (("02", "Ciudad Autónoma de Buenos Aires"), ("14", "Córdoba"))
         for sc, sn in (("1", "Mujer"), ("2", "Varon"))
         for ec, en in (("0", "0 a 4 años"), ("10", "10 a 14 años"), ("5", "5 a 9 años"))],
    )
    con.execute("""CREATE TABLE estimaciones_poblacion_departamento_2022_2035 (jurisdiccion_cod VARCHAR,
        jurisdiccion_nom VARCHAR, departamento_cod VARCHAR, departamento_nom VARCHAR, rmba VARCHAR,
        sexo_cod VARCHAR, sexo_nom VARCHAR, poblacion BIGINT, poblacion_null VARCHAR, fecha VARCHAR)""")
    con.executemany(
        "INSERT INTO estimaciones_poblacion_departamento_2022_2035 VALUES (?,?,?,?,'0',?,?,?,NULL,'2026')",
        [(jc, jn, dc, dn, sc, sn, pop)
         for jc, jn, dc, dn, pop in (("14", "Córdoba", "14014", "Capital", 700), ("14", "Córdoba", "14091", "San Martín", 60),
                                     ("50", "Mendoza", "50070", "San Martín", 70))
         for sc, sn in (("1", "Mujer"), ("2", "Varon"))],
    )

    # Wholesale prices: one row per index (IPIM/IPIB/IPP); 'Productos importados' exists as Origen and Sector.
    con.execute("""CREATE TABLE sipm_precios_mayoristas (operativo VARCHAR, indicador VARCHAR, clasificador VARCHAR,
        nivel VARCHAR, codigo_apertura VARCHAR, descripcion VARCHAR, periodo VARCHAR, tipo_de_indicador VARCHAR,
        region VARCHAR, valor DOUBLE, observaciones VARCHAR)""")
    sipm = []
    cats = [("SIPM 93", "Nivel general", "NG", "Nivel general"), ("SIPM 93", "Origen", "N", "Productos nacionales"),
            ("SIPM 93", "Origen", "I", "Productos importados"), ("SIPM 93", "Sector", "3", "Productos importados"),
            ("CIIU3", "Clase", "0111", "Cereales y oleaginosas")]
    for i, per in enumerate(months("202501", "202609")):
        obs = "* Dato provisorio" if per == "202609" else None
        for ind, bump in (("IPIM", 0.0), ("IPIB", 0.1), ("IPP", 0.2)):
            for clas, niv, cod, desc in cats:
                for tipo, val in (("v_m", 2.0 + bump), ("v_ia", 30.0 + bump), ("v_acum", 20.0), ("Indice", 1000.0 + 10 * i)):
                    sipm.append(("SIPM", ind, clas, niv, cod, desc, per, tipo, "Total del país", val, obs))
    con.executemany("INSERT INTO sipm_precios_mayoristas VALUES (?,?,?,?,?,?,?,?,?,?,?)", sipm)

    con.execute("""CREATE TABLE icc_precios_construccion (indicador VARCHAR, clasificador VARCHAR, nivel VARCHAR,
        codigo_apertura VARCHAR, descripcion VARCHAR, periodo VARCHAR, tipo_de_indicador VARCHAR, region VARCHAR,
        valor DOUBLE, observaciones VARCHAR)""")
    icc = []
    for i, per in enumerate(months("202501", "202609")):
        for niv, cod, desc, vm in (("Nivel general", "0", "Nivel general", 1.5), ("Capitulo", "02", "Mano de obra", 1.6),
                                   ("Ítems", "IT-12", "Pintura", 0.5)):
            for tipo, val in (("v_m", vm), ("v_ia", 31.0), ("v_acum", 25.0), ("Indice", 500.0 + 5 * i)):
                icc.append(("ICC", "ICC 93", niv, cod, desc, per, tipo, "GBA", val, None))
    con.executemany("INSERT INTO icc_precios_construccion VALUES (?,?,?,?,?,?,?,?,?,?)", icc)

    # Industrial production: original series for every branch; seasonally adjusted only for 2-digit divisions.
    con.execute("""CREATE TABLE ipi_manufacturero (periodo VARCHAR, indicador VARCHAR, cuadro_web VARCHAR,
        codigo_clanae_2004 VARCHAR, descripcion VARCHAR, serie VARCHAR, tipo_de_indicador VARCHAR, valor DOUBLE)""")
    ipi = []
    for i, per in enumerate(months("202501", "202609")):
        for cod, desc, division in (("D", "IPI Manufacturero - Nivel general", True), ("15", "Alimentos y bebidas", True),
                                    ("15521", "Vino", False)):
            ipi += [(per, "IPI manufacturero", "Cuadro 2", cod, desc, "Serie original", "Índice", 120.0 + i),
                    (per, "IPI manufacturero", "Cuadro 3", cod, desc, "Serie original", "Variación interanual", -1.7801)]
            if division:
                ipi += [(per, "IPI manufacturero", "Cuadro 5", cod, desc, "Serie desestacionalizada", "Variación intermensual", 0.7314),
                        (per, "IPI manufacturero", "Cuadro 5", cod, desc, "Serie desestacionalizada", "Índice", 115.0 + i)]
    con.executemany("INSERT INTO ipi_manufacturero VALUES (?,?,?,?,?,?,?,?)", ipi)
    con.execute("""CREATE TABLE ipi_minero (periodo VARCHAR, indicador VARCHAR, cuadro_web VARCHAR,
        codigo_clanae_2010 VARCHAR, codigo_cpc_2_1 VARCHAR, descripcion VARCHAR, serie VARCHAR,
        tipo_de_indicador VARCHAR, valor DOUBLE)""")
    mine = []
    for i, per in enumerate(months("202501", "202609")):
        mine += [(per, "IPI Minero", "Cuadro 2", "B", "-", "Nivel General - IPI Minero", "Serie Original", "Índice", 140.0 + i),
                 (per, "IPI Minero", "Cuadro 3", "B", "-", "Nivel General - IPI Minero", "Serie Original", "Variación interanual", 7.13),
                 (per, "IPI Minero", "Cuadro 1", "B", "-", "Nivel General - IPI Minero", "Serie Desestacionalizada", "Variación intermensual", -0.09),
                 (per, "IPI Minero", "Cuadro 1", "B", "-", "Nivel General - IPI Minero", "Serie Desestacionalizada", "Índice", 146.0 + i),
                 (per, "IPI Minero", "Cuadro 3", "06.100", "12010", "Petróleo crudo", "Serie Original", "Variación interanual", 15.57)]
    con.executemany("INSERT INTO ipi_minero VALUES (?,?,?,?,?,?,?,?,?)", mine)

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
