"""One call to go from an uploaded file to everything the app needs: model, data store, profiles."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from tabbot.config import Settings, get_settings
from tabbot.data.hyper_reader import TableInfo, export_to_parquet
from tabbot.data.profile import TableProfile, detect_entity, entity_consistency, profile_table
from tabbot.data.store import Store
from tabbot.twb.model import WorkbookModel
from tabbot.twb.parser import parse_workbook
from tabbot.workspace import Package, Workspace, new_workspace, open_upload


@dataclass
class LoadedWorkbook:
    source_name: str
    workspace: Workspace
    package: Package
    model: WorkbookModel
    tables: list[TableInfo] = field(default_factory=list)
    store: Store | None = None
    profiles: dict[str, TableProfile] = field(default_factory=dict)   # by table name
    notes: list[str] = field(default_factory=list)

    def table_for(self, datasource: str | None) -> str | None:
        """Extract table that holds a data source's data (v1: by extract relation name, else the only table)."""
        if not self.tables:
            return None
        ds = self.model.datasource(datasource) if datasource else None
        if ds is not None:
            for t in ds.extract_tables:
                if t in self.profiles:
                    return t
        return self.tables[0].name if len(self.tables) == 1 else None

    def profile_for(self, datasource: str | None) -> TableProfile | None:
        t = self.table_for(datasource)
        return self.profiles.get(t) if t else None

    def close(self) -> None:
        if self.store is not None:
            self.store.close()


def load_workbook(path: Path | str, settings: Settings | None = None,
                  extra_extracts: list[Path] | None = None) -> LoadedWorkbook:
    settings = settings or get_settings()
    path = Path(path)
    ws = new_workspace(settings.workspace_dir)
    pkg = open_upload(path, ws, extra_extracts, settings.max_unzipped_mb)
    model = parse_workbook(pkg.workbook, thumbnails_dir=ws.thumbnails)
    lw = LoadedWorkbook(source_name=path.name, workspace=ws, package=pkg, model=model)

    if pkg.unsupported_extracts:
        lw.notes.append(f"{len(pkg.unsupported_extracts)} old-format .tde extract(s) cannot be read")
    if not pkg.extracts:
        lw.notes.append("no .hyper extract in the package: structure only, no data")
        return lw

    lw.tables = export_to_parquet(pkg.extracts, ws.parquet, ws.logs)
    lw.store = Store(lw.tables)
    for t in lw.tables:
        lw.profiles[t.name] = profile_table(lw.store, t.name, settings.privacy)
    if len(lw.tables) > 1:
        lw.notes.append(f"{len(lw.tables)} extract tables: only sheets whose data sit in one table can be redrawn")

    for ds in model.datasources:
        prof = lw.profile_for(ds.name)
        if prof is not None and prof.entity is None:
            prof.entity = detect_entity(model, ds.name, prof)
            entity_consistency(lw.store, prof)
    return lw
