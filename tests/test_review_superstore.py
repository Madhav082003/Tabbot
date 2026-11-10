"""The four fixes from the Superstore review (Q7), each on a small workbook built here:

1. each data source is read from the package file it is linked to (Excel/CSV loaded; Local Data/ = geocoding);
2. action filters are tied to their action through user:ui-action-filter; leftovers have no effect;
3. a filter that keeps every value has no practical effect; dashboards get a sheet-by-sheet filter comparison;
4. a date truncation is a timeline, a date part combines years.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pandas as pd
import pytest

from tabbot.analysis.lints import filter_table, l3_copied_filter, l15_dashboard_filters_differ
from tabbot.config import load_settings
from tabbot.llm.tools import ToolContext, ToolError, _pick_datasource, t_describe_sheet, t_list_dashboards
from tabbot.loader import load_workbook
from tabbot.tparse import describe_filter, parse_workbook
from tabbot.tparse.describe import describe_action
from tabbot.tparse.fieldref import parse_ref
from tabbot.twb.fieldref import parse_ref as engine_ref
from tabbot.twb.from_kit import to_engine

NS = 'xmlns:user="http://www.tableausoftware.com/xml/user"'

ORDERS_DS = """
<datasource caption='Shop' inline='true' name='federated.shop'>
  <connection class='federated'>
    <named-connections><named-connection caption='Shop' name='excel-direct.1'>
      <connection class='excel-direct' filename='C:/Users/someone/Desktop/Shop Data.xlsx'/></named-connection>
    </named-connections>
    <relation type='collection'>
      <relation connection='excel-direct.1' name='Orders' table='[Orders$]' type='table'/>
      <relation connection='excel-direct.1' name='People' table='[People$]' type='table'/>
    </relation>
    <metadata-records>
      <metadata-record class='column'><remote-name>Order ID</remote-name><local-name>[Order ID]</local-name><parent-name>[Orders]</parent-name><local-type>string</local-type></metadata-record>
      <metadata-record class='column'><remote-name>Order Date</remote-name><local-name>[Order Date]</local-name><parent-name>[Orders]</parent-name><local-type>date</local-type></metadata-record>
      <metadata-record class='column'><remote-name>Region</remote-name><local-name>[Region]</local-name><parent-name>[Orders]</parent-name><local-type>string</local-type></metadata-record>
      <metadata-record class='column'><remote-name>Profitable</remote-name><local-name>[Profitable]</local-name><parent-name>[Orders]</parent-name><local-type>boolean</local-type></metadata-record>
      <metadata-record class='column'><remote-name>Sales</remote-name><local-name>[Sales]</local-name><parent-name>[Orders]</parent-name><local-type>real</local-type></metadata-record>
      <metadata-record class='column'><remote-name>Person</remote-name><local-name>[Person]</local-name><parent-name>[People]</parent-name><local-type>string</local-type></metadata-record>
      <metadata-record class='column'><remote-name>Region</remote-name><local-name>[Region (People)]</local-name><parent-name>[People]</parent-name><local-type>string</local-type></metadata-record>
    </metadata-records>
  </connection>
  <column datatype='boolean' name='[Profitable]' role='dimension' type='nominal'/>
  <object-graph>
    <objects>
      <object caption='Orders' id='Orders_1'><properties context=''>
        <relation connection='excel-direct.1' name='Orders' table='[Orders$]' type='table'/></properties></object>
      <object caption='People' id='People_2'><properties context=''>
        <relation connection='excel-direct.1' name='People' table='[People$]' type='table'/></properties></object>
    </objects>
    <relationships><relationship>
      <expression op='='><expression op='[Region]'/><expression op='[Region (People)]'/></expression>
      <first-end-point object-id='Orders_1'/><second-end-point object-id='People_2'/>
    </relationship></relationships>
  </object-graph>
</datasource>
<datasource caption='Targets' inline='true' name='federated.targets'>
  <connection class='federated'>
    <named-connections><named-connection caption='Targets' name='textscan.1'>
      <connection class='textscan' directory='Data/Shop' filename='Targets.csv'/></named-connection></named-connections>
    <relation connection='textscan.1' name='Targets.csv' table='[Targets#csv]' type='table'/>
  </connection>
</datasource>
<datasource caption='Gone' inline='true' name='federated.gone'>
  <connection class='federated'>
    <named-connections><named-connection caption='Gone' name='excel-direct.9'>
      <connection class='excel-direct' filename='C:/Users/someone/Client Secret Ltd.xlsx'/></named-connection>
    </named-connections>
    <relation connection='excel-direct.9' name='Sheet1' table='[Sheet1$]' type='table'/>
  </connection>
