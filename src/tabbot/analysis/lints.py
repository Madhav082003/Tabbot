"""Definition checks on a whole workbook (generic rules; no field or sheet names are hard-coded).

L1 counts dates instead of entities      L2 "%"/share in a name but no percentage calculation
L3 filter copied onto a chart that does not show that field
L4 sheet named after one value but filtered to another
L5 calculation ignores a unit/currency companion column that another calculation uses
L6 calculated field used by no sheet      L10 hidden sheets (info)   L12 large exclude lists (info)
L14 connection points to a local file path (info)
L15 sheets of one dashboard carry different filters (built from a sheet x filter table of the dashboard)
"""

from __future__ import annotations

import re
from collections import defaultdict

from pydantic import BaseModel

from tabbot.data.store import qi
from tabbot.twb.model import Field, Filter, WorkbookModel, Worksheet

FIELD_TOKEN = re.compile(r"\[((?:[^\]]|\]\])+)\]")
PERCENT_WORDS = re.compile(r"%|\bpercent(age)?s?\b|\bshare\b|\bpct\b|\bproportion\b|\bratio\b", re.I)
UNIT_SUFFIXES = {"unit", "units", "uom", "currency", "scale", "unitofmeasure", "curr"}
DATE_TYPES = {"date", "datetime"}


class Finding(BaseModel):
    code: str
    severity: str           # info | warn | error
    title: str
    message: str
    sheets: list[str] = []
    dashboards: list[str] = []
    evidence: dict = {}


def _formula_refs(f: Field) -> set[str]:
    return {t.replace("]]", "]") for t in FIELD_TOKEN.findall(f.formula or "")}


def _shown_fields(ws: Worksheet) -> set[tuple[str, str]]:
    refs = list(ws.rows) + list(ws.cols) + [r for v in ws.encodings.values() for r in v]
    return {(r.datasource, r.field) for r in refs}


def _filter_key(f: Filter) -> tuple:
    return (f.field.datasource, f.field.field, f.field.derivation, f.mode, tuple(sorted(f.members)))


def _dashboards_of(model: WorkbookModel, sheet: str) -> list[str]:
    return [d.name for d in model.dashboards if sheet in d.sheets]


# --------------------------------------------------------------------------------------------- rules


def l1_counts_dates(model: WorkbookModel) -> list[Finding]:
    out = []
    for ws in model.worksheets:
        for r in [r for v in ws.encodings.values() for r in v] + ws.rows + ws.cols:
            if r.aggregation not in ("count_distinct", "count"):
                continue
            f = model.field(r.datasource, r.field)
            if f is not None and (f.datatype or "").lower() in DATE_TYPES:
                out.append(Finding(
                    code="L1", severity="warn", title="Counts dates, not records",
                    message=f"'{ws.name}' shows {r.label}: it counts distinct dates, so two records received on the "
                            f"same day count once. Other sheets count records.",
                    sheets=[ws.name], dashboards=_dashboards_of(model, ws.name),
                    evidence={"measure": r.label, "field": f.caption, "datatype": f.datatype}))
                break
    return out


def _has_percent(ws: Worksheet) -> bool:
    if ws.table_calcs:
        return True
    return any(r.derivation.startswith(("pcto", "pcdf", "pctrk")) for r in ws.all_refs())


def l2_percent_without_percentage(model: WorkbookModel) -> list[Finding]:
    out = []
    for d in model.dashboards:
        if PERCENT_WORDS.search(d.name or "") or PERCENT_WORDS.search(d.title or ""):
            sheets = [model.worksheet(s) for s in d.sheets if model.worksheet(s)]
            if sheets and not any(_has_percent(w) for w in sheets):
                out.append(Finding(
                    code="L2", severity="warn", title="Name promises percentages, charts show counts",
                    message=f"Dashboard '{d.name}' suggests percentages/shares, but none of its {len(sheets)} sheets "
                            "has a percent-of-total calculation: the labels are raw counts.",
                    dashboards=[d.name], sheets=d.sheets))
    for ws in model.worksheets:
        if ws.title and PERCENT_WORDS.search(ws.title) and not _has_percent(ws) and \
                not any(PERCENT_WORDS.search(d) for d in _dashboards_of(model, ws.name)):
            out.append(Finding(code="L2", severity="warn", title="Title promises percentages, chart shows counts",
                               message=f"Sheet '{ws.name}' has a percent/share title but no percentage calculation.",
                               sheets=[ws.name], dashboards=_dashboards_of(model, ws.name)))
    return out


