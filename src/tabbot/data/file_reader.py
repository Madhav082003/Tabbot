"""Read data sources that connect straight to a file in the package (Excel .xls/.xlsx, CSV/text) into Parquet.

Mapping, as Tableau does it:
- an Excel relation table '[Orders$]' is the sheet 'Orders' (a name without '$' is a named range: not read);
- a CSV relation is the file itself;
- column names become the workbook's local names (metadata-record remote-name -> local-name, by parent table),
  e.g. People.Region -> 'Region (People)';
- a data source with several tables (logical layer) becomes one table: the root table LEFT JOINed to each related
  table on the workbook's own relationship, but only where the related side has one row per key (many-to-one),
  so no row of the root table is ever duplicated. Tables that cannot be joined safely stay separate tables.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from tabbot.data.hyper_reader import TableInfo, _safe
from tabbot.data.store import qi

REL_EQ = re.compile(r"^\s*\[(.+?)\]\s*=\s*\[(.+?)\]\s*$")


@dataclass
class FileLoad:
    tables: list[TableInfo] = field(default_factory=list)
    main: str | None = None            # TableInfo.name holding the data source's (joined) data
    notes: list[str] = field(default_factory=list)


def sheet_of(table: str) -> str | None:
    """'[Orders$]' -> 'Orders'; "['MARKETED CASES$']" -> 'MARKETED CASES'; '[Range1]' -> None (named range)."""
    t = table.strip()
    if t.startswith("[") and t.endswith("]"):
        t = t[1:-1].replace("]]", "]")
    t = t.strip("'")
    return t[:-1] if t.endswith("$") else None


def read_table(path: Path, cls: str, table: str) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if cls == "textscan" or suffix in (".csv", ".txt", ".tsv"):
        sep = "\t" if suffix == ".tsv" else None
        return pd.read_csv(path, sep=sep, engine="python", encoding="utf-8-sig", encoding_errors="replace")
    sheet = sheet_of(table)
    if sheet is None:
        raise ValueError(f"'{table}' is a named range, not a sheet")
    engine = "xlrd" if suffix == ".xls" else "openpyxl"
    return pd.read_excel(path, sheet_name=sheet, engine=engine)


def _to_arrow(df: pd.DataFrame) -> pa.Table:
    df = df.copy()
    df.columns = [str(c) for c in df.columns]
    for c in df.columns:
        if df[c].dtype == object:
            kinds = {type(v) for v in df[c].dropna().head(5000)}
            if len(kinds) > 1 or (kinds and not kinds <= {str}):
                df[c] = df[c].map(lambda v: None if pd.isna(v) else str(v))
    return pa.Table.from_pandas(df, preserve_index=False)


def _local_names(kds, rel_name: str | None) -> dict[str, str]:
    """remote column name -> workbook local name for one table."""
    parents = {f"[{rel_name}]", rel_name} if rel_name else set()
    out = {}
    for p in kds.physical_fields:
        if p.remote_name and p.local_name and (not parents or (p.parent or "").strip() in parents):
            out.setdefault(p.remote_name, p.local_name.strip()[1:-1].replace("]]", "]")
                           if p.local_name.strip().startswith("[") else p.local_name.strip())
    return out


def _rel_names(kds) -> dict[str, str]:
    """relation table ('[Orders$]') -> relation name ('Orders'), from the physical and logical layers."""
    out: dict[str, str] = {}

    def walk(r):
        if r is None:
            return
        if r.type == "table" and r.table and r.name:
            out.setdefault(r.table, r.name)
        for c in r.children:
            walk(c)
    walk(kds.relation)
    for o in kds.objects:
        walk(o)
    return out


def load_file_datasource(kds, links, unpacked: Path, out_dir: Path) -> FileLoad:
    """Read every table of one data source's linked files; return the tables and which one is its data."""
    res = FileLoad()
    label = kds.caption or kds.name
    names = _rel_names(kds)
    frames: dict[str, pd.DataFrame] = {}          # relation name -> frame (local column names)
    for lk in links:
        path = unpacked / lk.member
        for t in lk.tables or [""]:
            rel = names.get(t) or (posixpath.splitext(posixpath.basename(lk.member))[0] if not t else t)
            try:
                df = read_table(path, lk.cls or "", t)
            except Exception as e:  # noqa: BLE001 - report the table, keep the others
                res.notes.append(f"{label}: could not read '{sheet_of(t) or t}' from "
                                 f"{posixpath.basename(lk.member)} ({type(e).__name__})")
                continue
            df = df.rename(columns={c: n for c, n in _local_names(kds, rel).items() if c in df.columns})
            frames[rel] = df
    if not frames:
        return res
    con = duckdb.connect(":memory:")
    try:
        regs = {}
        for i, (rel, df) in enumerate(frames.items()):
            con.register(f"r_{i}", _to_arrow(df))
            regs[rel] = f"r_{i}"
        root, joins = _plan(kds, frames, con, regs, res, label)
        sql = f"SELECT * FROM {regs[root]} AS a"
        for i, (rel, left, right) in enumerate(joins):
            sql += (f" LEFT JOIN (SELECT DISTINCT * FROM {regs[rel]}) AS j{i} "
                    f"ON a.{qi(left)} = j{i}.{qi(right)}")
        rel_ = con.sql(sql)
        joined = rel_.to_arrow_table() if hasattr(rel_, "to_arrow_table") else rel_.arrow()
    finally:
        con.close()
    stem = _safe(f"{label}")
    res.main = _write(res, joined, f'"{label}"', label, out_dir / f"{stem}.parquet",
                      "+".join([root] + [j[0] for j in joins]))
    for rel, df in frames.items():
        if rel != root and rel not in {j[0] for j in joins}:
            _write(res, _to_arrow(df), f'"{label}"."{rel}"', rel, out_dir / f"{stem}.{_safe(rel)}.parquet", rel)
    return res


