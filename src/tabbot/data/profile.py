"""Understand an extracted table without assuming anything about the workbook.

- column profiles (type, nulls, distinct, average text length, kind)
- text date formats (e.g. 30-SEP-2021 -> %d-%b-%Y)
- the entity to count (what one "record" is) and rows per entity
- which columns are constant per entity (entity-level) vs vary across its rows (row-level)
- suggested sensitive columns (name patterns, free text, ids)

Values stay local: profiles hold counts and formats, not data values.
"""

from __future__ import annotations

import re
from collections import Counter

from pydantic import BaseModel

from tabbot.config import PrivacySettings
from tabbot.data.store import Store, qi
from tabbot.twb.model import WorkbookModel

DATE_FORMATS = ["%d-%b-%Y", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y%m%d", "%d.%m.%Y", "%d-%b-%y",
                "%b %d, %Y", "%d %b %Y", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S",
                "%m/%d/%Y %I:%M:%S %p", "%Y-%m-%dT%H:%M:%S"]
ID_NAME = re.compile(r"(^|[^a-z])(id|key|no|nbr|num|number|code)$|id$", re.I)
CATEGORICAL_MAX = 50
SAMPLE_DISTINCT = 5000


class DateFormat(BaseModel):
    format: str
    parsed_share: float
    ambiguous_with: str | None = None    # e.g. %d/%m/%Y when %m/%d/%Y also fits


class ColumnProfile(BaseModel):
    name: str
    dtype: str
    nulls: int
    distinct: int
    avg_len: float | None = None
    kind: str                            # id | categorical | high_cardinality | free_text | numeric | date | text_date
    date_format: DateFormat | None = None
    entity_level: bool | None = None     # constant within each entity
    entities_varying: int | None = None  # entities with >1 distinct value
    sensitive_reasons: list[str] = []


class EntityInfo(BaseModel):
    field: str
    method: str                          # "workbook_countd" | "id_heuristic" | "none"
    evidence: str
    distinct: int
    rows_per_entity: float


class TableProfile(BaseModel):
    table: str
    rows: int
    columns: list[ColumnProfile]
    entity: EntityInfo | None = None

    def column(self, name: str) -> ColumnProfile | None:
        return next((c for c in self.columns if c.name == name), None)

    @property
    def date_columns(self) -> list[str]:
        return [c.name for c in self.columns if c.kind in ("date", "text_date")]

    @property
    def sensitive_suggestions(self) -> list[str]:
        return [c.name for c in self.columns if c.sensitive_reasons]


def _is_text(dtype: str) -> bool:
    return dtype.upper().startswith(("VARCHAR", "TEXT", "STRING"))


def _is_numeric(dtype: str) -> bool:
    return dtype.upper().split("(")[0] in {"BIGINT", "INTEGER", "SMALLINT", "TINYINT", "HUGEINT", "DOUBLE",
                                            "FLOAT", "REAL", "DECIMAL", "UBIGINT", "UINTEGER"}


def _is_date(dtype: str) -> bool:
    return dtype.upper().startswith(("DATE", "TIMESTAMP"))


def detect_date_format(store: Store, view: str, col: str) -> DateFormat | None:
    c = qi(col)
    sample = (f"(SELECT DISTINCT {c} AS v FROM {view} WHERE {c} IS NOT NULL "
              f"AND regexp_matches({c}, '[0-9]') LIMIT {SAMPLE_DISTINCT})")
    n = store.scalar(f"SELECT COUNT(*) FROM {sample}")
    if not n:
        return None
    exprs = ", ".join(
        f"AVG(CASE WHEN try_strptime(v, '{f}') IS NOT NULL "
        f"AND year(try_strptime(v, '{f}')) BETWEEN 1900 AND 2100 THEN 1 ELSE 0 END)" for f in DATE_FORMATS)
    shares = store.con.execute(f"SELECT {exprs} FROM {sample}").fetchone()
    scored = sorted(zip(DATE_FORMATS, shares, strict=True), key=lambda x: -x[1])
    best_fmt, best = scored[0]
    if best < 0.99:
        return None
    ambiguous = None
    pairs = {"%m/%d/%Y": "%d/%m/%Y", "%d/%m/%Y": "%m/%d/%Y",
             "%m/%d/%Y %H:%M:%S": "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S": "%m/%d/%Y %H:%M:%S"}
    if best_fmt in pairs and dict(scored)[pairs[best_fmt]] >= 0.99:
        ambiguous = pairs[best_fmt]
    return DateFormat(format=best_fmt, parsed_share=round(float(best), 4), ambiguous_with=ambiguous)