def l3_copied_filter(model: WorkbookModel) -> list[Finding]:
    by_key: dict[tuple, list[Worksheet]] = defaultdict(list)
    for ws in model.worksheets:
        for f in ws.filters:
            if f.mode in ("include", "exclude") and f.members and not f.keeps_all_values:
                by_key[_filter_key(f)].append(ws)
    groups: dict[tuple, list[str]] = defaultdict(list)
    for key, sheets in by_key.items():
        field_id = (key[0], key[1])
        shown_somewhere = [w for w in sheets if field_id in _shown_fields(w)]
        if not shown_somewhere:
            continue
        for w in sheets:
            if field_id not in _shown_fields(w):
                groups[key].append(w.name)
    out = []
    for key, names in groups.items():
        f = next(f for w in model.worksheets if w.name == names[0] for f in w.filters if _filter_key(f) == key)
        dashboards = sorted({d for n in names for d in _dashboards_of(model, n)})
        out.append(Finding(
            code="L3", severity="warn", title="Filter copied onto charts that don't show that field",
            message=f"{len(names)} sheet(s) filter on {f.field.label} ({f.mode} {f.member_count} value(s)) without "
                    f"showing {f.field.label}; the same filter is on sheets that do show it, so it was probably "
                    "copied. These charts silently drop records with other values (e.g. unknown/null).",
            sheets=names, dashboards=dashboards,
            evidence={"field": f.field.label, "mode": f.mode, "members": f.members[:10]}))
    return out


def l4_misnamed_sheet(model: WorkbookModel, values: dict[tuple[str, str], set[str]] | None = None) -> list[Finding]:
    """`values` maps (datasource, field) -> known values (from data); filter members are always used too."""
    known: dict[tuple[str, str], set[str]] = defaultdict(set)
    for ws in model.worksheets:
        for f in ws.filters:
            known[(f.field.datasource, f.field.field)].update(f.members)
    for k, v in (values or {}).items():
        known[k].update(v)
    out = []
    for ws in model.worksheets:
        for f in ws.filters:
            if f.mode != "include" or len(f.members) != 1:
                continue
            chosen = f.members[0]
            name = ws.name.lower()
            if chosen.lower() in name:
                continue
            others = [v for v in known[(f.field.datasource, f.field.field)]
                      if v != chosen and len(v) >= 4 and v.lower() in name]
            if not others:
                continue
            named = max(others, key=len)
            title = (ws.title or "").strip()
            out.append(Finding(
                code="L4", severity="error", title="Sheet name doesn't match what it shows",
                message=f"Sheet '{ws.name}' is named after '{named}' but its filter on {f.field.label} keeps only "
                        f"'{chosen}'" + (f" (its title says '{title}')" if title and title != ws.name else "") +
                        ". Anyone finding charts by name gets the wrong one.",
                sheets=[ws.name], dashboards=_dashboards_of(model, ws.name),
                evidence={"field": f.field.label, "named_after": named, "filter_keeps": chosen, "title": title}))
    return out


def _norm(s: str) -> str:
    return re.sub(r"[\s_\-]+", "", s.lower())


def unit_companions(model: WorkbookModel) -> dict[tuple[str, str], str]:
    """(datasource, field) -> its unit/currency companion field, e.g. AGE -> AGE_UNIT."""
    out = {}
    for ds in model.datasources:
        phys = [f.internal_name for f in ds.fields if not f.is_calculated]
        for x in phys:
            for u in phys:
                if u == x:
                    continue
                nx, nu = _norm(x), _norm(u)
                if nu.startswith(nx) and nu[len(nx):] in UNIT_SUFFIXES:
                    out[(ds.name, x)] = u
    return out


def l5_unit_ignored(model: WorkbookModel, store=None, table_view: str | None = None,
                    entity: str | None = None) -> list[Finding]:
    comps = unit_companions(model)
    out = []
    for ds in model.datasources:
        calcs = [f for f in ds.fields if f.is_calculated]
        for (dsn, x), u in comps.items():
            if dsn != ds.name:
                continue
            aware = [c for c in calcs if {x, u} <= _formula_refs(c)]
            if not aware:
                continue
            for c in calcs:
                refs = _formula_refs(c)
                if x in refs and u not in refs:
                    ev = {"calc": c.caption, "field": x, "unit_field": u, "aware_calc": aware[0].caption,
                          "used_by_sheets": len(c.used_by_sheets)}
                    extra = ""
                    if store is not None and table_view:
                        mode = store.scalar(f"SELECT mode({qi(u)}) FROM {table_view}")
                        what = f"COUNT(DISTINCT {qi(entity)})" if entity else "COUNT(*)"
                        n = store.scalar(f"SELECT {what} FROM {table_view} WHERE {qi(u)} IS NOT NULL "
                                         f"AND {qi(u)} <> ?", [mode])
                        noun = f"{entity} value(s)" if entity else "row(s)"
                        ev |= {"usual_unit": mode, "affected": int(n or 0), "affected_unit": noun}
                        extra = f" {int(n or 0):,} {noun} have {u} other than '{mode}'."
                    out.append(Finding(
                        code="L5", severity="warn" if c.used_by_sheets else "info",
                        title="Calculation ignores a unit column",
                        message=f"'{c.caption}' uses {x} but not {u}, while '{aware[0].caption}' does use {u}: values "
                                f"recorded in other units are treated as if they were the usual unit.{extra}",
                        sheets=c.used_by_sheets,
                        dashboards=sorted({d for s in c.used_by_sheets for d in _dashboards_of(model, s)}),
                        evidence=ev))
    return out


