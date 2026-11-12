"""Pydantic models for a parsed Tableau workbook.

Everything here is metadata about the workbook. No data values from the extract live in these models; filter
member lists, aliases and group members do (they are part of the XML), so `tabbot.twb.redact` (milestone M3)
must decide what of them may leave the machine.
"""
from __future__ import annotations

from pydantic import BaseModel
from pydantic import Field as PField

from .fieldref import FieldRef, Shelf
from .formula import FormulaInfo

# ------------------------------------------------------------------ data sources

class Connection(BaseModel):
    name: str | None = None              # named-connection name, e.g. "excel-direct.0x7..."
    caption: str | None = None
    cls: str | None = None               # class: excel-direct, hyper, sqlserver, textscan, sqlproxy, federated...
    server: str | None = None
    dbname: str | None = None
    schema_name: str | None = None
    filename: str | None = None          # may contain local paths and user names: redact before sharing
    directory: str | None = None         # textscan (CSV) connections keep the folder here
    port: str | None = None
    username: str | None = None          # never sent anywhere; kept for lint L14
    other: dict[str, str] = {}


class Relation(BaseModel):
    type: str                            # table | join | union | text (custom SQL) | collection | pivot | ...
    object_id: str | None = None         # logical-table id (object-graph); multi-table extracts name tables after it
    name: str | None = None
    table: str | None = None
    connection: str | None = None
    join: str | None = None              # inner | left | right | full
    clause: str | None = None            # readable join condition
    custom_sql: str | None = None
    columns: list[str] = []              # declared columns (unions and text files declare them here)
    children: list[Relation] = []


class Relationship(BaseModel):           # logical layer (object model) relationship between two tables
    left_object: str | None = None
    right_object: str | None = None
    expression: str = ""


class PhysicalField(BaseModel):
    remote_name: str | None = None
    local_name: str | None = None
    local_type: str | None = None
    parent: str | None = None            # table/relation name
    aggregation: str | None = None
    in_extract: bool = False


class BinDef(BaseModel):
    source: str | None = None
    size: str | None = None
    size_parameter: str | None = None
    peg: str | None = None


class GroupDef(BaseModel):               # categorical-bin ("group" in the UI)
    source: str | None = None
    groups: dict[str, int] = {}          # group label -> number of member values
    other_group: bool = False


class Field(BaseModel):
    datasource: str
    name: str                            # internal name without brackets, e.g. "Calculation_123" or "Sales"
    caption: str | None = None
    datatype: str | None = None
    role: str | None = None              # dimension | measure
    type: str | None = None              # nominal | ordinal | quantitative
    kind: str = "physical"               # physical | calculated | bin | group | parameter | generated
    scope: str = "datasource"            # datasource | worksheet (ad-hoc calc typed on a shelf) | dashboard
    defined_in: str | None = None        # worksheet/dashboard name when scope != datasource
    formula: str | None = None
    formula_readable: str | None = None
    formula_info: FormulaInfo | None = None
    default_aggregation: str | None = None
    hidden: bool = False
    description: str | None = None
    semantic_role: str | None = None
    aliases: dict[str, str] = {}
    bin: BinDef | None = None
    group: GroupDef | None = None
    used_directly_by: list[str] = []     # worksheet names that reference this field
    used_by: list[str] = []              # including via other calculations

    @property
    def label(self) -> str:
        return self.caption or self.name


class Parameter(BaseModel):
    name: str
    caption: str | None = None
    datatype: str | None = None
    domain_type: str | None = None       # list | range | any
    value: str | None = None
    members: list[str] = []
    range: dict[str, str] = {}
    used_by_calcs: list[str] = []


class SetDef(BaseModel):
    name: str
    caption: str | None = None
    level: str | None = None             # field the set is built on
    mode: str = "unknown"                # members | condition | top | combined | unknown
    member_count: int = 0
    condition: str | None = None
    ui_builder: str | None = None


class DrillPath(BaseModel):
    name: str
    fields: list[str] = []


class Extract(BaseModel):
    enabled: bool = False
    tables: list[str] = []
    hyper_tables: list[str] = []         # the relations' table attribute, e.g. "[Extract].[Extract]" (added for tabbot)
    dbname: str | None = None            # <extract><connection dbname=...>: the .hyper path inside the package
    incremental: bool | None = None
    filters: int = 0


