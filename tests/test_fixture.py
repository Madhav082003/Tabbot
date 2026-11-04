"""The synthetic workbook is valid and contains everything expected.json promises."""

import zipfile

import pandas as pd
from defusedxml import ElementTree as ET
from tableauhyperapi import Connection, HyperProcess, Telemetry

TWB = "fixture.twb"
HYPER = "Data/fixture.twb Files/federated.hyper"


def test_zip_members(fixture_twbx):
    with zipfile.ZipFile(fixture_twbx) as z:
        assert sorted(z.namelist()) == sorted([TWB, HYPER])


def _root(fixture_twbx):
    with zipfile.ZipFile(fixture_twbx) as z:
        return ET.fromstring(z.read(TWB))


def test_twb_parses_and_matches_expected(fixture_twbx, expected):
    root = _root(fixture_twbx)
    wb = expected["workbook"]
    assert root.tag == "workbook" and root.get("version") == "18.1"

    ds = root.find("datasources").find("datasource")
    assert ds.get("name") == wb["datasource"]
    calcs = {c.get("name").strip("[]"): c.get("caption") for c in ds.findall("column")
             if c.find("calculation") is not None}
    assert calcs == wb["calc_captions"]
    # physical fields are listed twice (source connection + extract), as in real workbooks
    assert len(ds.findall(".//metadata-record[@class='column']")) == 2 * wb["physical_fields"]

    sheets = root.find("worksheets").findall("worksheet")
    assert len(sheets) == wb["worksheets"]
    marks = {w.get("name"): w.find(".//pane/mark").get("class") for w in sheets}
    assert marks == wb["mark_by_sheet"]

    for db in root.find("dashboards").findall("dashboard"):
        names = [z.get("name") for z in db.iter("zone") if z.get("name") and not z.get("type-v2")]
        # phone layout repeats every sheet zone once
        assert names == wb["dashboards"][db.get("name")] * 2
        assert db.find(".//devicelayout") is not None

    hidden = sorted(w.get("name") for w in root.find("windows")
                    if w.get("class") == "worksheet" and w.get("hidden") == "true")
    assert hidden == wb["hidden_worksheets"]
    assert len(root.find("thumbnails")) == wb["thumbnails"]


def test_unused_calcs_really_unused(fixture_twbx, expected):
    root = _root(fixture_twbx)
    ds = root.find("datasources").find("datasource")
    calcs = {c.get("name").strip("[]"): c.get("caption") for c in ds.findall("column")
             if c.find("calculation") is not None}
    shelves = " ".join((w.findtext(".//rows") or "") + (w.findtext(".//cols") or "")
                       + " ".join(e.get("column") or "" for e in w.iter() if e.tag in ("color", "size", "text"))
                       for w in root.find("worksheets"))
    unused = sorted(cap for name, cap in calcs.items() if name not in shelves)
    assert unused == expected["workbook"]["unused_calcs"]


def test_hyper_rows_and_story(fixture_twbx, expected, tmp_path):
    with zipfile.ZipFile(fixture_twbx) as z:
        z.extract(HYPER, tmp_path)
    hyper = tmp_path / HYPER
    with HyperProcess(Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU, parameters={"log_dir": str(tmp_path)}) as hp:
        with Connection(hp.endpoint, str(hyper)) as c:
            assert c.execute_scalar_query('SELECT COUNT(*) FROM "Extract"."Extract"') == expected["data"]["rows"]
            rows = c.execute_list_query(
                'SELECT "RECORD_ID", "RECEIVED_DATE", "CATEGORY", "CHANNEL" FROM "Extract"."Extract"')
    df = pd.DataFrame(rows, columns=["id", "d", "cat", "ch"])
    df["date"] = pd.to_datetime(df["d"], format=expected["data"]["date_format"])
    assert df["id"].nunique() == expected["data"]["entities"]
    assert str(df["date"].max().date()) == expected["data"]["date_max"]

    story = expected["story"]
    q = df["date"].dt.to_period("Q").astype(str)
    cat = df[df["cat"] == story["surge_category"]]
    last = cat[q[cat.index].isin(story["last3_full_quarters"])]["id"].nunique()
    prev = cat[q[cat.index].isin(story["prev3_full_quarters"])]["id"].nunique()
    assert (prev, last) == (story["surge_category_prev3"], story["surge_category_last3"])
    assert last / prev >= 2.8  # "roughly triples"
    top_driver = max(story["delta_by_channel"], key=story["delta_by_channel"].get)
    assert top_driver == story["driver_value"]


def test_rows_per_entity_and_free_text(fixture_twbx, tmp_path):
    with zipfile.ZipFile(fixture_twbx) as z:
        z.extract(HYPER, tmp_path)
    with HyperProcess(Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU, parameters={"log_dir": str(tmp_path)}) as hp:
        with Connection(hp.endpoint, str(tmp_path / HYPER)) as c:
            per = c.execute_scalar_query(
                'SELECT COUNT(*) * 1.0 / COUNT(DISTINCT "RECORD_ID") FROM "Extract"."Extract"')
            notes = c.execute_list_query(
                'SELECT AVG(LENGTH("NOTES")), COUNT(DISTINCT "NOTES") * 1.0 / COUNT(*) FROM "Extract"."Extract"')[0]
    assert per > 2  # several rows per entity -> must count distinct
    assert notes[0] > 30 and notes[1] > 0.5  # looks like free text
