# Tableau workbook format — working reference

What we know about `.twbx` / `.twb` / `.hyper`, learned from inspecting one real workbook (Tableau 2026.2,
`version='18.1'`, 88 worksheets, 13 dashboards, one Excel-based extract) plus general Tableau knowledge.

Every item is marked:
- **[verified]** seen and checked in code on the real workbook;
- **[assumed]** general Tableau knowledge, not yet checked here. The parser must handle these defensively and
  record anything unexpected in `WorkbookModel.unsupported` instead of failing.

## 1. Packaging

- `.twbx` is an ordinary zip. **[verified]** Members seen: `<Name>.twb` at the root and
  `Data/<Name>.twb Files/federated.hyper`. Other workbooks may also hold images, `.tde` (old extract format),
  Excel/CSV source files, or several `.hyper` files. **[assumed]**
- `.twb` is UTF-8 XML. Root element `<workbook>` with attributes `version`, `source-build`, `source-platform`,
  `original-version`, `xml:base` (for example a Tableau Cloud URL). **[verified]**
- Root also carries fingerprint attributes (`_.fcp.WorkbookFingerprinting...author-id`, `machine-id`). Never
  send these to the LLM. **[verified]**

## 2. Top-level sections

| Element | Content | Notes |
| --- | --- | --- |
| `document-format-change-manifest` | feature flags | ignore **[verified]** |
| `preferences`, `style`, `repository-location` | small | ignore **[verified]** |
| `datasources` | data sources, fields, calculations, extract definition | **[verified]** |
| `worksheets` | one `<worksheet>` per sheet | usually the largest section (874 KB of 1.77 MB here) **[verified]** |
| `dashboards` | one `<dashboard>` per dashboard | **[verified]** |
| `windows` | one `<window>` per sheet/dashboard, `hidden='true'` for hidden sheets | **[verified]** |
| `thumbnails` | base64 PNG per visible sheet/dashboard | 192×192 here, 426 KB total; never send **[verified]** |
| `actions` | dashboard actions | 0 here; parse names and types only **[assumed]** |

## 3. Data sources

Verified nesting:

```
datasource (name='federated.pv', caption='PV Cases (Argus Extract)')
├── connection (class='federated')                                  [verified]
│   ├── named-connections/named-connection(caption)/connection(class='excel-direct', filename=...)
│   ├── relation(type='table', name=..., table=...)                 [verified]
│   └── metadata-records/metadata-record(class='column')            [verified]
│         remote-name, local-type, ... (one per physical field)
├── column (caption, name='[Calculation_...]', datatype, role, type)
│   └── calculation (class='tableau', formula='...')                [verified]
├── extract (enabled='true', units='records', count='-1')           [verified]
│   └── connection/relation + metadata-records  (the SAME fields again)  [verified]
└── object-graph/objects/object/properties/relation/columns/column [verified, not used]
```

Facts:
- Physical fields appear twice (Excel connection and extract connection): de-duplicate by `remote-name`. **[verified]**
- Calculated fields are `<column>` elements with a `<calculation>` child. Internal names look like
  `Calculation_0322741033488384`, but a duplicated calc can be named `Female Cases (copy)_0322741149810692`
  while its caption is `Male Cases` — always map by the `name` attribute, show the `caption`. **[verified]**
- Formulas reference fields as `[FIELD]` or by internal calc name `[Calculation_…]`. **[verified]**
- Parameters live in a datasource named `Parameters`; their `<column>` has `param-domain-type`. **[assumed]**
- `connection class` values: `excel-direct` **[verified]**; `sqlproxy` = published data source on Tableau
  Server/Cloud (no local data); `hyper`, `textscan`, database classes **[assumed]**.
- Connection attributes can contain local paths with user names and client names (`filename=C:/Users/...`). Redact. **[verified]**

## 4. Field references

Shelves, encodings and filters reference fields as:

```
[<datasource>].[<derivation>:<field internal name>:<type key>]
[federated.pv].[ctd:CASEID:qk]
[federated.pv].[none:SOC:nk]
[federated.pv].[tqr:Calculation_0322741033488384:qk]
[federated.pv].[ctd:CASEID:vtsum:qk]        # 4 parts                               [verified]
```

Parse with a regex over the whole shelf string, e.g. `\[([^\]]+)\]\.\[([^\]]+)\]`, then split the second group
on `:`; first part = derivation, last part = type key, middle = field (may itself contain colons? treat
everything between first and last as field + extras, and check the field against the field index).

**Derivations**

| Prefix | Meaning | Status |
| --- | --- | --- |
| `none` | field as-is (dimension) | [verified] |
| `ctd` | COUNT DISTINCT | [verified] |
| `yr`, `qr`, `mn` | date part: year, quarter, month (discrete headers) | [verified] |
| `tqr` | date truncated to quarter (continuous axis) | [verified] |
| `vtsum` (as an extra part) | believed to be "total using Sum" | [verified present, meaning assumed] |
| `sum`, `avg`, `cnt`, `min`, `max`, `med`, `attr` | aggregations | [assumed] |
| `usr` | user calculation that already aggregates | [assumed] |
| `tyr`, `tmn`, `twk`, `tdy`, `wk`, `dy`, `hr`, `mi`, `sc` | other date truncations / parts | [assumed] |
| `pcto`, `cum`, `diff`, `rank` … | quick table calcs (percent of total, running total, …) | [assumed] — also look for `<table-calc>` elements |