def l6_unused_calcs(model: WorkbookModel) -> list[Finding]:
    by_caption = {f.caption: f for f in model.calculated_fields}
    used = {f.caption for f in model.calculated_fields if f.used_by_sheets}
    changed = True
    while changed:  # a calc used by a used calc is used
        changed = False
        for f in model.calculated_fields:
            if f.caption not in used and any(c in used for c in f.used_by_calcs):
                used.add(f.caption)
                changed = True
    unused = [c for c in by_caption if c not in used]
    if not unused:
        return []
    return [Finding(code="L6", severity="info", title="Unused calculated fields",
                    message=f"{len(unused)} calculated field(s) are defined but used by no sheet: "
                            f"{', '.join(unused)}. They would mislead if reused without checking.",
                    evidence={"calcs": unused})]


def l10_hidden(model: WorkbookModel) -> list[Finding]:
    hidden = [w.name for w in model.worksheets if w.hidden]
    if not hidden:
        return []
    visible = [w.name for w in model.worksheets if not w.hidden]
    return [Finding(code="L10", severity="info", title="Hidden worksheets",
                    message=f"{len(hidden)} of {len(model.worksheets)} worksheets are hidden; when published, only the "
                            f"{len(model.dashboards)} dashboard(s) and {len(visible)} visible sheet(s) become views.",
                    evidence={"hidden": len(hidden), "visible": visible})]


def l12_large_exclude(model: WorkbookModel, threshold: int = 20) -> list[Finding]:
    out = []
    for ws in model.worksheets:
        for f in ws.filters:
            if f.mode == "exclude" and f.member_count > threshold:
                out.append(Finding(code="L12", severity="info", title="Long exclude list",
                                   message=f"'{ws.name}' excludes {f.member_count} values of {f.field.label}: any new "
                                           "value in future data will appear automatically.",
                                   sheets=[ws.name], dashboards=_dashboards_of(model, ws.name),
                                   evidence={"field": f.field.label, "excluded": f.member_count}))
    return out


def l14_local_path(model: WorkbookModel) -> list[Finding]:
    return [Finding(code="L14", severity="info", title="Connection points to a local file",
                    message=f"Data source '{ds.caption or ds.name}' was built from a local file; its path "
                            "(which may include a user or client name) is stored in the workbook. "
                            + ("The packaged extract is a snapshot." if ds.has_extract else
                               "The copy packaged with the workbook is a snapshot of that file."),
                    evidence={"datasource": ds.caption or ds.name, "file": ds.connection_file})
            for ds in model.datasources if ds.connection_has_local_path]


def _effective(f: Filter) -> bool:
    """Does this filter restrict what a sheet shows? (Action filters are driven by clicks; leftovers do nothing.)"""
    if f.action_status is not None or f.keeps_all_values or f.mode == "all" or f.field.kind == "special":
        return False          # 'special' = Measure Names/Values: picks measures, not records
    if f.mode == "range":
        whole = PART_DOMAIN.get(f.field.derivation)
        lo, hi = _as_int(f.min), _as_int(f.max)
        if whole is not None and lo is not None and hi is not None and lo <= whole[0] and hi >= whole[1]:
            return False      # e.g. QUARTER between 1 and 4: keeps every value
        return f.min is not None or f.max is not None
    return f.mode in ("include", "exclude") and bool(f.members)


PART_DOMAIN = {"qr": (1, 4), "mn": (1, 12), "wd": (1, 7), "dy": (1, 31), "wk": (1, 53)}


def _as_int(v: str | None) -> int | None:
    try:
        return int(float((v or "").strip("#")))
    except ValueError:
        return None


