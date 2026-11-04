"""Structured model of a Tableau workbook (built by twb/parser.py)."""

from __future__ import annotations

from pydantic import BaseModel
from pydantic import Field as PField

from tabbot.twb.fieldref import FieldRef


class Field(BaseModel):
    datasource: str
    internal_name: str                    # "Calculation_0322741033488384" or "CASEID"
    caption: str                          # readable name ("Parsed Date"); internal name if none
    datatype: str | None = None           # string, integer, real, date, datetime, boolean
    role: str | None = None               # dimension | measure
    is_calculated: bool = False
    formula: str | None = None
    formula_readable: str | None = None   # internal calc names replaced by captions
    is_parameter: bool = False
    used_by_sheets: list[str] = []
    used_by_calcs: list[str] = []         # captions of calcs whose formula references this field


class DataSource(BaseModel):
    name: str
    caption: str | None = None
    connection_class: str | None = None   # excel-direct, sqlserver, sqlproxy (published) ...
    connection_file: str | None = None    # basename only (directories redacted)
    connection_has_local_path: bool = False
    has_extract: bool = False
    extract_tables: list[str] = []        # e.g. ['"Extract"."Extract"']
    fields: list[Field] = []

    def field(self, internal_name: str) -> Field | None:
        return next((f for f in self.fields if f.internal_name == internal_name), None)


class Filter(BaseModel):
    field: FieldRef
    kind: str                             # categorical | quantitative | relative-date | unknown
    mode: str                             # include | exclude | all | range | unknown
    members: list[str] = []               # values (quotes stripped); "%null%" = the null member
    member_count: int = 0
    min: str | None = None
    max: str | None = None
    raw_function: str | None = None       # top groupfilter function, for diagnostics


class Worksheet(BaseModel):
    name: str
    title: str | None = None
    datasource: str | None = None
    rows: list[FieldRef] = []
    cols: list[FieldRef] = []
    rows_raw: str = ""
    cols_raw: str = ""
    mark: str | None = None               # Pie, Line, Square, Bar, Automatic, Text, Circle ...
    marks: list[str] = []                 # all pane marks (dual-axis sheets have several)
    encodings: dict[str, list[FieldRef]] = {}
    filters: list[Filter] = []
    sort_count: int = 0
    table_calcs: int = 0
    hidden: bool = False
    xml_bytes: int = 0
    unsupported: list[str] = []

    def all_refs(self) -> list[FieldRef]:
        refs = list(self.rows) + list(self.cols)
        for v in self.encodings.values():
            refs.extend(v)
        refs.extend(f.field for f in self.filters)
        return refs


class Zone(BaseModel):
    name: str
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0


class QuickFilter(BaseModel):
    field: FieldRef
    mode: str | None = None               # checkdropdown, dropdown, radiolist ...
    sheet: str | None = None              # worksheet the filter control is tied to


class Dashboard(BaseModel):
    name: str
    title: str | None = None
    sheets: list[str] = []                # de-duplicated, in layout order
    zones: list[Zone] = []                # positions of sheet zones (0..100000 units)
    quick_filters: list[QuickFilter] = []
    legends: int = 0
    has_device_layouts: bool = False
    size: dict[str, str] = {}


class Thumbnail(BaseModel):
    name: str
    width: int | None = None
    height: int | None = None
    bytes: int = 0
    file: str | None = None               # PNG path inside the workspace (never sent to the LLM)


class WorkbookModel(BaseModel):
    version: str | None = None
    source_build: str | None = None
    datasources: list[DataSource] = []
    parameters: list[Field] = []
    worksheets: list[Worksheet] = []
    dashboards: list[Dashboard] = []
    thumbnails: list[Thumbnail] = []
    actions: int = 0
    size_report: dict = PField(default_factory=dict)
    unsupported: list[str] = []

    # ---- lookups
    def worksheet(self, name: str) -> Worksheet | None:
        return next((w for w in self.worksheets if w.name == name), None)

    def dashboard(self, name: str) -> Dashboard | None:
        return next((d for d in self.dashboards if d.name == name), None)

    def datasource(self, name: str) -> DataSource | None:
        return next((d for d in self.datasources if d.name == name), None)

    def field(self, datasource: str, internal_name: str) -> Field | None:
        ds = self.datasource(datasource)
        return ds.field(internal_name) if ds else None

    @property
    def calculated_fields(self) -> list[Field]:
        return [f for ds in self.datasources for f in ds.fields if f.is_calculated]
