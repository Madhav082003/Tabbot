"""Parser output vs the facts planted in the synthetic workbook (expected.json)."""

import zipfile

import pytest
from defusedxml import EntitiesForbidden

from tabbot.twb.parser import parse_workbook


@pytest.fixture(scope="module")
def model(fixture_twbx, tmp_path_factory):
    d = tmp_path_factory.mktemp("parse")
    with zipfile.ZipFile(fixture_twbx) as z:
        z.extract("fixture.twb", d)
    return parse_workbook(d / "fixture.twb", thumbnails_dir=d / "thumbs")


def test_datasource(model, expected):
    wb = expected["workbook"]
    assert len(model.datasources) == 1
    ds = model.datasources[0]
    assert ds.name == wb["datasource"] and ds.caption == wb["datasource_caption"]
    assert ds.connection_class == wb["connection_class"]
    assert ds.connection_file == "fixture_source.xlsx"          # basename only
    assert ds.connection_has_local_path is True                 # planted local path
    assert ds.has_extract and ds.extract_tables == [expected["data"]["table"]]
    physical = [f for f in ds.fields if not f.is_calculated]
    assert len(physical) == wb["physical_fields"]               # de-duplicated (listed twice in XML)


def test_calculations(model, expected):
    wb = expected["workbook"]
    calcs = {f.internal_name: f for f in model.calculated_fields}
    assert {k: f.caption for k, f in calcs.items()} == wb["calc_captions"]
    used = sorted(f.caption for f in calcs.values() if f.used_by_sheets)
    assert used == wb["used_calcs"]
    assert calcs["Calculation_1001"].formula_readable == "DATE([RECEIVED_DATE])"


def test_worksheets(model, expected):
    wb = expected["workbook"]
    assert {w.name: w.mark for w in model.worksheets} == wb["mark_by_sheet"]
    assert sorted(w.name for w in model.worksheets if w.hidden) == wb["hidden_worksheets"]
    heat = model.worksheet("Category Heatmap")
    assert [r.field for r in heat.rows] == ["CATEGORY"]
    assert [(r.derivation, r.caption) for r in heat.cols] == [("yr", "Parsed Date"), ("qr", "Parsed Date")]
    assert heat.encodings["color"][0].aggregation == "count_distinct"
    assert all(not w.unsupported for w in model.worksheets)


def test_filters(model, expected):
    f = model.worksheet("Category Heatmap").filters[0]
    assert (f.mode, f.member_count) == ("include", 8) and "Category A" in f.members
    f = model.worksheet("Category B").filters[0]
    assert (f.mode, f.members) == ("include", ["Category D"])
    f = model.worksheet("Dates by Subcategory").filters[0]
    assert (f.mode, f.member_count) == ("exclude", expected["planted_problems"]["large_exclude_filter"]["members"])
    f = model.worksheet("Age Group Pie").filters[0]
    assert (f.field.field, f.mode, sorted(f.members)) == ("GENDER", "include", ["Female", "Male"])


def test_dashboards(model, expected):
    wb = expected["workbook"]
    assert {d.name: d.sheets for d in model.dashboards} == wb["dashboards"]       # no phone-layout duplicates
    assert {d.name: [q.field.field for q in d.quick_filters] for d in model.dashboards} == wb["quick_filters"]
    assert all(d.has_device_layouts for d in model.dashboards)
    vt = model.dashboard("Volume Trend")
    assert len(vt.zones) == 3 and all(z.w > 0 and z.h > 0 for z in vt.zones)


def test_thumbnails_and_size(model, expected):
    assert len(model.thumbnails) == expected["workbook"]["thumbnails"]
    assert all((t.width, t.height) == (192, 192) and t.file for t in model.thumbnails)
    assert model.size_report["sections_kb"]["worksheets"] > 0
    assert model.size_report["token_estimate_rough"] > 0


def test_unknown_xml_does_not_crash(tmp_path):
    twb = tmp_path / "odd.twb"
    twb.write_text(
        "<workbook version='18.1'><datasources><datasource name='ds1'><column name='[X]' caption='X'>"
        "<calculation class='tableau' formula='{FIXED [A] : SUM([B])}'/></column></datasource></datasources>"
        "<worksheets><worksheet name='W'><table><view><datasources><datasource name='ds1'/></datasources>"
        "<filter class='categorical' column='[ds1].[none:A:nk]'><groupfilter function='filter' expression='x'/>"
        "</filter></view><panes><pane><mark class='Hexbin'/></pane></panes>"
        "<rows>[ds1].[pcto:sum:B:qk]</rows><cols/></table><table-calc/></worksheet>"
        "<worksheet name='Empty'/></worksheets><mystery-section/></workbook>", encoding="utf-8")
    m = parse_workbook(twb)
    w = m.worksheet("W")
    assert w.mark == "Hexbin"
    assert any("table calculation" in u for u in w.unsupported)
    assert any("unsupported type" in u for u in w.unsupported)
    assert any("LOD" in u for u in m.unsupported)
    assert m.worksheet("Empty").unsupported


def test_rejects_xml_entity_attack(tmp_path):
    twb = tmp_path / "evil.twb"
    twb.write_text('<?xml version="1.0"?><!DOCTYPE w [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]>'
                   "<workbook>&b;</workbook>", encoding="utf-8")
    with pytest.raises(EntitiesForbidden):  # defusedxml refuses entity declarations
        parse_workbook(twb)