</datasource>
"""


def sheet(name: str, rows: str = "", cols: str = "", filters: str = "", ds: str = "federated.shop") -> str:
    return f"""<worksheet name='{name}'><table><view>
      <datasources><datasource name='{ds}'/></datasources>{filters}</view>
      <panes><pane><mark class='Line'/><encodings/></pane></panes>
      <rows>{rows}</rows><cols>{cols}</cols></table></worksheet>"""


PROFIT_BOTH = """<filter class='categorical' column='[federated.shop].[none:Profitable:nk]'>
  <groupfilter function='union' user:ui-enumeration='inclusive'>
    <groupfilter function='member' level='[none:Profitable:nk]' member='false'/>
    <groupfilter function='member' level='[none:Profitable:nk]' member='true'/></groupfilter></filter>"""
SALES_RANGE = """<filter class='quantitative' column='[federated.shop].[sum:Sales:qk]' included-values='in-range'>
  <min>10</min><max>500</max></filter>"""
ACTION_LINKED = """<filter class='categorical' column='[federated.shop].[Action (Region)]'>
  <groupfilter function='level-members' level='[Region]' user:ui-action-filter='[Action1]' user:ui-enumeration='all'/>
</filter>"""
ACTION_MISSING = """<filter class='categorical' column='[federated.shop].[Action (Person)]'>
  <groupfilter function='level-members' level='[Person]' user:ui-action-filter='[Action77]' user:ui-enumeration='all'/>
</filter>"""
ACTION_UNLINKED = """<filter class='categorical' column='[federated.shop].[Action (Profitable,MONTH(Order Date))]'>
  <groupfilter function='crossjoin' user:ui-enumeration='all'>
    <groupfilter function='level-members' level='[Profitable]'/></groupfilter></filter>"""

TREND = "[federated.shop].[sum:Sales:qk]"
SHEETS = (sheet("Map", rows=TREND, cols="[federated.shop].[none:Region:nk]", filters=PROFIT_BOTH + SALES_RANGE)
          + sheet("Trend", rows=TREND, cols="[federated.shop].[tmn:Order Date:qk]",
                  filters=PROFIT_BOTH + ACTION_LINKED + ACTION_MISSING)
          + sheet("Seasonal", rows=TREND, cols="[federated.shop].[mn:Order Date:ok]", filters=ACTION_UNLINKED)
          + sheet("Targets", rows="[federated.targets].[sum:Target:qk]", ds="federated.targets")
          + sheet("Old", rows="[federated.gone].[sum:Amount:qk]", ds="federated.gone"))
DASH = """<dashboard name='Overview'><zones><zone h='100000' id='1' w='100000' x='0' y='0'>
  <zone h='50000' id='2' name='Map' w='50000' x='0' y='0'/>
  <zone h='50000' id='3' name='Trend' w='50000' x='50000' y='0'/>
  <zone h='50000' id='4' name='Seasonal' w='50000' x='0' y='50000'/></zone></zones></dashboard>"""
ACTIONS = """<actions>
  <action caption='Region Filter' name='[Action1]'>
    <activation auto-clear='true' type='on-select'/>
    <source dashboard='Overview' type='sheet' worksheet='Map'/>
    <link caption='Region Filter' expression='tsl:Overview?x=&lt;[Region]~na&gt;'/>
    <command command='tsc:tsl-filter'><param name='exclude' value='Map'/><param name='target' value='Overview'/></command>
  </action>
  <action caption='Month Highlight' name='[Action5]'>
    <activation type='on-select'/><source dashboard='Overview' type='sheet'/>
    <command command='tsc:brush'><param name='field-captions' value='MONTH(Order Date)'/>
      <param name='target' value='Overview'/></command>
  </action>
</actions>"""


def twb() -> bytes:
    return f"""<?xml version='1.0' encoding='utf-8' ?>
<workbook version='18.1' source-build='2024.3.0' {NS}>
  <datasources>{ORDERS_DS}</datasources>
  <worksheets>{SHEETS}</worksheets>
  <dashboards>{DASH}</dashboards>
  {ACTIONS}