def _plan(kds, frames, con, regs, res: FileLoad, label: str):
    """Root table and the many-to-one joins [(relation, left column, right column)] from the relationships."""
    by_id = {o.object_id: (o.name or "") for o in kds.objects if o.object_id}
    rights = {r.right_object for r in kds.relationships}
    roots = [by_id[r.left_object] for r in kds.relationships if r.left_object not in rights and r.left_object in by_id]
    root = next((r for r in roots if r in frames), None) or next(iter(frames))
    joins = []
    for r in kds.relationships:
        left, right = by_id.get(r.left_object), by_id.get(r.right_object)
        m = REL_EQ.match(r.expression or "")
        if left != root or right not in frames or not m:
            if right in frames and left == root:
                res.notes.append(f"{label}: relationship to '{right}' is not a simple 'a = b' and was not joined")
            continue
        lcol, rcol = m.group(1), m.group(2)
        if lcol not in frames[root].columns or rcol not in frames[right].columns:
            res.notes.append(f"{label}: relationship columns {lcol} / {rcol} not found; '{right}' kept separate")
            continue
        rv = regs[right]
        dup = con.execute(f"SELECT COUNT(*) FROM (SELECT {qi(rcol)} FROM (SELECT DISTINCT * FROM {rv}) "
                          f"GROUP BY 1 HAVING COUNT(*) > 1)").fetchall()[0][0]
        if dup:
            res.notes.append(f"{label}: '{right}' has several rows per {rcol}; kept as a separate table (joining "
                             "would duplicate rows)")
            continue
        joins.append((right, lcol, rcol))
    return root, joins


def _write(res: FileLoad, table: pa.Table, name: str, tname: str, out: Path, source: str) -> str:
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, out)
    res.tables.append(TableInfo(name=name, schema_name="", table=tname, source_file=source, parquet=str(out.resolve()),
                                rows=table.num_rows, columns=[(f.name, str(f.type)) for f in table.schema]))
    return name
