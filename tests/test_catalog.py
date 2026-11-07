from indec_mcp.catalog import column_name, load_snapshot, parse_spec, table_name

from .conftest import make_spec


def test_real_snapshot_parses_all_datasets_with_primary_keys():
    datasets = parse_spec(load_snapshot())
    assert len(datasets) == 19
    by_table = {d.table: d for d in datasets}
    assert by_table["canasta_crianza"].primary_key == ("Periodo",)
    assert by_table["pobreza_eph"].primary_key == ("area_geografica_nom", "periodo")
    assert len(by_table["ipc_nacional"].primary_key) == 12
    assert all(d.primary_key for d in datasets)


def test_names_are_normalized():
    assert table_name("/estimador-mensual-de-actividad-economica-sectores") == (
        "estimador_mensual_de_actividad_economica_sectores"
    )
    assert column_name("codigo_CLaNAE_2004") == "codigo_clanae_2004"


def test_column_docs_are_cleaned():
    (ds,) = parse_spec(make_spec({"/x": ["Anio"]}))
    assert ds.column_docs["Anio"] == "Año de referencia"
    assert ds.description == "Description /x"