</workbook>""".encode()


@pytest.fixture(scope="module")
def package(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("shop")
    xlsx = d / "Shop Data.xlsx"
    with pd.ExcelWriter(xlsx) as w:
        pd.DataFrame({"Order ID": ["A1", "A2", "A3", "A4"],
                      "Order Date": pd.to_datetime(["2020-01-05", "2020-02-10", "2021-01-07", "2021-03-01"]),
                      "Region": ["East", "West", "East", "South"], "Profitable": [True, False, True, True],
                      "Sales": [100.0, 50.0, 300.0, 20.0]}).to_excel(w, sheet_name="Orders", index=False)
        pd.DataFrame({"Person": ["Ann", "Bo"], "Region": ["East", "West"]}).to_excel(w, sheet_name="People",
                                                                                      index=False)
    out = d / "shop.twbx"
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("shop.twb", twb())
        z.write(xlsx, "Data/Shop/Shop Data.xlsx")
        z.writestr("Data/Shop/Targets.csv", "\ufeffRegion,Target\nEast,400\nWest,100\n")
        z.writestr("Data/Shop/Unused.csv", "a,b\n1,2\n")
        z.writestr("Local Data/GeocodingData.hyper", b"not a real hyper file")   # must never be opened
        z.writestr("Local Data/City.tds", "<datasource/>")
    return out


@pytest.fixture(scope="module")
def lw(package, tmp_path_factory):
    settings = load_settings().model_copy(update={"workspace_dir": tmp_path_factory.mktemp("ws")})
    out = load_workbook(package, settings=settings)
    yield out
    out.close(delete=True)


# ------------------------------------------------------------------ 1. data sources -> package files

def test_links_each_datasource_to_its_package_file(package):
    m = parse_workbook(package)
    links = {lk.datasource: lk for lk in m.data_links}
    assert links["federated.shop"].member == "Data/Shop/Shop Data.xlsx"          # absolute path -> by file name
    assert links["federated.shop"].match == "name"
    assert links["federated.shop"].tables == ["[Orders$]", "[People$]"]
    assert links["federated.targets"].member == "Data/Shop/Targets.csv"          # directory + filename -> path
    assert links["federated.targets"].match == "path"
    assert links["federated.gone"].member is None
    assert m.geocoding_files == ["Local Data/GeocodingData.hyper"]                # .tds is not a data file
    assert m.unlinked_data_files == ["Data/Shop/Unused.csv"]


def test_loads_excel_and_csv_and_joins_on_relationship(lw):
    shop = lw.table_for("federated.shop")
    assert shop is not None and lw.store is not None
    df = lw.store.df(f"SELECT * FROM {lw.store.view(shop)} ORDER BY \"Order ID\"")
    assert len(df) == 4                                                           # no row duplicated by the join
    assert {"Person", "Region (People)", "Sales"} <= set(df.columns)              # workbook's local names
    assert df.loc[df["Order ID"] == "A1", "Person"].item() == "Ann"
    assert pd.isna(df.loc[df["Order ID"] == "A4", "Person"].item())               # South has no person
    targets = lw.store.df(f"SELECT * FROM {lw.store.view(lw.table_for('federated.targets'))}")
    assert list(targets.columns) == ["Region", "Target"]                          # BOM stripped
    assert not any("Geocoding" in t.source_file for t in lw.tables)              # Local Data/ never read


def test_missing_file_is_named_locally_but_not_sent(lw):
    assert lw.table_for("federated.gone") is None
    assert any("Client Secret Ltd.xlsx" in x and "NOT in the package" in x for x in lw.data_files)
    shared = " ".join(lw.data_files_shared + lw.notes)
    assert "Client Secret" not in shared and "its .xlsx file is NOT in the package" in shared
    assert any("Local Data/" in x and "geocoding" in x for x in lw.data_files_shared)
    with pytest.raises(ToolError, match="Gone"):
        _pick_datasource(lw, "Gone")                                              # never falls back to other data
    assert _pick_datasource(lw, None) == "federated.shop"                         # most sheets use it


# ------------------------------------------------------------------ 2. action filters -> actions

def test_action_filters_are_linked_to_their_actions():
    m = parse_workbook(twb(), name="t")
    st = {f.field.field: (f.action_status, f.action_caption) for w in m.worksheets for f in w.filters
          if f.is_action_filter}
    assert st["Action (Region)"] == ("linked", "Region Filter")
    assert st["Action (Person)"] == ("missing", None)
    assert st["Action (Profitable,MONTH(Order Date))"] == ("unlinked", None)
    trend = m.worksheet("Trend")
    texts = [describe_filter(f) for f in trend.filters]
    assert "Action (Region): filtered by clicks via the 'Region Filter' action" in texts
    assert any("left over from a removed action (Action77), no effect" in t for t in texts)


def test_actions_described_from_the_actions_list_only():
    m = parse_workbook(twb(), name="t")
    kinds = {a.caption: a.kind for a in m.actions}
    assert kinds == {"Region Filter": "filter", "Month Highlight": "highlight"}   # tsl: link is not a URL action
    txt = {a.caption: describe_action(a) for a in m.actions}
    assert "selecting a mark in 'Map'" in txt["Region Filter"] and "filters the sheets of 'Overview'" in \
        txt["Region Filter"]
    assert "does not filter anything" in txt["Month Highlight"]


def test_tools_report_interactions_and_filter_effects(lw):
    ctx = ToolContext(lw=lw)
    dash = next(d for d in t_list_dashboards(ctx, None)["dashboards"] if d["name"] == "Overview")
    assert len(dash["interactions"]) == 2 and any("highlight" in x for x in dash["interactions"])
    from tabbot.llm.tools import SheetInput
    out = t_describe_sheet(ctx, SheetInput(sheet="Seasonal"))
    eff = {f["field"]: f.get("effect") for f in out["filters"]}
    assert "no effect" in eff["Action (Profitable,MONTH(Order Date))"]
    assert out["columns_shelf"] == ["MONTH(Order Date) (Jan–Dec, years combined) [mn]"]


# ------------------------------------------------------------------ 3. keep-everything filters, consistency

def test_filter_keeping_every_value_has_no_practical_effect():
    m = parse_workbook(twb(), name="t")
    f = next(f for f in m.worksheet("Map").filters if f.field.field == "Profitable")
    assert f.keeps_all_values
    assert describe_filter(f) == "Profitable: keeps all non-null values (false, true) - no practical effect"
    eng = to_engine(m)
    assert not l3_copied_filter(eng)                       # not reported as a copied filter


def test_dashboard_filter_table_and_differences():
    eng = to_engine(parse_workbook(twb(), name="t"))
    table = filter_table(eng, "Overview")
    assert list(table) == ["SUM(Sales)"]                   # action filters and keep-all filters are not listed
    assert table["SUM(Sales)"] == {"Map": "between 10 and 500", "Trend": "-", "Seasonal": "-"}
    (f,) = l15_dashboard_filters_differ(eng)
    assert f.code == "L15" and f.severity == "info"
    assert "Trend, Seasonal have no such filter, so their numbers include what Map leaves out" in f.message


# ------------------------------------------------------------------ 4. date truncation vs date part

def test_truncation_is_a_timeline_and_part_combines_years():
    assert parse_ref("[ds].[tmn:Order Date:qk]").label() == "Order Date by month (timeline)"
    assert parse_ref("[ds].[mn:Order Date:ok]").label() == "MONTH(Order Date) (Jan–Dec, years combined)"
    assert parse_ref("[ds].[tqr:Order Date:qk]").label() == "Order Date by quarter (timeline)"
    assert parse_ref("[ds].[yr:Order Date:ok]").label() == "YEAR(Order Date)"
    assert engine_ref("[ds].[tmn:Order Date:qk]").label == "Order Date by month (timeline)"
    assert engine_ref("[ds].[mn:Order Date:ok]").label == "MONTH(Order Date) (Jan–Dec, years combined)"
    # next to YEAR of the same field, a quarter is a quarter of that year - not combined
    m = parse_workbook(twb().replace(b"[federated.shop].[mn:Order Date:ok]",
                                     b"([federated.shop].[yr:Order Date:ok] / [federated.shop].[qr:Order Date:ok])"),
                       name="t")
    assert m.worksheet("Seasonal").cols.describe() == "YEAR(Order Date) › QUARTER(Order Date)"


def test_no_value_questions_without_data(fixture_twbx, tmp_path):
    """A bare .twb (or a .twbx without its data) cannot answer value questions, so none are suggested."""
    import zipfile

    from tabbot.config import load_settings
    from tabbot.explain.report import explain_workbook
    from tabbot.loader import load_workbook

    with zipfile.ZipFile(fixture_twbx) as z:
        name = next(n for n in z.namelist() if n.endswith(".twb"))
        twb = tmp_path / "bare.twb"
        twb.write_bytes(z.read(name))
    s = load_settings().model_copy(update={"workspace_dir": tmp_path / "ws"})
    lw = load_workbook(twb, settings=s)
    try:
        assert lw.store is None or not lw.ds_tables
        rep = explain_workbook(lw)
        qs = [q for v in rep.dashboards for q in v.questions]
        assert qs and not any("stand" in q or "changed" in q for q in qs)
        assert any("fields and calculations" in q for q in qs)
    finally:
        lw.close(delete=True)
