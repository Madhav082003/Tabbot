"""Numbers for the explanation, computed locally from the extract (never sent anywhere by this module).

Everything is chosen from how the workbook itself is built:
- the record = the field the sheets count with COUNT DISTINCT (data profile);
- the main date = the date column used (directly or through calculations) by the most sheets;
- the main category = the plain dimension used by the most sheets;
- "flags" = calculations of the form COUNTD(IF [F] = "v" THEN [record] END) (e.g. 'Serious Cases').
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

import pandas as pd

from tabbot.analysis import recipes as rc
from tabbot.data.store import qi
from tabbot.views.rebuild import RebuildResult, rebuild_worksheet

MAX_LIST = 5


@dataclass
class DataBasics:
    datasource: str
    table: str
    view: str
    rows: int
    entity: str | None
    entities: int
    rows_per_entity: float
    date_col: str | None = None
    date_sql: str | None = None
    date_from: dt.date | None = None
    date_to: dt.date | None = None
    varying: list[str] = field(default_factory=list)      # fields that change within one record
    constant: list[str] = field(default_factory=list)     # fields with one value per record
    sensitive: set[str] = field(default_factory=set)
    category: str | None = None                           # main category column


def _kit_ds(lw, ds_name):
    return next((d for d in lw.kit.datasources if d.name == ds_name), None) if lw.kit else None


def primary_datasource(lw) -> str | None:
    """The data source most sheets use (that has data)."""
    counts: dict[str, int] = {}
    for w in lw.model.worksheets:
        if w.datasource:
            counts[w.datasource] = counts.get(w.datasource, 0) + 1
    for name in sorted(counts, key=lambda n: -counts[n]):
        if lw.profile_for(name) is not None:
            return name
    return next((d.name for d in lw.model.datasources if lw.profile_for(d.name) is not None), None)


def basics(lw) -> DataBasics | None:
    ds = primary_datasource(lw)
    if ds is None or lw.store is None:
        return None
    prof = lw.profile_for(ds)
    table = lw.table_for(ds)
    view = lw.store.view(table)
    kds = _kit_ds(lw, ds)
    usage = {f.name: len(f.used_by) for f in kds.fields} if kds else {}
    direct = {f.name: len(f.used_directly_by) for f in kds.fields} if kds else {}
    # a "record" is what the workbook itself mostly counts with COUNT DISTINCT. A guessed id, or one sheet's
    # COUNTD(Customer Name) in a workbook that mostly sums Sales, would turn every number into "customers":
    # then one row is one record.
    ent = prof.entity if prof.entity is not None and prof.entity.method == "workbook_countd" else None
    if ent is not None:
        measure_sheets = countd_sheets = 0
        for w in lw.model.worksheets:
            if w.datasource != ds:
                continue
            refs = [r for r in w.all_refs() if r.kind == "measure"]
            if refs:
                measure_sheets += 1
                countd_sheets += any(r.aggregation == "count_distinct" and r.field == ent.field for r in refs)
        if measure_sheets and countd_sheets / measure_sheets < 0.5:
            ent = None
    b = DataBasics(datasource=ds, table=table, view=view, rows=prof.rows, entity=ent.field if ent else None,
                   entities=ent.distinct if ent else prof.rows, rows_per_entity=ent.rows_per_entity if ent else 1.0,
                   sensitive=set(prof.sensitive_suggestions))
    # main date: the date column most used by sheets (directly or via calculations)
    dates = [c for c in prof.columns if c.kind in ("date", "text_date") and (ent is None or c.entity_level is not False)]
    if dates:
        c = max(dates, key=lambda c: (usage.get(c.name, 0), -dates.index(c)))
        b.date_col = c.name
        b.date_sql = (f"CAST(try_strptime({qi(c.name)}, '{c.date_format.format}') AS TIMESTAMP)" if c.date_format
                      else f"CAST({qi(c.name)} AS TIMESTAMP)")
        lo, hi = lw.store.con.execute(f"SELECT CAST(MIN({b.date_sql}) AS DATE), CAST(MAX({b.date_sql}) AS DATE) "
                                      f"FROM {view}").fetchone()
        b.date_from, b.date_to = lo, hi
    # which fields vary inside one record, which are fixed per record (only fields the workbook uses)
    used = [c for c in prof.columns if c.name != b.entity and c.name not in b.sensitive
            and c.kind in ("categorical", "high_cardinality", "text_date", "date", "numeric")]
    used.sort(key=lambda c: -usage.get(c.name, 0))
    b.varying = [c.name for c in used if c.entity_level is False and usage.get(c.name, 0)][:6]
    b.constant = [c.name for c in used if c.entity_level and usage.get(c.name, 0)
                  and c.kind != "text_date"][:6]
    # main category: plain dimension used directly by the most sheets
    cands = [c for c in prof.columns if c.kind in ("categorical", "high_cardinality") and c.name not in b.sensitive
             and c.name != b.entity and 2 <= c.distinct <= 200 and direct.get(c.name, 0)]
    if cands:
        b.category = max(cands, key=lambda c: direct.get(c.name, 0)).name
    return b


def breakdown(lw, b: DataBasics, col: str, limit: int = MAX_LIST) -> tuple[list[tuple[str, int]], int]:
    """Top values of a column by number of records (a record counts once per value it has)."""
    ent = qi(b.entity) if b.entity else "*"
    measure = f"COUNT(DISTINCT {ent})" if b.entity else "COUNT(*)"
    rows = lw.store.con.execute(
        f"SELECT CAST({qi(col)} AS VARCHAR) AS v, {measure} AS n FROM {b.view} GROUP BY 1 ORDER BY 2 DESC, 1 "
        f"LIMIT {limit}").fetchall()
    distinct = lw.store.scalar(f"SELECT COUNT(DISTINCT {qi(col)}) FROM {b.view}")
    return [("(empty)" if v is None else v, int(n)) for v, n in rows], int(distinct or 0)


def workbook_context(lw, b: DataBasics) -> rc.AnalysisContext | None:
    if not b.date_sql:
        return None
    measure = f"COUNT(DISTINCT {qi(b.entity)})" if b.entity else "COUNT(*)"
    return rc.AnalysisContext(
        store=lw.store, view=b.view, where=[], params=[], measure_sql=measure,
        measure_label=f"records ({b.entity})" if b.entity else "rows", date_sql=b.date_sql, date_label=b.date_col,
        grain="quarter", category_sql=qi(b.category) if b.category else None, category_label=b.category,
        entity_sql=qi(b.entity) if b.entity else None, entity_label=b.entity, profile=lw.profile_for(b.datasource))


def per_year(lw, b: DataBasics) -> list[tuple[int, int]]:
    if not b.date_sql:
        return []
    measure = f"COUNT(DISTINCT {qi(b.entity)})" if b.entity else "COUNT(*)"
    rows = lw.store.con.execute(f"SELECT year({b.date_sql}) y, {measure} FROM {b.view} WHERE {b.date_sql} IS NOT NULL "
                                f"GROUP BY 1 ORDER BY 1").fetchall()
    return [(int(y), int(n)) for y, n in rows]


FLAG_RE = re.compile(r"^\s*COUNTD\(\s*IF\s+\[([^\]]+)\]\s*=\s*['\"]([^'\"]+)['\"]\s+THEN\s+\[([^\]]+)\]\s*END\s*\)\s*$",
                     re.I | re.S)


def flag_calcs(lw, b: DataBasics) -> list[tuple[str, str, str]]:
    """(calc caption, field, value) for calcs that count records with one field value - e.g. 'Serious Cases'."""
    kds = _kit_ds(lw, b.datasource)
    out = []
    if not kds or not b.entity:
        return out
    cols = {c.name for c in lw.profile_for(b.datasource).columns}
    for f in kds.fields:
        if f.kind != "calculated" or not f.formula:
            continue
        m = FLAG_RE.match(f.formula)
        if m and m.group(3) == b.entity and m.group(1) in cols and m.group(1) not in b.sensitive:
            out.append((f.caption or f.name, m.group(1), m.group(2)))
    return out


def rebuild(lw, sheet_name: str) -> RebuildResult | None:
    ws = lw.model.worksheet(sheet_name)
    if ws is None or lw.store is None:
        return None
    return rebuild_worksheet(lw.model, ws, lw.store, lw.table_for(ws.datasource), lw.profile_for(ws.datasource))


def values_in_data(lw, ds: str | None, column: str, limit: int = 500) -> list[str] | None:
    """Distinct values of a physical column (for 'this filter leaves only ...' sentences)."""
    prof = lw.profile_for(ds) if lw.store is not None else None
    if prof is None or prof.column(column) is None or prof.column(column).distinct > limit:
        return None
    view = lw.store.view(lw.table_for(ds))
    return [r[0] for r in lw.store.con.execute(
        f"SELECT DISTINCT CAST({qi(column)} AS VARCHAR) FROM {view} WHERE {qi(column)} IS NOT NULL").fetchall()]


def excluded_by_include_filter(lw, b: DataBasics, column: str, members: list[str]) -> tuple[int, int] | None:
    """How many records an include filter on `column` drops (records with any other value or empty)."""
    if not b.entity or column not in {c.name for c in lw.profile_for(b.datasource).columns}:
        return None
    vals = [m for m in members if m != "%null%"]
    ph = ", ".join("?" for _ in vals) or "NULL"
    kept = lw.store.scalar(f"SELECT COUNT(DISTINCT {qi(b.entity)}) FROM {b.view} WHERE CAST({qi(column)} AS VARCHAR) "
                           f"IN ({ph})" + (f" OR {qi(column)} IS NULL" if "%null%" in members else ""), vals)
    return b.entities - int(kept or 0), b.entities


def series_change(df: pd.DataFrame, date_col: str, value_col: str, data_end: dt.date | None,
                  n: int = 4) -> tuple[float, float] | None:
    """Last n full quarters vs the n before, from a rebuilt quarterly series."""
    if df.empty:
        return None
    s = df[[date_col, value_col]].copy()
    s[date_col] = pd.to_datetime(s[date_col])
    s = s.groupby(date_col)[value_col].sum().sort_index()
    if data_end is not None and len(s):
        last = s.index[-1]
        end = (last + pd.offsets.QuarterEnd(0)).date()
        if data_end < end - dt.timedelta(days=7):
            s = s.iloc[:-1]
    if len(s) < 2 * n:
        return None
    return float(s.iloc[-2 * n:-n].sum()), float(s.iloc[-n:].sum())
