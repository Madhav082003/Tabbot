"""Find the patterns in how a workbook is built.

- Sheet families: sheets with the same design (chart type, shelves, marks, filter fields) that differ only in
  the single value their filter keeps - e.g. "27 pie charts, one per SOC".
- Dashboard groups: dashboards that hold parts of one family, e.g. "...Trellis -1" and "...Trellis -2".
Generic: no field or sheet names are assumed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from tabbot.tparse.model import Dashboard as KDashboard
from tabbot.tparse.model import WorkbookModel as KModel
from tabbot.tparse.model import Worksheet as KWorksheet

MIN_FAMILY = 3


def shape(ws: KWorksheet) -> tuple:
    """What a sheet shows, ignoring which single value its filters keep and its title."""
    enc = tuple(sorted((e.channel, e.field.inner) for p in ws.panes for e in p.encodings))
    flt = tuple(sorted((f.field.inner, f.mode, f.member_count == 1) for f in ws.filters))
    return (ws.chart_type, ws.rows.raw.replace(" ", ""), ws.cols.raw.replace(" ", ""), enc, flt)


@dataclass
class Family:
    sheets: list[KWorksheet]
    varying_field: str | None = None        # caption of the field whose single filter value differs per sheet
    values: list[str] = field(default_factory=list)

    @property
    def example(self) -> KWorksheet:
        return self.sheets[0]


def sheet_families(model: KModel) -> list[Family]:
    groups: dict[tuple, list[KWorksheet]] = {}
    for w in model.worksheets:
        groups.setdefault(shape(w), []).append(w)
    out = []
    for ws in groups.values():
        if len(ws) < MIN_FAMILY:
            continue
        fam = Family(sheets=ws)
        singles = [f for f in ws[0].filters if f.mode == "include" and f.member_count == 1]
        for f0 in singles:
            vals = []
            for w in ws:
                fd = next((f for f in w.filters if f.field.inner == f0.field.inner and f.member_count == 1), None)
                vals.append(fd.members[0] if fd else None)
            if len(set(vals)) > 1:
                fam.varying_field = f0.field.caption or f0.field.field
                fam.values = [v for v in vals if v is not None]
                break
        out.append(fam)
    out.sort(key=lambda f: -len(f.sheets))
    return out


def base_name(name: str) -> str:
    """'MedDRA SOCs % by Gender Pie Trellis (cumul)-1' -> 'MedDRA SOCs % by Gender Pie Trellis (cumul)'."""
    s = name.strip()
    s = re.sub(r"\s*[-–_]\s*\d+\s*$", "", s)
    s = re.sub(r"\s*\(\d+\)\s*$", "", s)
    s = re.sub(r"\s+(part|page)\s*\d+\s*$", "", s, flags=re.I)
    return s.strip()


@dataclass
class DashboardGroup:
    name: str                               # common name of the group (or the dashboard's own name)
    dashboards: list[KDashboard]
    family: Family | None = None            # when the dashboards hold parts of one sheet family

    @property
    def sheets(self) -> list[str]:
        out: list[str] = []
        for d in self.dashboards:
            out += [s for s in d.sheets if s not in out]
        return out


def dashboard_groups(model: KModel, families: list[Family]) -> list[DashboardGroup]:
    fam_of = {w.name: fam for fam in families for w in fam.sheets}
    by_key: dict[tuple, list[KDashboard]] = {}
    order: list[tuple] = []
    for d in model.dashboards:
        fams = {id(fam_of[s]) for s in d.sheets if s in fam_of}
        fam_key = next(iter(fams)) if len(fams) == 1 and all(s in fam_of for s in d.sheets) else None
        key = (base_name(d.name), fam_key)
        if key not in by_key:
            by_key[key] = []
            order.append(key)
        by_key[key].append(d)
    out = []
    for key in order:
        ds = by_key[key]
        fam = fam_of.get(ds[0].sheets[0]) if key[1] is not None and ds[0].sheets else None
        out.append(DashboardGroup(name=key[0] if len(ds) > 1 else ds[0].name.strip(), dashboards=ds, family=fam))
    return out


def main_sheet(model: KModel, d: KDashboard) -> KWorksheet | None:
    """The sheet a dashboard is mostly about: the largest zone, else the first sheet."""
    best, area = None, -1
    for z in d.zones:
        if z.kind in ("sheet", "sheet-or-object") and z.name in d.sheets:
            a = (z.w or 0) * (z.h or 0)
            if a > area:
                best, area = z.name, a
    name = best or (d.sheets[0] if d.sheets else None)
    return model.worksheet(name) if name else None