def profile_table(store: Store, table_name: str, privacy: PrivacySettings | None = None) -> TableProfile:
    privacy = privacy or PrivacySettings()
    view = store.view(table_name)
    cols = store.columns(table_name)
    rows = int(store.scalar(f"SELECT COUNT(*) FROM {view}"))

    parts = []
    for name, dtype in cols:
        c = qi(name)
        avg = f"AVG(LENGTH({c}))" if _is_text(dtype) else "NULL"
        parts.append(f"COUNT(*) - COUNT({c}), COUNT(DISTINCT {c}), {avg}")
    stats = store.con.execute(f"SELECT {', '.join(parts)} FROM {view}").fetchone() if cols else []

    patterns = [re.compile(p, re.I) for p in privacy.sensitive_column_patterns]
    ft = privacy.free_text_detection
    profiles = []
    for i, (name, dtype) in enumerate(cols):
        nulls, distinct, avg_len = int(stats[3 * i]), int(stats[3 * i + 1]), stats[3 * i + 2]
        nonnull = max(rows - nulls, 1)
        p = ColumnProfile(name=name, dtype=dtype, nulls=nulls, distinct=distinct,
                          avg_len=round(float(avg_len), 1) if avg_len is not None else None, kind="categorical")
        if _is_date(dtype):
            p.kind = "date"
        elif _is_text(dtype):
            if avg_len is not None and avg_len >= ft.min_avg_length and distinct / nonnull >= ft.min_distinct_ratio:
                p.kind = "free_text"
            else:
                p.date_format = detect_date_format(store, view, name) if distinct > 1 else None
                if p.date_format:
                    p.kind = "text_date"
                elif ID_NAME.search(name.strip()) and distinct > CATEGORICAL_MAX:
                    p.kind = "id"
                elif distinct <= CATEGORICAL_MAX:
                    p.kind = "categorical"
                else:
                    p.kind = "high_cardinality"
        elif _is_numeric(dtype):
            p.kind = "id" if ID_NAME.search(name.strip()) and distinct > CATEGORICAL_MAX else "numeric"

        if any(rx.search(name) for rx in patterns):
            p.sensitive_reasons.append("name matches a sensitive pattern")
        if p.kind == "free_text":
            p.sensitive_reasons.append("free text")
        if p.kind == "id":
            p.sensitive_reasons.append("identifier (count only)")
        if name in privacy.sensitive_columns:
            p.sensitive_reasons.append("marked sensitive in config")
        profiles.append(p)
    return TableProfile(table=table_name, rows=rows, columns=profiles)


def detect_entity(model: WorkbookModel, datasource: str | None, profile: TableProfile) -> EntityInfo | None:
    """What one counted 'record' is. Prefer the workbook's own COUNT DISTINCT measures."""
    colnames = {c.name for c in profile.columns}
    counts: Counter[str] = Counter()
    for ws in model.worksheets:
        if datasource and ws.datasource and ws.datasource != datasource:
            continue
        for ref in ws.all_refs():
            if ref.aggregation == "count_distinct" and ref.field in colnames:
                counts[ref.field] += 1
    if counts:
        field, n = counts.most_common(1)[0]
        col = profile.column(field)
        return EntityInfo(field=field, method="workbook_countd",
                          evidence=f"used in COUNT DISTINCT by {n} worksheet reference(s)",
                          distinct=col.distinct, rows_per_entity=round(profile.rows / max(col.distinct, 1), 2))

    candidates = [c for c in profile.columns if c.kind == "id"]
    if not candidates:
        return None
    # an entity usually repeats over several rows; prefer the id with most distinct values among those
    candidates.sort(key=lambda c: (profile.rows / max(c.distinct, 1) > 1, c.distinct), reverse=True)
    col = candidates[0]
    return EntityInfo(field=col.name, method="id_heuristic", evidence="id-like name and many distinct values",
                      distinct=col.distinct, rows_per_entity=round(profile.rows / max(col.distinct, 1), 2))


def entity_consistency(store: Store, profile: TableProfile) -> None:
    """Mark each column entity-level (one value per entity) or row-level. Updates the profile in place."""
    if profile.entity is None:
        return
    ent = qi(profile.entity.field)
    targets = [c for c in profile.columns if c.name != profile.entity.field and c.kind != "free_text"]
    if not targets:
        return
    inner = ", ".join(f"COUNT(DISTINCT {qi(c.name)}) AS c{i}" for i, c in enumerate(targets))
    outer = ", ".join(f"COUNT(*) FILTER (WHERE c{i} > 1)" for i in range(len(targets)))
    res = store.con.execute(f"SELECT {outer} FROM (SELECT {inner} FROM {store.view(profile.table)} "
                            f"GROUP BY {ent}) s").fetchone()
    for c, n in zip(targets, res, strict=True):
        c.entities_varying = int(n)
        c.entity_level = n == 0