**Type keys:** `nk` nominal (discrete category), `ok` ordinal (discrete ordered, e.g. date parts), `qk`
quantitative (continuous measure or axis). **[verified]**

**Shelf operators:** `/` nests (HLT then PT inside it), `*` crosses (every SOC × every quarter); parentheses group.
For aggregation both mean "group by both". **[verified]** Other separators (e.g. `+` between several measures)
**[assumed]** — keep the raw text and parse defensively.

## 5. Worksheets

```
worksheet (name)
├── layout-options/title/formatted-text/run (text; may be the placeholder "<Sheet Name>")   [verified]
└── table
    ├── view
    │   ├── datasource-dependencies (fields used)                                           [verified]
    │   ├── filter (class='categorical'|'quantitative'|..., column=<field ref>)             [verified: categorical]
    │   │   └── groupfilter (function, member, level, ui-enumeration, ui-marker) — nested
    │   ├── manual-sort / shelf-sort-v2 / computed-sort / natural-sort                      [verified: first two]
    │   └── slices
    ├── panes/pane/mark(class='Pie'|'Line'|'Square'|'Automatic'|...)                         [verified]
    │   └── encodings/(color|size|text|lod|tooltip|shape|...)(column=<field ref>)           [verified: color,size,text,lod]
    ├── rows  (text: shelf expression)                                                       [verified]
    └── cols  (text: shelf expression)                                                       [verified]
```

**Mark classes → chart:** `Pie` pie · `Line` line · `Square` with colour + text = heatmap / highlight table ·
`Automatic` with only text = text table **[verified]** · `Bar`, `Circle`, `Shape`, `Area`, `Map`, `Text`
**[assumed]**.

**Filters (`groupfilter`):**

| Pattern | Meaning | Status |
| --- | --- | --- |
| `function='member' member='"X"'` | keep exactly X | [verified] |
| `function='union'` containing `member` children | keep the listed members (`ui-enumeration='inclusive'`) | [verified] |
| `function='except'` containing a `level-members` and a union of members | keep all except the listed ones | [verified] |
| `function='level-members'` alone (`ui-enumeration='all'`) | keep everything | [verified] |
| member value `'%null%'` | the null member | [verified] |
| `function='range'`, quantitative `<min>`/`<max>` | numeric/date range | [assumed] |
| `function='filter'` with an expression, `order` / top-N | condition or top-N filters | [assumed] → unsupported in v1 |

Member values are stored with double quotes (`'"Cardiac disorders"'`); strip them. Member lists can be huge
(554 HLTs on one sheet) and can contain free text such as reporter verbatims — treat as sensitive by default.

**Table calcs:** `<table-calc>` elements (0 in the reference workbook). Report their presence; v1 does not rebuild them.

## 6. Dashboards and windows

```
dashboard (name)
├── size (sizing-mode='automatic' | minwidth/maxwidth/minheight/maxheight)   [verified]
├── zones/zone/zone/...  (nested layout)                                      [verified]
│   zone name='<worksheet>' (no type-v2)  → a sheet on the dashboard
│   zone type-v2='filter' param='<field ref>' mode='checkdropdown'|...       → quick filter
│   zone type-v2='color'  → colour legend · 'title' · 'layout-basic' · 'layout-flow'      [verified]
│   other type-v2 values ('text', 'bitmap', 'web', 'empty', 'paramctrl', …)  [assumed]
└── devicelayouts/devicelayout/...  a second (phone) copy of the zones        [verified]
```

- Because of the phone layout, every sheet appears twice in a naive zone walk: de-duplicate. **[verified]**
- `windows/window(class='worksheet'|'dashboard', name, hidden='true')`. In the reference workbook every worksheet
  except one was hidden, so on publishing only the dashboards and that one sheet become views. **[verified hidden
  flags; publishing behaviour assumed]**

## 7. The extract (`.hyper`)

- Open with `tableauhyperapi`: `HyperProcess(Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU, parameters={"log_dir": ...})`,
  `Connection(endpoint, path)`, `catalog.get_schema_names()`, `get_table_names(schema)`,
  `get_table_definition(table)`. **[verified]**
- Reference workbook: one table `"Extract"."Extract"`, 128,522 rows, 56 columns, almost all `TEXT`. **[verified]**
- Dates may be stored as text (`30-SEP-2021`) and turned into dates by a calc (`DATE([INITIAL_RECEIVED_DATE])`).
  Detect formats; do not assume date types. **[verified]**
- Rows are not one per entity: 36 rows per case on average, none fully duplicated. Count distinct entity ids;
  never count rows. **[verified]**
- Multi-table extracts use other schema/table names; map via `<extract><connection><relation>`. **[assumed]**
- Extract filters would appear under `<extract>`; none in the reference workbook. **[verified absence]**

## 8. Known pitfalls

- `root.find('worksheets') or []` triggers a DeprecationWarning (element truthiness); use `is not None`.
- The worksheet name, its title and its filter can disagree (a sheet named after one SOC filtering another).
- A "%" in a dashboard name does not mean the chart shows percentages: check for a percent table calc.
- A calc can look right and still ignore a unit column (age in days treated as years).
- A filter can be copied onto charts where it does not belong (a gender filter on age-group pies).
- `COUNT([dim])` over an extract with repeated rows inflates silently.
- The last period of a time axis is often partial.
- XML size is dominated by filter member lists and manual-sort lists, not by chart definitions.