class DataSource(BaseModel):
    name: str
    caption: str | None = None
    is_parameters: bool = False
    is_published: bool = False           # sqlproxy: a published data source, no local data
    connections: list[Connection] = []
    relation: Relation | None = None     # physical layer as stored under <connection>
    objects: list[Relation] = []         # logical tables from <object-graph>
    relationships: list[Relationship] = []
    extract: Extract | None = None
    fields: list[Field] = []
    physical_fields: list[PhysicalField] = []
    sets: list[SetDef] = []
    drill_paths: list[DrillPath] = []
    folders: dict[str, list[str]] = {}

    @property
    def label(self) -> str:
        return self.caption or self.name


# ------------------------------------------------------------------ worksheets

class FilterDef(BaseModel):
    field: FieldRef
    kind: str                            # categorical | quantitative | relative-date | unknown
    mode: str                            # include | exclude | all | range | top | bottom | condition | action |
                                         # crossjoin | exclude-null | unknown
    members: list[str] = []
    member_count: int = 0
    includes_null: bool | None = None
    top: dict | None = None              # {"end": "top", "count": 10, "by": "SUM([Sales])"}
    range: dict | None = None            # {"from":..,"to":..} or {"min":..,"max":..,"included_values":..}
    relative: dict | None = None         # {"period_type":"day","first":-2,"last":0,"anchor":...}
    condition: str | None = None
    context: bool = False
    filter_group: str | None = None
    is_action_filter: bool = False
    action_ref: str | None = None        # user:ui-action-filter, e.g. "Action8" (added for tabbot)
    action_status: str | None = None     # linked | missing (action deleted) | unlinked (no reference) | tooltip
    action_caption: str | None = None    # caption of the linked action
    keeps_all_values: bool = False       # include list that names every value the field can have
    functions: list[str] = []            # groupfilter functions seen in the tree
    ui_enumeration: str | None = None


class SortDef(BaseModel):
    kind: str                            # manual | computed | natural | shelf | unknown
    field: FieldRef | None = None
    direction: str | None = None
    using: FieldRef | None = None        # measure the sort is computed by
    member_count: int = 0


class TableCalcDef(BaseModel):
    column: str                          # the column-instance name it applies to
    type: str | None = None              # PctTotal | Rank | Difference | CumTotal | PctDiff | ...
    ordering_type: str | None = None     # Rows = Table (across) | Columns = Table (down) | Field (verified 60 wb)
    ordering_fields: list[str] = []      # 'Field': the references it runs along, as written ('[ds].[none:X:nk]')


class Encoding(BaseModel):
    channel: str                         # color | size | text | lod | tooltip | shape | path | wedge-size | ...
    field: FieldRef


class ReferenceLine(BaseModel):
    formula: str | None = None           # average | max | median | constant | ...
    scope: str | None = None
    value_column: FieldRef | None = None
    label: str | None = None


class Pane(BaseModel):
    id: str | None = None
    mark: str | None = None
    encodings: list[Encoding] = []
    customized_label: bool = False
    customized_tooltip: bool = False
    reference_lines: list[ReferenceLine] = []


class ColumnInstance(BaseModel):
    name: str                            # "[sum:Sales:qk]"
    column: str | None = None            # "[Sales]"
    derivation: str | None = None        # "Sum"
    type: str | None = None              # quantitative
    pivot: str | None = None
    table_calc: TableCalcDef | None = None


class Worksheet(BaseModel):
    name: str
    title: str | None = None
    title_is_placeholder: bool = False   # "<Sheet Name>" or no custom title
    datasources: list[str] = []
    rows: Shelf = PField(default_factory=lambda: Shelf(raw=""))
    cols: Shelf = PField(default_factory=lambda: Shelf(raw=""))
    pages: list[FieldRef] = []
    panes: list[Pane] = []
    filters: list[FilterDef] = []
    sorts: list[SortDef] = []
    table_calcs: list[TableCalcDef] = []
    column_instances: list[ColumnInstance] = []
    uses_measure_names: bool = False
    annotations: int = 0
    hidden: bool = False
    in_dashboards: list[str] = []
    chart_type: str | None = None        # inferred, see describe.py
    xml_bytes: int = 0

    @property
    def mark(self) -> str | None:
        return next((p.mark for p in self.panes if p.mark), None)

    def all_refs(self) -> list[FieldRef]:
        refs = list(self.rows.refs) + list(self.cols.refs) + list(self.pages)
        for p in self.panes:
            refs += [e.field for e in p.encodings]
        refs += [f.field for f in self.filters]
        return refs


# ------------------------------------------------------------------ dashboards, actions, windows

