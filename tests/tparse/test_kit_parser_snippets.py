"""Parser behaviour on small workbooks assembled from XML shapes seen in public workbooks."""
from tabbot.tparse import describe_filter, describe_worksheet, parse_workbook
from tabbot.tparse.xmlutil import load_twb, text_of, unescape_member

NS = 'xmlns:user="http://www.tableausoftware.com/xml/user"'

DS = """
<datasource name='ds' caption='Cases'>
  <connection class='federated'>
    <named-connections><named-connection name='excel.1' caption='file'>
      <connection class='excel-direct' filename='C:/Users/someone/data.xlsx'/></named-connection></named-connections>
    <relation name='Sheet1' table='[Sheet1$]' type='table'/>
    <metadata-records>
      <metadata-record class='column'><remote-name>CASEID</remote-name><local-name>[CASEID]</local-name><local-type>string</local-type></metadata-record>
      <metadata-record class='column'><remote-name>SOC</remote-name><local-name>[SOC]</local-name><local-type>string</local-type></metadata-record>
      <metadata-record class='column'><remote-name>RECEIVED</remote-name><local-name>[RECEIVED]</local-name><local-type>string</local-type></metadata-record>
      <metadata-record class='column'><remote-name>GENDER</remote-name><local-name>[GENDER]</local-name><local-type>string</local-type></metadata-record>
    </metadata-records>
  </connection>
  <column caption='Parsed Date' datatype='date' name='[Calculation_1]' role='dimension' type='ordinal'>
    <calculation class='tableau' formula='DATE([RECEIVED])'/></column>
  <column caption='Unused' datatype='integer' name='[Calculation_2]' role='measure' type='quantitative'>
    <calculation class='tableau' formula='COUNTD([CASEID])'/></column>
  <group caption='Top SOC' name='[Top SOC]' name-style='unqualified' user:ui-builder='filter-group'>
    <groupfilter function='member' level='[none:SOC:nk]' member='&quot;Cardiac disorders&quot;'/></group>
</datasource>
"""


def wb(worksheets: str, dashboards: str = "", windows: str = "", extra_root: str = "") -> bytes:
    return f"""<?xml version='1.0' encoding='utf-8' ?>
<workbook version='18.1' source-build='2024.3.0' {NS} {extra_root}>
  <datasources>{DS}</datasources>
  <worksheets>{worksheets}</worksheets>
  <dashboards>{dashboards}</dashboards>
  <windows>{windows}</windows>
</workbook>""".encode()


def sheet(name: str, rows: str = "", cols: str = "", filters: str = "", mark: str = "Square", enc: str = "",
          title: str | None = None) -> str:
    t = f"<layout-options><title><formatted-text><run>{title}</run><run>Æ&#10;</run></formatted-text></title></layout-options>" if title else ""
    return f"""<worksheet name='{name}'>{t}<table><view>
      <datasources><datasource name='ds'/></datasources>{filters}</view>
      <panes><pane><mark class='{mark}'/><encodings>{enc}</encodings></pane></panes>
      <rows>{rows}</rows><cols>{cols}</cols></table></worksheet>"""


def test_heatmap_sheet_resolves_and_describes():
    x = sheet("Sheet 105", rows="[ds].[none:SOC:nk]",
              cols="([ds].[yr:Calculation_1:ok] / [ds].[qr:Calculation_1:ok])",
              enc="<color column='[ds].[ctd:CASEID:qk]'/><text column='[ds].[ctd:CASEID:qk]'/>",
              title="SOC By Quarter")
    m = parse_workbook(wb(x), name="t")
    ws = m.worksheet("Sheet 105")
    assert ws.chart_type == "heatmap (highlight table)"
    assert ws.title == "SOC By Quarter"                # the Æ line-break marker is removed
    assert ws.cols.describe() == "YEAR(Parsed Date) › QUARTER(Parsed Date)"
    assert not m.unresolved_refs
    calc = m.field("ds", "Calculation_1")
    assert calc.used_by == ["Sheet 105"] and m.field("ds", "Calculation_2").used_by == []
    assert "COUNTD(CASEID)" in describe_worksheet(ws, m)


