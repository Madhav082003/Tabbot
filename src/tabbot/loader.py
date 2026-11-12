"""One call to go from an uploaded file to everything the app needs: model, data store, profiles."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from tabbot.config import Settings, get_settings
from tabbot.data.hyper_reader import TableInfo, export_to_parquet
from tabbot.data.profile import (
    TableProfile,
    detect_entity,
    entity_consistency,
    profile_table,
    recheck_name_matches,
)
from tabbot.data.store import Store
from tabbot.twb.model import WorkbookModel
from tabbot.twb.parser import parse_both
from tabbot.workspace import Package, Workspace, delete_workspace, new_workspace, open_upload

FILE_CLASSES = {"excel-direct", "textscan"}


@dataclass
class LoadedWorkbook:
    source_name: str
    workspace: Workspace
    package: Package
    model: WorkbookModel
    kit: object = None                    # the parser kit's rich model (tabbot.tparse.model.WorkbookModel)
    tables: list[TableInfo] = field(default_factory=list)
    store: Store | None = None
    profiles: dict[str, TableProfile] = field(default_factory=dict)   # by table name
    notes: list[str] = field(default_factory=list)
    ds_tables: dict[str, str] = field(default_factory=dict)          # data source name -> table holding its data
    data_files: list[str] = field(default_factory=list)             # plain sentences: which file feeds what
    data_files_shared: list[str] = field(default_factory=list)      # same, missing file names hidden (for Claude)

    def table_for(self, datasource: str | None) -> str | None:
        """The table that holds a data source's data - only ever from a file that data source is linked to."""
        if datasource is None:
            return self.tables[0].name if len(self.tables) == 1 else None
        return self.ds_tables.get(datasource)

    def profile_for(self, datasource: str | None) -> TableProfile | None:
        t = self.table_for(datasource)
        return self.profiles.get(t) if t else None

    def close(self, delete: bool = False) -> None:
        """Release the data store; with delete=True also remove the local copy of the workbook and its data."""
        if self.store is not None:
            self.store.close()
            self.store = None
        if delete:
            delete_workspace(self.workspace)


def _add(lw: LoadedWorkbook, tables: list[TableInfo], prefix: str) -> list[TableInfo]:
    """Register tables, renaming any whose name is already taken (two extracts can both hold "Extract"."Extract")."""
    taken = {t.name for t in lw.tables}
    out = []
    for t in tables:
        if t.name in taken:
            t = t.model_copy(update={"name": f'"{prefix}".{t.name}'})
        taken.add(t.name)
        lw.tables.append(t)
        out.append(t)
    return out


def _load_data(lw: LoadedWorkbook, ws: Workspace, extra_extracts: list[Path] | None) -> None:
    """Load each data source's data from the file it is linked to (extract, Excel or CSV) - nothing else."""
    from tabbot.data.file_reader import load_file_datasource
    from tabbot.tparse.datafiles import describe_links

    kit = lw.kit
    lw.data_files = describe_links(kit)
    lw.data_files_shared = describe_links(kit, name_missing=False)
    hyper_done: dict[str, list[TableInfo]] = {}
    for kds in kit.datasources:
        if kds.is_parameters:
            continue
        label = kds.caption or kds.name
        links = [lk for lk in kit.data_links if lk.datasource == kds.name]
        ext = next((lk for lk in links if lk.via == "extract" and lk.member), None)
        if ext is not None and ext.member.lower().endswith(".tde"):
            lw.notes.append(f"{label}: old-format .tde extract cannot be read")
            ext = None
        if ext is not None:
            if ext.member not in hyper_done:
                got = export_to_parquet([ws.unpacked / ext.member], ws.parquet, ws.logs)
                hyper_done[ext.member] = _add(lw, got, Path(ext.member).stem)
            tabs = hyper_done[ext.member]
            eng = lw.model.datasource(kds.name)
            want = set(eng.extract_tables if eng else [])
            hit = next((t for t in tabs if t.name in want or f'"{t.schema_name}"."{t.table}"' in want), None)
            if hit is None and len(tabs) == 1:
                hit = tabs[0]
            if hit is not None:
                lw.ds_tables[kds.name] = hit.name
            else:
                lw.notes.append(f"{label}: its extract holds {len(tabs)} tables; sheets using it cannot be redrawn")
            continue
        files = [lk for lk in links if lk.via == "file" and lk.member and (lk.cls or "") in FILE_CLASSES]
        if files:
            fl = load_file_datasource(kds, files, ws.unpacked, ws.parquet)
            lw.notes.extend(fl.notes)
            added = _add(lw, fl.tables, label)
            if fl.main is not None:
                lw.ds_tables[kds.name] = added[[t.name for t in fl.tables].index(fl.main)].name
            continue
        missing = [lk for lk in links if not lk.member]
        if missing:
            # no file name here: notes reach Claude, and the original file name can carry a client name
            lw.notes.append(f"{label}: its data file is not in the package, so its numbers cannot be computed here")
        elif kds.is_published:
            lw.notes.append(f"{label}: a published (server) data source - no local data")
    # extracts handed in separately (CLI --extract) for a .twb without a package
    if not lw.tables and extra_extracts:
        got = _add(lw, export_to_parquet([ws.unpacked / Path(e).name for e in extra_extracts], ws.parquet, ws.logs),
                   "extra")
        for d in lw.model.datasources:
            hit = next((t for t in got if t.name in d.extract_tables), None)
            if hit is not None:
                lw.ds_tables[d.name] = hit.name


def load_workbook(path: Path | str, settings: Settings | None = None,
                  extra_extracts: list[Path] | None = None) -> LoadedWorkbook:
    settings = settings or get_settings()
    path = Path(path)
    ws = new_workspace(settings.workspace_dir)
    lw: LoadedWorkbook | None = None
    try:
        pkg = open_upload(path, ws, extra_extracts, settings.max_unzipped_mb)
        # parse the uploaded package itself (not the unpacked .twb) so data sources can be linked to its files
        model, kit = parse_both(pkg.workbook if path.suffix.lower() == ".twb" else ws.upload / path.name)
        lw = LoadedWorkbook(source_name=path.name, workspace=ws, package=pkg, model=model, kit=kit)
        _load_data(lw, ws, extra_extracts)
        if path.suffix.lower() == ".twb":     # a .twb is not a package: say why its data is missing
            def twb(t: str) -> str:
                return (t.replace("its data file is not in the package", "its data file is separate - a .twb "
                                  "file never includes data")
                        .replace("is NOT in the package", "is not included - a .twb file never includes data"))
            lw.notes = [twb(n) for n in lw.notes]
            lw.data_files = [twb(n) for n in lw.data_files]
            lw.data_files_shared = [twb(n) for n in lw.data_files_shared]
        if not lw.tables:
            lw.notes.append("a .twb file holds only the workbook design, never its data (only a packaged .twbx "
                            "includes data): structure only" if path.suffix.lower() == ".twb"
                            else "no readable data in the package: structure only")
            return lw
        lw.store = Store(lw.tables)
    except Exception:
        # never leave a partial copy of the workbook or its data behind
        if lw is not None:
            lw.close(delete=True)
        else:
            delete_workspace(ws)
        raise
    for t in lw.tables:
        lw.profiles[t.name] = profile_table(lw.store, t.name, settings.privacy)
    for ds in model.datasources:
        prof = lw.profile_for(ds.name)
        if prof is not None and prof.entity is None:
            prof.entity = detect_entity(model, ds.name, prof)
            entity_consistency(lw.store, prof)
            recheck_name_matches(lw.store, prof, settings.privacy)
    return lw
