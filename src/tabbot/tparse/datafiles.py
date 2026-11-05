"""Link each data source to the file inside the package that holds its data (added for tabbot).

- An extract: <extract><connection class='hyper' dbname='Data/Extracts/x.hyper'> -> that .hyper member.
- A file connection (Excel, CSV ...): named-connection/connection@filename (CSV: @directory + @filename) matched
  to a package member by full path, then by path suffix, then by file name.
- Data files in the package that no data source uses are listed; anything under 'Local Data/' is Tableau's
  custom geocoding (map locations), never the workbook's own data.

The declared path can be an absolute path on the author's computer (user names): it is kept in the model for
matching but tabbot only ever shows the file name.
"""

from __future__ import annotations

import posixpath

from .model import DataFileLink, DataSource, Relation, WorkbookModel

DATA_SUFFIXES = (".hyper", ".tde", ".xls", ".xlsx", ".xlsm", ".csv", ".txt", ".tsv", ".json", ".mdb", ".accdb",
                 ".sas7bdat", ".sav", ".kml", ".shp", ".geojson")
FILE_CLASSES = {"excel-direct", "excel", "textscan", "hyper", "dataengine"}
GEOCODING_DIR = "local data/"


def _norm(p: str | None) -> str:
    p = (p or "").replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p.lower()


def match_member(declared: str, members: list[str]) -> tuple[str | None, str]:
    """(member, how) for a declared path: exact path, then path suffix, then unique file name."""
    d = _norm(declared)
    if not d:
        return None, ""
    by_norm = {_norm(m): m for m in members}
    if d in by_norm:
        return by_norm[d], "path"
    suffix = [m for n, m in by_norm.items() if d.endswith("/" + n) or n.endswith("/" + d)]
    if len(suffix) == 1:
        return suffix[0], "suffix"
    base = posixpath.basename(d)
    named = [m for n, m in by_norm.items() if posixpath.basename(n) == base]
    if len(named) == 1:
        return named[0], "name"
    if len(named) > 1:   # prefer the one outside Local Data/
        own = [m for m in named if not _norm(m).startswith(GEOCODING_DIR)]
        if len(own) == 1:
            return own[0], "name"
    return None, ""


def _tables(rel: Relation | None, conn: str | None, out: list[str]) -> list[str]:
    if rel is None:
        return out
    if rel.type == "table" and rel.table and (conn is None or rel.connection in (None, conn)) \
            and rel.table not in out:
        out.append(rel.table)
    for c in rel.children:
        _tables(c, conn, out)
    return out


def _links_for(ds: DataSource, members: list[str]) -> list[DataFileLink]:
    out: list[DataFileLink] = []
    if ds.extract is not None and ds.extract.enabled:
        declared = ds.extract.dbname or ""
        m, how = match_member(declared, members) if declared else (None, "")
        out.append(DataFileLink(datasource=ds.name, cls="hyper", via="extract", declared=declared, member=m,
                                match=how, tables=list(ds.extract.hyper_tables or ds.extract.tables)))
    for c in ds.connections:
        if c.cls not in FILE_CLASSES or not c.filename:
            continue
        declared = posixpath.join(c.directory.replace("\\", "/"), c.filename) if c.directory else c.filename
        m, how = match_member(declared, members)
        tables: list[str] = []
        _tables(ds.relation, c.name, tables)
        for o in ds.objects:
            _tables(o, c.name, tables)
        out.append(DataFileLink(datasource=ds.name, connection=c.name, cls=c.cls, via="file", declared=declared,
                                member=m, match=how, tables=tables))
    return out


def link_data_files(model: WorkbookModel, members: list[str]) -> None:
    model.data_links = []
    for ds in model.datasources:
        if not ds.is_parameters:
            model.data_links.extend(_links_for(ds, members))
    used = {lk.member for lk in model.data_links if lk.member}
    data = [m for m in members if m.lower().endswith(DATA_SUFFIXES)]
    model.geocoding_files = [m for m in data if _norm(m).startswith(GEOCODING_DIR)]
    model.unlinked_data_files = [m for m in data if m not in used and m not in model.geocoding_files]


def describe_links(model: WorkbookModel, name_missing: bool = True) -> list[str]:
    """Plain sentences: which file each data source reads, what is missing, what is unused.
    name_missing=False hides the names of files that are not in the package (the original source file name can
    carry a client or user name); use that for anything sent off this computer."""
    lines = []
    for lk in model.data_links:
        ds = model.datasource(lk.datasource)
        label = ds.label if ds else lk.datasource
        name = posixpath.basename(lk.declared.replace("\\", "/")) or "(no file name)"
        ext = posixpath.splitext(name)[1]
        shown = f"'{name}'" if name_missing else (f"its {ext} file" if ext else "its source file")
        what = "extract" if lk.via == "extract" else (lk.cls or "file")
        has_extract = any(x.datasource == lk.datasource and x.via == "extract" and x.member
                          for x in model.data_links)
        if lk.member:
            lines.append(f"{label}: reads {what} '{lk.member}' (inside the package)")
        elif lk.via == "file" and has_extract:
            lines.append(f"{label}: built from {shown}, which is not packaged; the packaged extract holds a "
                         "snapshot of its data")
        else:
            lines.append(f"{label}: {what} file {shown} is NOT in the package - its data cannot be read here")
    if model.unlinked_data_files:
        lines.append("data files in the package that no data source uses: " + ", ".join(model.unlinked_data_files))
    if model.geocoding_files:
        lines.append(f"{len(model.geocoding_files)} file(s) under 'Local Data/' are Tableau custom geocoding "
                     "(map locations), not the workbook's data")
    return lines