class Zone(BaseModel):
    id: str | None = None
    kind: str                            # sheet | filter | paramctrl | color | size | shape | legend | text | title |
                                         # bitmap | web | empty | layout-basic | layout-flow | dashboard-object | ...
    name: str | None = None              # worksheet name for sheet zones
    param: str | None = None
    field: FieldRef | None = None
    mode: str | None = None              # dropdown | checkdropdown | slider | ...
    text: str | None = None
    depth: int = 0
    x: int | None = None
    y: int | None = None
    w: int | None = None
    h: int | None = None


class Dashboard(BaseModel):
    name: str
    kind: str = "dashboard"              # dashboard | storyboard
    title: str | None = None
    size: dict[str, str] = {}
    sheets: list[str] = []               # de-duplicated, main layout
    zones: list[Zone] = []               # main layout only (phone layouts are copies)
    quick_filters: list[FieldRef] = []
    parameter_controls: list[str] = []
    legends: int = 0
    text_blocks: list[str] = []
    images: int = 0
    web_objects: int = 0
    device_layouts: list[str] = []
    story_points: list[str] = []
    xml_bytes: int = 0


class Action(BaseModel):
    name: str
    caption: str | None = None
    kind: str = "other"                  # filter | highlight | url | parameter | set | go-to-sheet | other
    command: str | None = None
    activation: str | None = None        # on-select | on-hover | on-menu
    source_dashboard: str | None = None
    source_worksheet: str | None = None
    excluded_sheets: list[str] = []
    params: dict[str, str] = {}


class DataFileLink(BaseModel):
    """Which file inside the package holds a data source's data (added for tabbot)."""
    datasource: str
    connection: str | None = None        # named-connection name (None for the extract)
    cls: str | None = None               # hyper (extract) | excel-direct | textscan | ...
    via: str = "file"                    # extract | file
    declared: str = ""                   # the path as written in the workbook (may be an absolute local path)
    member: str | None = None            # matching package member, None when the file is not in the package
    match: str = ""                      # path | suffix | name | "" (not found)
    tables: list[str] = []               # relation tables read from this file, e.g. ["[Orders$]", "[People$]"]


class Window(BaseModel):
    name: str
    cls: str | None = None
    hidden: bool = False


class Thumbnail(BaseModel):
    name: str
    width: int | None = None
    height: int | None = None
    base64_bytes: int = 0
    # the preview image Tableau saved (PNG bytes), for display on this computer only: excluded from every
    # dump/serialisation so it can never end up in a tool result or a request (added for tabbot)
    png: bytes | None = PField(default=None, exclude=True, repr=False)


# ------------------------------------------------------------------ workbook

class WorkbookModel(BaseModel):
    source: str | None = None
    version: str | None = None
    source_build: str | None = None
    source_platform: str | None = None
    original_version: str | None = None
    datasources: list[DataSource] = []
    parameters: list[Parameter] = []
    worksheets: list[Worksheet] = []
    dashboards: list[Dashboard] = []
    actions: list[Action] = []
    windows: list[Window] = []
    thumbnails: list[Thumbnail] = []
    size_report: dict = {}
    feature_flags: dict[str, int] = {}
    unresolved_refs: list[str] = PField(default_factory=list)   # refs the parser could not tie to a field
    unsupported: list[str] = PField(default_factory=list)       # constructs recorded but not interpreted
    warnings: list[str] = PField(default_factory=list)
    data_links: list[DataFileLink] = PField(default_factory=list)         # data source -> package file
    unlinked_data_files: list[str] = PField(default_factory=list)         # data files no data source uses
    geocoding_files: list[str] = PField(default_factory=list)             # Local Data/...: custom geocoding

    # convenience lookups
    def worksheet(self, name: str) -> Worksheet | None:
        return next((w for w in self.worksheets if w.name == name), None)

    def dashboard(self, name: str) -> Dashboard | None:
        return next((d for d in self.dashboards if d.name == name), None)

    def datasource(self, name: str) -> DataSource | None:
        return next((d for d in self.datasources if d.name == name), None)

    def field(self, datasource: str, name: str) -> Field | None:
        ds = self.datasource(datasource)
        if ds is None:
            return None
        return next((f for f in ds.fields if f.name == name), None)

    @property
    def hidden_worksheets(self) -> list[str]:
        return [w.name for w in self.worksheets if w.hidden]

    @property
    def calculated_fields(self) -> list[Field]:
        return [f for d in self.datasources for f in d.fields if f.kind == "calculated"]