def _num(v: str | None) -> str:
    v = (v or "").strip("#")
    try:
        x = float(v)
    except ValueError:
        return v
    return f"{x:,.3g}" if abs(x) < 1000 else f"{x:,.0f}"


def _short(f: Filter) -> str:
    vals = [("(empty)" if v == "%null%" else v) for v in f.members]
    if f.mode == "range":
        lo, hi = _num(f.min) if f.min is not None else None, _num(f.max) if f.max is not None else None
        return (f"between {lo} and {hi}" if lo is not None and hi is not None
                else f"at least {lo}" if lo is not None else f"at most {hi}")
    if f.mode == "include":
        return f"keeps {', '.join(vals)}" if len(vals) <= 3 else f"keeps {len(vals)} values"
    return f"excludes {', '.join(vals)}" if len(vals) <= 3 else f"excludes {len(vals)} values"


def filter_table(model: WorkbookModel, dashboard: str) -> dict[str, dict[str, str]]:
    """{filter field: {sheet: what that sheet's filter does, or '-' when the sheet has none}} for one dashboard.
    Only filters that restrict something count (not click-driven action filters, not keep-everything filters)."""
    d = next((x for x in model.dashboards if x.name == dashboard), None)
    if d is None:
        return {}
    sheets = [w for w in (model.worksheet(s) for s in d.sheets) if w is not None]
    table: dict[str, dict[str, str]] = {}
    for w in sheets:
        for f in w.filters:
            if _effective(f):
                table.setdefault(f.field.label, {})[w.name] = _short(f)
    for row in table.values():
        for w in sheets:
            row.setdefault(w.name, "-")
    return table


def l15_dashboard_filters_differ(model: WorkbookModel) -> list[Finding]:
    out = []
    for d in model.dashboards:
        sheets = [s for s in d.sheets if model.worksheet(s) is not None]
        if len(sheets) < 2:
            continue
        for field_label, row in filter_table(model, d.name).items():
            versions = defaultdict(list)
            for sheet, what in row.items():
                versions[what].append(sheet)
            if len(versions) < 2:
                continue
            missing = versions.get("-", [])
            if not missing and all(len(v) == 1 for v in versions.values()):
                continue       # every sheet filtered to its own value: one chart per value (a trellis), by design
            have = {w: v for w, v in versions.items() if w != "-"}
            parts = [f"{', '.join(v)} {w}" for w, v in have.items()]
            msg = (f"On dashboard '{d.name}' the filter on {field_label} is not the same on every sheet: "
                   + "; ".join(parts))
            if missing:
                filtered = [x for v in have.values() for x in v]
                who = filtered[0] if len(filtered) == 1 else "the filtered sheets"
                verb = "has" if len(missing) == 1 else "have"
                msg += (f"; {', '.join(missing)} {verb} no such filter, so "
                        f"{'its' if len(missing) == 1 else 'their'} numbers include what {who} "
                        f"{'leaves' if len(filtered) == 1 else 'leave'} out")
            out.append(Finding(code="L15", severity="info",
                               title="Sheets on one dashboard use different filters", message=msg + ".",
                               sheets=[s for v in have.values() for s in v] + missing, dashboards=[d.name],
                               evidence={"field": field_label, "by_sheet": row}))
    return out


def run_lints(lw) -> list[Finding]:
    """All rules on a LoadedWorkbook (uses data when available for L4 values and L5 counts)."""
    from tabbot.views.rebuild import field_values

    model = lw.model
    values: dict[tuple[str, str], set[str]] = {}
    if lw.store is not None:
        for ws in model.worksheets:
            for f in ws.filters:
                key = (f.field.datasource, f.field.field)
                if key not in values and f.mode == "include" and len(f.members) == 1:
                    ds = f.field.datasource
                    values[key] = set(field_values(model, f.field, lw.store, lw.table_for(ds), lw.profile_for(ds),
                                                   limit=5000))
    view = entity = None
    if lw.store is not None and len(lw.tables) == 1:
        view = lw.store.view()
        prof = lw.profiles.get(lw.tables[0].name)
        entity = prof.entity.field if prof is not None and prof.entity else None
    findings = (l4_misnamed_sheet(model, values) + l1_counts_dates(model) + l2_percent_without_percentage(model)
                + l3_copied_filter(model) + l5_unit_ignored(model, lw.store, view, entity) + l6_unused_calcs(model)
                + l12_large_exclude(model) + l10_hidden(model) + l14_local_path(model)
                + l15_dashboard_filters_differ(model))
    order = {"error": 0, "warn": 1, "info": 2}
    return sorted(findings, key=lambda f: order[f.severity])
