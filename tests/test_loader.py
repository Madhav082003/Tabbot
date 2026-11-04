"""Data side of S2: extract -> Parquet -> DuckDB, profiling, entity and date detection."""

import pytest

from tabbot.cli import main
from tabbot.config import load_settings
from tabbot.loader import load_workbook


@pytest.fixture(scope="module")
def lw(fixture_twbx, tmp_path_factory):
    settings = load_settings().model_copy(update={"workspace_dir": tmp_path_factory.mktemp("ws")})
    loaded = load_workbook(fixture_twbx, settings=settings)
    yield loaded
    loaded.close()


def test_tables_and_rows(lw, expected):
    assert [t.name for t in lw.tables] == [expected["data"]["table"]]
    assert lw.tables[0].rows == expected["data"]["rows"]
    assert len(lw.tables[0].columns) == expected["data"]["columns"]
    view = lw.store.view()
    assert lw.store.scalar(f'SELECT COUNT(DISTINCT "RECORD_ID") FROM {view}') == expected["data"]["entities"]


def test_entity_detected_from_workbook(lw, expected):
    prof = lw.profile_for(expected["workbook"]["datasource"])
    e = prof.entity
    assert e.field == expected["data"]["entity"] and e.method == "workbook_countd"
    assert e.distinct == expected["data"]["entities"]
    assert e.rows_per_entity == pytest.approx(expected["data"]["rows"] / expected["data"]["entities"], abs=0.01)


def test_column_kinds(lw, expected):
    prof = lw.profile_for(None)
    kinds = {c.name: c.kind for c in prof.columns}
    assert [n for n, k in kinds.items() if k == "free_text"] == expected["data"]["free_text_columns"]
    assert [n for n, k in kinds.items() if k == "id"] == expected["data"]["id_columns"]
    d = prof.column(expected["data"]["date_field"])
    assert d.kind == "text_date" and d.date_format.format == expected["data"]["date_format"]
    assert d.date_format.parsed_share == 1.0 and d.date_format.ambiguous_with is None
    assert kinds["CATEGORY"] == "categorical" and kinds["SUBCATEGORY"] == "high_cardinality"
    assert set(expected["data"]["free_text_columns"]) <= set(prof.sensitive_suggestions)


def test_entity_level_columns(lw):
    prof = lw.profile_for(None)
    assert prof.column("CHANNEL").entity_level is True
    assert prof.column("RECEIVED_DATE").entity_level is True
    assert prof.column("CATEGORY").entity_level is False and prof.column("CATEGORY").entities_varying > 0


def test_ambiguous_date_format_flagged(tmp_path):
    import duckdb  # noqa: F401
    import pyarrow as pa
    import pyarrow.parquet as pq

    from tabbot.data.hyper_reader import TableInfo
    from tabbot.data.profile import profile_table
    from tabbot.data.store import Store

    p = tmp_path / "t.parquet"
    pq.write_table(pa.table({"D": ["01/02/2021", "03/04/2021", "05/06/2022"] * 10}), p)
    store = Store([TableInfo(name='"T"', schema_name="", table="T", source_file="x", parquet=str(p), rows=30,
                             columns=[("D", "TEXT")])])
    col = profile_table(store, '"T"').column("D")
    assert col.kind == "text_date" and col.date_format.ambiguous_with is not None


def test_cli_inspect(fixture_twbx, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    from tabbot import config

    config.get_settings.cache_clear()
    assert main(["inspect", str(fixture_twbx)]) == 0
    out = capsys.readouterr().out
    assert "entity to count: RECORD_ID" in out
    assert "Category by Quarter" in out and "UNUSED" in out
    assert "%d-%b-%Y" in out
    config.get_settings.cache_clear()