def test_filters_of_every_shape():
    f = """
    <filter class='categorical' column='[ds].[none:GENDER:nk]'>
      <groupfilter function='union' user:ui-enumeration='inclusive' user:ui-marker='enumerate'>
        <groupfilter function='member' level='[none:GENDER:nk]' member='&quot;Female&quot;'/>
        <groupfilter function='member' level='[none:GENDER:nk]' member='&quot;Male&quot;'/></groupfilter></filter>
    <filter class='categorical' column='[ds].[none:SOC:nk]'>
      <groupfilter function='except' user:ui-enumeration='exclusive'>
        <groupfilter function='level-members' level='[none:SOC:nk]'/>
        <groupfilter function='member' level='[none:SOC:nk]' member='&quot;Product issues&quot;'/></groupfilter></filter>
    <filter class='categorical' column='[ds].[none:CASEID:nk]' context='true'>
      <groupfilter count='7' end='top' function='end' units='records' user:ui-marker='end'>
        <groupfilter direction='DESC' expression='SUM([x])' function='order'>
          <groupfilter function='level-members' level='[none:CASEID:nk]'/></groupfilter></groupfilter></filter>
    <filter class='quantitative' column='[ds].[none:Calculation_1:qk]' included-values='in-range'>
      <min>#2017-01-03#</min><max>#2020-12-30#</max></filter>
    <filter class='relative-date' column='[ds].[none:Calculation_1:qk]' first-period='-2' last-period='0'
      period-anchor='#2011-12-09 00:00:00#' period-type-v2='day'/>
    <filter class='categorical' column='[ds].[none:SOC:nk]'>
      <groupfilter function='except'><groupfilter function='level-members' level='[none:SOC:nk]'/>
        <groupfilter function='empty-level' member='[none:SOC:nk]'/></groupfilter></filter>
    <filter class='categorical' column='[ds].[Top SOC]'/>
    <filter class='categorical' column='[ds].[Action (SOC)]'>
      <groupfilter function='crossjoin' user:ui-action-filter='[Action1]'>
        <groupfilter function='level-members' level='[none:SOC:nk]'/></groupfilter></filter>
    """
    m = parse_workbook(wb(sheet("S", rows="[ds].[none:SOC:nk]", filters=f)), name="t")
    fs = m.worksheet("S").filters
    assert [x.mode for x in fs] == ["include", "exclude", "top", "range", "relative", "exclude-null", "set", "action"]
    assert fs[0].members == ["Female", "Male"] and fs[2].top["count"] == 7 and fs[2].context
    assert fs[3].range["min"] == "#2017-01-03#"
    assert fs[4].relative["first"] == -2
    assert "keeps 2 value(s) (Female, Male)" in describe_filter(fs[0])
    assert not m.unsupported


def test_feature_flag_variants_keep_true():
    raw = b"""<workbook version='18.1'><datasources><datasource name='d'>
      <connection class='federated'>
        <_.fcp.ObjectModelEncapsulateLegacy.false...relation name='old' type='table'/>
        <_.fcp.ObjectModelEncapsulateLegacy.true...relation name='new' type='table'/>
      </connection></datasource></datasources>
      <dashboards><dashboard name='D'><zones>
        <zone id='1' _.fcp.SetMembershipControl.false...type='layout-basic' _.fcp.SetMembershipControl.true...type-v2='layout-basic'/>
      </zones></dashboard></dashboards></workbook>"""
    root, rep, _ = load_twb(raw)
    rels = root.findall("./datasources/datasource/connection/relation")
    assert [r.get("name") for r in rels] == ["new"] and rep.dropped_false_variants == 1
    zone = root.find(".//zone")
    assert zone.get("type-v2") == "layout-basic" and zone.get("type") == "layout-basic"


def test_text_and_member_helpers():
    root, _, _ = load_twb(b"<t><run>Estimated Sales:</run><run>\xc3\x86 </run><run>Total</run></t>")
    assert text_of(root) == "Estimated Sales: Total"
    assert unescape_member(r"[ds].[usr:\% Diff Sales:qk]") == "[ds].[usr:% Diff Sales:qk]"


def test_dashboard_dedup_hidden_and_actions():
    s1 = sheet("A", rows="[ds].[none:SOC:nk]", enc="<text column='[ds].[ctd:CASEID:qk]'/>", mark="Automatic")
    dash = """<dashboard name='D'><size sizing-mode='automatic'/><zones>
      <zone id='1' type-v2='layout-basic'><zone id='2' name='A'/>
        <zone id='3' type-v2='filter' param='[ds].[none:SOC:nk]' mode='checkdropdown'/></zone></zones>
      <devicelayouts><devicelayout name='Phone'><zones><zone id='9' name='A'/></zones></devicelayout></devicelayouts>
      </dashboard>"""
    win = "<window class='worksheet' name='A' hidden='true'/><window class='dashboard' name='D'/>"
    m = parse_workbook(wb(s1, dash, win), name="t")
    d = m.dashboard("D")
    assert d.sheets == ["A"] and d.device_layouts == ["Phone"] and d.quick_filters[0].field == "SOC"
    assert m.worksheet("A").hidden and m.worksheet("A").in_dashboards == ["D"]
    assert m.datasources[0].connections[0].filename.startswith("C:/Users")   # kept locally; redact before sharing
