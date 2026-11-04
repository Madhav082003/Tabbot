"""Build a synthetic Tableau workbook (fixture.twbx) with made-up data, for tests and the app demo button.

No real data is used. The workbook follows the element paths in docs/TABLEAU_REFERENCE.md and plants:
- a data story: one CATEGORY roughly triples in the last 3 full quarters, driven by one CHANNEL value;
- known definition problems (counting dates, "%" name with counts, copied filter, misnamed sheet,
  calc ignoring a unit column, unused calcs, big exclude filter, local path in connection,
  placeholder title, hidden sheets);
- a partial last quarter, several rows per entity, text dates, a free-text column.

Everything planted is written to expected.json next to the .twbx.

Usage:  python -m tabbot.demo_workbook [out_dir]
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import random
import re
import struct
import sys
import zipfile
import zlib
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import pandas as pd
from tableauhyperapi import (
    NOT_NULLABLE,
    NULLABLE,
    Connection,
    CreateMode,
    HyperProcess,
    Inserter,
    SqlType,
    TableDefinition,
    TableName,
    Telemetry,
)

SEED = 42
DS = "federated.fx01"
DS_CAPTION = "Fixture Records"
START = dt.date(2021, 1, 1)
END = dt.date(2026, 5, 20)  # mid-quarter on purpose (Q2 2026 is partial)
SURGE_START = dt.date(2025, 7, 1)
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

CATEGORIES = [f"Category {c}" for c in "ABCDEFGH"]
SURGE_CATEGORY = "Category C"
CHANNELS = ["Phone", "Email", "Web"]
SURGE_CHANNEL = "Web"
REGIONS = ["North", "South", "East", "West", "Central", "Overseas"]
OUTCOMES = ["Recovered", "Not recovered", "Unknown", "Fatal"]
WORDS = ("patient reported mild moderate severe pain after taking the second dose with no prior history "
         "resolved within days follow up requested by caller noted dizziness rash nausea fatigue").split()

# ----------------------------------------------------------------------------------------------- data


def fmt_date(d: dt.date) -> str:
    return f"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year}"


def rand_date(rng: random.Random, a: dt.date, b: dt.date) -> dt.date:
    return a + dt.timedelta(days=rng.randrange((b - a).days + 1))


def make_rows(rng: random.Random) -> list[dict]:
    subcats = {c: [f"{c.split()[1]}{i:02d} subtype" for i in range(1, 11)] for c in CATEGORIES}
    rows: list[dict] = []
    n = 0

    def add_entity(date: dt.date, channel: str, cats: list[str]) -> None:
        nonlocal n
        n += 1
        rid = f"R{n:05d}"
        g = rng.random()
        gender = "Female" if g < 0.50 else "Male" if g < 0.95 else "UNK" if g < 0.98 else None
        unit_roll = rng.random()
        if unit_roll < 0.05:
            age, unit = None, None
        elif unit_roll < 0.055:
            age, unit = rng.randint(3, 23), "Months"
        elif unit_roll < 0.058:
            age, unit = rng.randint(20, 300), "Days"
        else:
            age, unit = rng.randint(1, 90), "Years"
        o = rng.random()
        outcome = OUTCOMES[0] if o < 0.60 else OUTCOMES[1] if o < 0.85 else OUTCOMES[2] if o < 0.985 else OUTCOMES[3]
        region = rng.choice(REGIONS)
        for i, cat in enumerate(cats, start=1):
            rows.append({
                "RECORD_ID": rid,
                "RECEIVED_DATE": fmt_date(date),
                "CATEGORY": cat,
                "SUBCATEGORY": rng.choice(subcats[cat]),
                "CHANNEL": channel,
                "REGION": region,
                "GENDER": gender,
                "AGE": age,
                "AGE_UNIT": unit,
                "SEVERITY": "Serious" if rng.random() < 0.15 else "Non-serious",
                "OUTCOME": outcome,
                "NOTES": " ".join(rng.choice(WORDS) for _ in range(rng.randint(8, 18))) + f" #{n}-{i}",
                "SORT_ID": i,
            })

    # baseline: 2,600 entities spread over the whole period, 2-8 rows each
    for _ in range(2600):
        c = rng.random()
        channel = "Phone" if c < 0.5 else "Email" if c < 0.8 else "Web"
        k = rng.randint(2, 8)
        add_entity(rand_date(rng, START, END), channel, [rng.choice(CATEGORIES) for _ in range(k)])

    # planted surge: extra Web entities in SURGE_CATEGORY from SURGE_START, simpler records (2 rows)
    for _ in range(480):
        add_entity(rand_date(rng, SURGE_START, END), SURGE_CHANNEL, [SURGE_CATEGORY, rng.choice(CATEGORIES)])
    return rows


def write_hyper(rows: list[dict], path: Path, log_dir: Path) -> None:
    text_cols = ["RECORD_ID", "RECEIVED_DATE", "CATEGORY", "SUBCATEGORY", "CHANNEL", "REGION", "GENDER"]
    cols = [TableDefinition.Column(c, SqlType.text(), NOT_NULLABLE if c == "RECORD_ID" else NULLABLE)
            for c in text_cols]
    cols += [TableDefinition.Column("AGE", SqlType.big_int(), NULLABLE),
             TableDefinition.Column("AGE_UNIT", SqlType.text(), NULLABLE),
             TableDefinition.Column("SEVERITY", SqlType.text(), NULLABLE),
             TableDefinition.Column("OUTCOME", SqlType.text(), NULLABLE),
             TableDefinition.Column("NOTES", SqlType.text(), NULLABLE),
             TableDefinition.Column("SORT_ID", SqlType.big_int(), NULLABLE)]
    table = TableDefinition(TableName("Extract", "Extract"), cols)
    order = [c.name.unescaped for c in cols]
    with HyperProcess(Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU, parameters={"log_dir": str(log_dir)}) as hp:
        with Connection(hp.endpoint, str(path), CreateMode.CREATE_AND_REPLACE) as con:
            con.catalog.create_schema_if_not_exists("Extract")
            con.catalog.create_table(table)
            with Inserter(con, table) as ins:
                ins.add_rows([[r[c] for c in order] for r in rows])
                ins.execute()


# ----------------------------------------------------------------------------------------------- png


def tiny_png(w: int, h: int, rgb: tuple[int, int, int]) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


# ----------------------------------------------------------------------------------------------- twb

CALCS = {
    # internal name: (caption, datatype, role, type, formula)
    "Calculation_1001": ("Parsed Date", "date", "dimension", "ordinal", "DATE([RECEIVED_DATE])"),
    "Calculation_1002": ("Age Group", "string", "dimension", "nominal",
                         'IF ISNULL([AGE]) THEN "Not Reported" ELSEIF [AGE] < 18 THEN "Under 18" '
                         'ELSEIF [AGE] < 65 THEN "18 to 64" ELSE "65 and above" END'),
    "Calculation_1003": ("Age Years", "real", "measure", "quantitative",
                         'IF [AGE_UNIT] = "Years" THEN [AGE] ELSEIF [AGE_UNIT] = "Months" THEN [AGE] / 12 '
                         'ELSEIF [AGE_UNIT] = "Days" THEN [AGE] / 365 END'),
    "Calculation_1004": ("Serious Records", "integer", "measure", "quantitative",
                         'COUNTD(IF [SEVERITY] = "Serious" THEN [RECORD_ID] END)'),
    "Calculation_1005": ("Rows Per Record", "real", "measure", "quantitative",
                         "COUNT([SUBCATEGORY]) / COUNTD([RECORD_ID])"),
    "Serious Records (copy)_1006": ("Non-serious Records", "integer", "measure", "quantitative",
                                    'COUNTD(IF [SEVERITY] = "Non-serious" THEN [RECORD_ID] END)'),
}
PHYSICAL = [("RECORD_ID", "string"), ("RECEIVED_DATE", "string"), ("CATEGORY", "string"),
            ("SUBCATEGORY", "string"), ("CHANNEL", "string"), ("REGION", "string"), ("GENDER", "string"),
            ("AGE", "integer"), ("AGE_UNIT", "string"), ("SEVERITY", "string"), ("OUTCOME", "string"),
            ("NOTES", "string"), ("SORT_ID", "integer")]

DERIVATION_NAME = {"none": "None", "ctd": "CountD", "yr": "Year", "qr": "Quarter", "tqr": "Quarter-Trunc",
                   "usr": "User", "sum": "Sum"}
TYPE_NAME = {"nk": "nominal", "ok": "ordinal", "qk": "quantitative"}


def ref(deriv: str, field: str, tk: str) -> str:
    return f"[{DS}].[{deriv}:{field}:{tk}]"


def inner(r: str) -> str:
    """'[ds].[ctd:X:qk]' -> 'ctd:X:qk'"""
    return r.split("].[", 1)[1].rstrip("]")


def member_el(level: str, m: str) -> str:
    """One <groupfilter function='member'>; Tableau stores member values wrapped in double quotes."""
    quoted = '"' + m + '"'
    return f"<groupfilter function='member' level={quoteattr(level)} member={quoteattr(quoted)} />"


def member_filter(field_ref: str, members: list[str]) -> str:
    level = "[" + inner(field_ref) + "]"
    if len(members) == 1:
        m = quoteattr(f'"{members[0]}"')
        return (f"<filter class='categorical' column={quoteattr(field_ref)}>"
                f"<groupfilter function='member' level={quoteattr(level)} member={m} "
                f"user:ui-domain='database' user:ui-enumeration='inclusive' user:ui-marker='enumerate' />"
                f"</filter>")
    kids = "".join(member_el(level, m) for m in members)
    return (f"<filter class='categorical' column={quoteattr(field_ref)}>"
            f"<groupfilter function='union' user:ui-domain='database' user:ui-enumeration='inclusive' "
            f"user:ui-marker='enumerate'>{kids}</groupfilter></filter>")


def except_filter(field_ref: str, members: list[str]) -> str:
    level = "[" + inner(field_ref) + "]"
    kids = "".join(member_el(level, m) for m in members)
    return (f"<filter class='categorical' column={quoteattr(field_ref)}>"
            f"<groupfilter function='except' user:ui-domain='database' user:ui-enumeration='exclusive'>"
            f"<groupfilter function='level-members' level={quoteattr(level)} />"
            f"<groupfilter function='union'>{kids}</groupfilter></groupfilter></filter>")


def deps_xml(refs: list[str]) -> str:
    fields, instances = {}, {}
    for r in refs:
        deriv, field, tk = inner(r).split(":")
        fields[field] = True
        instances[inner(r)] = (deriv, field, tk)
    cols = []
    for f in fields:
        if f in CALCS:
            cap, dtype, role, typ, formula = CALCS[f]
            cols.append(f"<column caption={quoteattr(cap)} datatype='{dtype}' name={quoteattr('[' + f + ']')} "
                        f"role='{role}' type='{typ}'><calculation class='tableau' formula={quoteattr(formula)} />"
                        f"</column>")
        else:
            dtype = dict(PHYSICAL)[f]
            cols.append(f"<column datatype='{dtype}' name='[{f}]' role='dimension' type='nominal' />")
    for name, (deriv, field, tk) in instances.items():
        cols.append(f"<column-instance column={quoteattr('[' + field + ']')} derivation='{DERIVATION_NAME[deriv]}' "
                    f"name={quoteattr('[' + name + ']')} pivot='key' type='{TYPE_NAME[tk]}' />")
    return f"<datasource-dependencies datasource='{DS}'>{''.join(cols)}</datasource-dependencies>"


def worksheet_xml(name: str, *, title: str | None, rows: str, cols: str, mark: str,
                  encodings: dict[str, str], filters: list[str]) -> str:
    refs = [r for r in (rows.replace("(", " ").replace(")", " ").replace(" / ", " ").split() if rows else [])]
    refs += [r for r in (cols.replace("(", " ").replace(")", " ").replace(" / ", " ").split() if cols else [])]
    refs += list(encodings.values())
    for f in filters:
        refs.append(re.search(r"column=[\"']([^\"']+)[\"']", f).group(1))
    refs = [r for r in refs if r.startswith("[")]
    title_xml = (f"<layout-options><title><formatted-text><run>{escape(title)}</run></formatted-text></title>"
                 f"</layout-options>") if title else ""
    enc = "".join(f"<{k} column={quoteattr(v)} />" for k, v in encodings.items())
    return (f"<worksheet name={quoteattr(name)}>{title_xml}<table><view>"
            f"<datasources><datasource caption={quoteattr(DS_CAPTION)} name='{DS}' /></datasources>"
            f"{deps_xml(refs)}{''.join(filters)}<aggregation value='true' /></view><style />"
            f"<panes><pane selection-relaxation-option='selection-relaxation-allow'><view><breakdown value='auto' />"
            f"</view><mark class='{mark}' /><encodings>{enc}</encodings></pane></panes>"
            f"<rows>{escape(rows)}</rows><cols>{escape(cols)}</cols></table>"
            f"<simple-id uuid='{{00000000-0000-0000-0000-{zlib.crc32(name.encode()):012d}}}' /></worksheet>")


PD = "Calculation_1001"  # Parsed Date
CTD = ref("ctd", "RECORD_ID", "qk")
YQ = f"({ref('yr', PD, 'ok')} / {ref('qr', PD, 'ok')})"
TQR = ref("tqr", PD, "qk")


def build_worksheets(excluded_subcats: list[str]) -> dict[str, dict]:
    gender_filter = member_filter(ref("none", "GENDER", "nk"), ["Female", "Male"])
    return {
        "Category Heatmap": dict(title="Records by Category and Quarter", rows=ref("none", "CATEGORY", "nk"),
                                 cols=YQ, mark="Square", encodings={"color": CTD, "text": CTD},
                                 filters=[member_filter(ref("none", "CATEGORY", "nk"), CATEGORIES)]),
        "Records Trend": dict(title="Records per Quarter", rows=CTD, cols=TQR, mark="Line",
                              encodings={"text": CTD}, filters=[]),
        "Channel Trend": dict(title="Records per Quarter by Channel", rows=CTD, cols=TQR, mark="Line",
                              encodings={"color": ref("none", "CHANNEL", "nk")}, filters=[]),
        "Serious Trend": dict(title="Serious Records per Quarter", rows=ref("usr", "Calculation_1004", "qk"),
                              cols=TQR, mark="Line", encodings={}, filters=[]),
        "Gender Pie": dict(title="Records by Gender", rows="", cols="", mark="Pie",
                           encodings={"color": ref("none", "GENDER", "nk"), "size": CTD, "text": CTD},
                           filters=[gender_filter]),
        # planted L3: gender filter copied onto a chart that does not show gender
        "Age Group Pie": dict(title="Records by Age Group", rows="", cols="", mark="Pie",
                              encodings={"color": ref("none", "Calculation_1002", "nk"), "size": CTD, "text": CTD},
                              filters=[gender_filter]),
        "Region Table": dict(title="Records by Region and Year", rows=ref("none", "REGION", "nk"),
                             cols=ref("yr", PD, "ok"), mark="Automatic", encodings={"text": CTD}, filters=[]),
        "Category A": dict(title="Category A", rows=CTD, cols=TQR, mark="Line", encodings={},
                           filters=[member_filter(ref("none", "CATEGORY", "nk"), ["Category A"])]),
        # planted L4: sheet named after one member, filters another
        "Category B": dict(title="Category D", rows=CTD, cols=TQR, mark="Line", encodings={},
                           filters=[member_filter(ref("none", "CATEGORY", "nk"), ["Category D"])]),
        # planted L1 (counts distinct dates) + L12/L13 (large exclude list)
        "Dates by Subcategory": dict(title="Subcategory by Year", rows=ref("none", "SUBCATEGORY", "nk"),
                                     cols=ref("yr", PD, "ok"), mark="Square",
                                     encodings={"color": ref("ctd", PD, "qk"), "text": ref("ctd", PD, "qk")},
                                     filters=[except_filter(ref("none", "SUBCATEGORY", "nk"), excluded_subcats)]),
        # planted L10 (the only visible worksheet) + L11 (placeholder title)
        "Outcome Bars": dict(title="<Sheet Name>", rows=ref("none", "OUTCOME", "nk"), cols=CTD, mark="Bar",
                             encodings={}, filters=[]),
    }


DASHBOARDS = {
    "Category by Quarter": dict(sheets=["Category Heatmap"], filters=[ref("none", "CATEGORY", "nk")],
                                legend=("Category Heatmap", CTD)),
    "Volume Trend": dict(sheets=["Records Trend", "Channel Trend", "Serious Trend"],
                         filters=[ref("none", "CHANNEL", "nk")],
                         legend=("Channel Trend", ref("none", "CHANNEL", "nk"))),
    # planted L2: "%" in the name, but the charts show counts
    "Demographics % Share": dict(sheets=["Gender Pie", "Age Group Pie", "Region Table"], filters=[], legend=None),
    "Category Detail": dict(sheets=["Category A", "Category B", "Dates by Subcategory"], filters=[], legend=None),
}


def zones_xml(name: str, d: dict, next_id: list[int]) -> str:
    def zid() -> int:
        next_id[0] += 1
        return next_id[0]

    sheets = d["sheets"]
    parts = [f"<zone h='8000' id='{zid()}' type-v2='title' w='100000' x='0' y='0' />"]
    has_side = bool(d["filters"] or d["legend"])
    body_w = 80000 if has_side else 100000
    n = len(sheets)
    cols_n = 1 if n == 1 else 2
    rows_n = (n + cols_n - 1) // cols_n
    cell_w, cell_h = body_w // cols_n, 92000 // rows_n
    for i, s in enumerate(sheets):
        x, y = (i % cols_n) * cell_w, 8000 + (i // cols_n) * cell_h
        parts.append(f"<zone h='{cell_h}' id='{zid()}' name={quoteattr(s)} w='{cell_w}' x='{x}' y='{y}' />")
    y = 8000
    for f in d["filters"]:
        parts.append(f"<zone h='10000' id='{zid()}' mode='checkdropdown' name={quoteattr(sheets[0])} "
                     f"param={quoteattr(f)} type-v2='filter' w='20000' x='80000' y='{y}' />")
        y += 10000
    if d["legend"]:
        parts.append(f"<zone h='20000' id='{zid()}' name={quoteattr(d['legend'][0])} param={quoteattr(d['legend'][1])} "
                     f"type-v2='color' w='20000' x='80000' y='{y}' />")
    return (f"<zone h='100000' id='{zid()}' type-v2='layout-basic' w='100000' x='0' y='0'>"
            f"{''.join(parts)}</zone>")


def dashboard_xml(name: str, d: dict) -> str:
    counter = [0]
    main = zones_xml(name, d, counter)
    phone = zones_xml(name, d, counter)
    return (f"<dashboard name={quoteattr(name)}><style />"
            f"<size maxheight='800' maxwidth='1200' minheight='800' minwidth='1200' />"
            f"<zones>{main}</zones>"
            f"<devicelayouts><devicelayout auto-generated='true' name='Phone'>"
            f"<size maxheight='1300' minheight='1300' sizing-mode='vscroll' /><zones>{phone}</zones>"
            f"</devicelayout></devicelayouts>"
            f"<simple-id uuid='{{11111111-0000-0000-0000-{zlib.crc32(name.encode()):012d}}}' /></dashboard>")


def datasource_xml() -> str:
    mrs = "".join(
        f"<metadata-record class='column'><remote-name>{c}</remote-name><remote-type>130</remote-type>"
        f"<local-name>[{c}]</local-name><parent-name>[Sheet1$]</parent-name><remote-alias>{c}</remote-alias>"
        f"<local-type>{t}</local-type><aggregation>Count</aggregation><contains-null>true</contains-null>"
        f"</metadata-record>" for c, t in PHYSICAL)
    calc_cols = "".join(
        f"<column caption={quoteattr(cap)} datatype='{dtype}' name={quoteattr('[' + nm + ']')} role='{role}' "
        f"type='{typ}'><calculation class='tableau' formula={quoteattr(formula)} /></column>"
        for nm, (cap, dtype, role, typ, formula) in CALCS.items())
    phys_cols = ("<column datatype='string' name='[CATEGORY]' role='dimension' type='nominal' />"
                 "<column datatype='integer' name='[AGE]' role='measure' type='quantitative' />")
    # planted L14: a local path with a user name in the connection
    return (
        f"<datasources><datasource caption={quoteattr(DS_CAPTION)} inline='true' name='{DS}' version='18.1'>"
        f"<connection class='federated'><named-connections>"
        f"<named-connection caption='fixture_source' name='excel-direct.0abc'>"
        f"<connection class='excel-direct' cleaning='no' compat='no' dataRefreshTime='' "
        f"filename='C:/Users/jdoe/Documents/Client Data/fixture_source.xlsx' interpretationMode='0' "
        f"password='' server='' validate='no' /></named-connection></named-connections>"
        f"<relation connection='excel-direct.0abc' name='Sheet1' table='[Sheet1$]' type='table' />"
        f"<metadata-records>{mrs}</metadata-records></connection>"
        f"{phys_cols}{calc_cols}"
        f"<extract count='-1' enabled='true' object-id='' units='records'>"
        f"<connection access_mode='readonly' authentication='auth-none' author-locale='en_US' class='hyper' "
        f"dbname='Data/fixture.twb Files/federated.hyper' default-settings='hyper' schema='Extract' "
        f"sslmode='' tablename='Extract' update-time='05/21/2026 09:00:00 AM' username='tableau_internal_user'>"
        f"<relation name='Extract' table='[Extract].[Extract]' type='table' />"
        f"<metadata-records>{mrs}</metadata-records></connection></extract>"
        f"</datasource></datasources>")


def build_twb(excluded_subcats: list[str]) -> tuple[str, dict]:
    sheets = build_worksheets(excluded_subcats)
    ws_xml = "".join(worksheet_xml(n, **spec) for n, spec in sheets.items())
    db_xml = "".join(dashboard_xml(n, d) for n, d in DASHBOARDS.items())
    visible = {"Outcome Bars"}
    HIDDEN = "hidden='true'"
    windows = "".join(
        f"<window class='worksheet' {'' if n in visible else HIDDEN} maximized='true' name={quoteattr(n)}>"
        f"<cards /></window>" for n in sheets)
    windows += "".join(f"<window class='dashboard' maximized='true' name={quoteattr(n)}><viewpoints /></window>"
                       for n in DASHBOARDS)
    colours = [(78, 121, 167), (242, 142, 43), (225, 87, 89), (118, 183, 178), (89, 161, 79)]
    thumbs = "".join(
        f"<thumbnail height='192' name={quoteattr(n)} width='192'>"
        f"{base64.b64encode(tiny_png(192, 192, colours[i % len(colours)])).decode()}</thumbnail>"
        for i, n in enumerate(list(DASHBOARDS) + sorted(visible)))
    twb = (
        "<?xml version='1.0' encoding='utf-8' ?>\n"
        "<workbook original-version='18.1' source-build='2026.2.0 (20262.26.0101.0000)' source-platform='win' "
        "version='18.1' xml:base='https://example.online.tableau.com' "
        "xmlns:user='http://www.tableausoftware.com/xml/user'>"
        "<document-format-change-manifest><_.fcp.MarkAnimation.true...MarkAnimation />"
        "</document-format-change-manifest>"
        "<repository-location id='FixtureWorkbook' path='/t/example/workbooks' revision='' site='example' />"
        "<preferences><preference name='ui.encoding.shelf.height' value='24' /></preferences>"
        "<style />"
        f"{datasource_xml()}<worksheets>{ws_xml}</worksheets><dashboards>{db_xml}</dashboards>"
        f"<windows source-height='30'>{windows}</windows><thumbnails>{thumbs}</thumbnails></workbook>"
    )
    return twb, sheets


# ----------------------------------------------------------------------------------------------- expected


def expected_facts(rows: list[dict], sheets: dict, excluded_subcats: list[str]) -> dict:
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["RECEIVED_DATE"], format="%d-%b-%Y")
    df["q"] = df["date"].dt.to_period("Q")
    last_q = df["q"].max()
    full_qs = sorted(q for q in df["q"].unique() if q != last_q)
    last3, prev3 = full_qs[-3:], full_qs[-6:-3]
    cat = df[df["CATEGORY"] == SURGE_CATEGORY]
    c_last3 = int(cat[cat["q"].isin(last3)]["RECORD_ID"].nunique())
    c_prev3 = int(cat[cat["q"].isin(prev3)]["RECORD_ID"].nunique())
    by_channel = cat[cat["q"].isin(last3)].groupby("CHANNEL")["RECORD_ID"].nunique() \
        - cat[cat["q"].isin(prev3)].groupby("CHANNEL")["RECORD_ID"].nunique()
    ents = df.groupby("RECORD_ID").first()
    odd_units = ents[ents["AGE_UNIT"].isin(["Months", "Days"])]

    dashboards = {n: d["sheets"] for n, d in DASHBOARDS.items()}
    used_calcs = sorted({CALCS[c][0] for s in sheets.values()
                         for c in CALCS if c in (s["rows"] + s["cols"] + " ".join(s["encodings"].values()))})
    return {
        "workbook": {
            "datasource": DS, "datasource_caption": DS_CAPTION, "connection_class": "excel-direct",
            "physical_fields": len(PHYSICAL), "calculated_fields": len(CALCS),
            "calc_captions": {k: v[0] for k, v in CALCS.items()},
            "used_calcs": used_calcs,
            "unused_calcs": sorted(v[0] for v in CALCS.values() if v[0] not in used_calcs),
            "worksheets": len(sheets),
            "mark_by_sheet": {n: s["mark"] for n, s in sheets.items()},
            "dashboards": dashboards,
            "quick_filters": {n: [inner(f).split(":")[1] for f in d["filters"]] for n, d in DASHBOARDS.items()},
            "hidden_worksheets": sorted(n for n in sheets if n != "Outcome Bars"),
            "visible_worksheets": ["Outcome Bars"],
            "thumbnails": len(DASHBOARDS) + 1,
            "has_device_layouts": True,
        },
        "data": {
            "table": '"Extract"."Extract"', "rows": len(df), "columns": len(PHYSICAL),
            "entity": "RECORD_ID", "entities": int(df["RECORD_ID"].nunique()),
            "date_field": "RECEIVED_DATE", "date_format": "%d-%b-%Y",
            "date_min": str(df["date"].min().date()), "date_max": str(df["date"].max().date()),
            "partial_last_period": str(last_q),
            "free_text_columns": ["NOTES"], "id_columns": ["RECORD_ID"],
            "unit_companion": {"AGE": "AGE_UNIT"},
            "entities_with_non_year_age": int(len(odd_units)),
        },
        "story": {
            "category_field": "CATEGORY", "surge_category": SURGE_CATEGORY,
            "driver_field": "CHANNEL", "driver_value": SURGE_CHANNEL,
            "last3_full_quarters": [str(q) for q in last3], "prev3_full_quarters": [str(q) for q in prev3],
            "surge_category_prev3": c_prev3, "surge_category_last3": c_last3,
            "ratio": round(c_last3 / c_prev3, 2),
            "delta_by_channel": {k: int(v) for k, v in by_channel.fillna(0).items()},
        },
        "planted_problems": {
            "counts_dates": {"sheet": "Dates by Subcategory", "field": "Parsed Date"},
            "percent_name_without_percent": {"dashboard": "Demographics % Share"},
            "copied_filter": {"sheet": "Age Group Pie", "field": "GENDER"},
            "misnamed_sheet": {"sheet": "Category B", "filters": "Category D"},
            "unit_ignored": {"calc": "Age Group", "field": "AGE", "unit_field": "AGE_UNIT", "used_by": "Age Years"},
            "unused_calcs": sorted(v[0] for v in CALCS.values() if v[0] not in used_calcs),
            "large_exclude_filter": {"sheet": "Dates by Subcategory", "members": len(excluded_subcats)},
            "local_path_in_connection": True,
            "placeholder_title": {"sheet": "Outcome Bars"},
            "hidden_sheets": len(sheets) - 1,
        },
    }


# ----------------------------------------------------------------------------------------------- main


def build(out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)
    rows = make_rows(rng)
    all_subcats = sorted({r["SUBCATEGORY"] for r in rows})
    excluded = all_subcats[:25]

    work = out_dir / "_build"
    work.mkdir(exist_ok=True)
    hyper_path = work / "federated.hyper"
    write_hyper(rows, hyper_path, work)
    twb, sheets = build_twb(excluded)

    twbx = out_dir / "fixture.twbx"
    with zipfile.ZipFile(twbx, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("fixture.twb", twb.encode("utf-8"))
        z.write(hyper_path, "Data/fixture.twb Files/federated.hyper")
    (out_dir / "expected.json").write_text(json.dumps(expected_facts(rows, sheets, excluded), indent=2),
                                           encoding="utf-8")
    for p in work.iterdir():
        p.unlink()
    work.rmdir()
    return twbx


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd() / "demo_out"
    path = build(target)
    print(f"wrote {path} ({path.stat().st_size // 1024} KB) and expected.json")
