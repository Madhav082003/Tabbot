# Tableau workbook format — working reference (v2)

Replaces v1 of this file in the build kit. v1 was learned from one workbook; v2 is checked against 60 public
workbooks (Tableau 8.2 to 2025.2) plus one private one. Counts like "(43 wb)" say in how many workbooks an item
was seen; `docs/PATTERNS.md` has the full statistics.

- **[verified]** seen in the corpus and handled by `tabbot.twb.parser` with tests.
- **[assumed]** not seen yet; the parser handles it defensively (records it in `model.unsupported`).

**Correction to v1:** the "stray Æ" in titles is not a stray character. `Æ` followed by a newline, space or tab is
how Tableau stores a line break in formatted text (≈500 runs in the corpus). Lint L11 must not flag it.

## 1. Packaging

- `.twbx` = zip. Members seen: the workbook `.twb` at the root, `Data/.../*.hyper` (64 files in 20 wb; one package
  has 21), `.tde` (old extracts, 3), `.tds` data-source copies (11), images (`.png` 31, `.jpg` 5), `.csv`/`.xls(x)`
  source files, and `.bin`/`.key` geocoding files. **[verified]** Pick the shallowest `.twb` as the workbook.
- Root `<workbook version source-build source-platform original-version xml:base>`. Version `18.1` covers all
  builds from 2020.1 to 2025.2; older files use 8.2, 9.3, 10.0, 10.1. **[verified]**
- Fingerprint attributes (`WorkbookFingerprinting...author-id`, `machine-id`) on the root: never send. **[verified]**

## 2. Normalise first (`tabbot.twb.xmlutil`)

1. **Feature-flag prefixes** (46 wb): `_.fcp.<Feature>.<true|false>...<name>` on tags and attributes, possibly nested,
   and sometimes both variants side by side. Strip prefixes; keep the `true` variant when both exist; a `false`
   attribute only fills a gap. Examples: `ObjectModelEncapsulateLegacy` (relation / object-graph),
   `ObjectModelTableType` (column), `SetMembershipControl` (zone `type` vs `type-v2`), `MarkAnimation`, `Layers`
   (pane). **[verified]**
2. **Namespaced attributes**: `{http://www.tableausoftware.com/xml/user}x` → `user:x` (`ui-marker`, `ui-enumeration`,
   `ui-domain`, `ui-action-filter`, `ui-top-by-field`, `ui-builder`, `auto-column`, `unnamed`). **[verified]**
3. Parse with `defusedxml` only.

## 3. Top-level sections

| Element | Content | Status |
| --- | --- | --- |
| `datasources` | data sources, fields, calcs, extract definition | [verified] 59 wb |
| `worksheets` | sheets | [verified] 54 wb |
| `dashboards` | dashboards and stories | [verified] 43 wb |
| `windows` | one per sheet/dashboard; `hidden='true'` | [verified] 54 wb |
| `thumbnails` | base64 PNG per visible sheet, typically 192×192; median 52% of file bytes | [verified] 43 wb |
| `actions` | dashboard actions | [verified] 32 wb |
| `mapsources`, `shared-views`, `external`, `datasource-relationships`, `style`, `preferences`, `document-format-change-manifest`, `repository-location` | recorded as present, not interpreted | [verified present] |

## 4. Data sources

```
datasource (name, caption, inline='true', [hasconnection='false'])
├── connection (class='federated' | a direct class)                         [verified]
│   ├── named-connections/named-connection(name, caption)/connection(class, filename|server|dbname...)
│   ├── relation (type table|join|union|collection|pivot|text, name, table, connection, join)
│   │   ├── clause/expression(op)/expression...          join condition      [verified 5 wb]
│   │   ├── columns/column(name, datatype)                union/text columns [verified]
│   │   └── relation ...                                  nested              [verified]
│   ├── metadata-records/metadata-record(class='column'): remote-name, local-name, local-type, parent-name, aggregation
│   └── refresh                                                                 [verified]
├── column (name, caption, datatype, role, type, [aggregation], [hidden], [param-domain-type], [semantic-role])
│   ├── calculation(class='tableau', formula)            calculated field   [verified 45 wb]
│   ├── calculation(class='bin', formula, size, size-parameter, peg)        [verified 15 wb]
│   ├── calculation(class='categorical-bin', column, new-bin)/bin(value)/value   group [verified 5 wb]
│   ├── aliases/alias(key, value), desc                                    [verified]
│   └── range(min, max, granularity) | members/member(value)    parameters  [verified]
├── column-instance (name, column, derivation, type, pivot) [+ table-calc]  [verified]
├── group (name, caption, user:ui-builder)/groupfilter...     sets          [verified 34 wb]
├── drill-paths/drill-path/field, folder/folder-item                        [verified 8 / 2 wb]
├── extract (enabled)/connection/relation + metadata-records (same fields again) + refresh   [verified 38 wb]
└── object-graph/objects/object(id, caption)/properties/relation           [verified 22 wb]
    └── relationships/relationship/expression + first-end-point/second-end-point(object-id)  [verified 12 wb]
```

Connection classes: federated, hyper, textscan, excel-direct, sqlserver, dataengine (.tde), textclean, mysql,
google-sheets, postgres, sqlproxy (published data source, no local data). **[verified]** Others (snowflake,
bigquery, oracle, redshift…) **[assumed]**: treat like any class string.

Facts:
- Parameters live in a data source named `Parameters`; each is a `<column param-domain-type>` with `value`. **[verified]**
- A calc's internal name is `Calculation_<digits>` or `<Caption> (copy)_<digits>`; always map by `name`. **[verified]**
- Ad-hoc calculations typed onto a shelf are `<column>`s inside that worksheet's (or dashboard's)
  `datasource-dependencies`, with `user:unnamed='<sheet>'`. **[verified 10 wb]**
- `[__tableau_internal_object_id__].[Table_<HEX>]` columns = row count of a logical table. **[verified]**
- Connections can expose local paths and user names (`filename='C:/Users/...'`). Redact. **[verified]**

## 5. Field references

Grammar (see `tabbot.twb.fieldref`):

```
ref      := '[' datasource ']' '.' '[' inner ']'
          | '[' datasource ']' '.' '[__tableau_internal_object_id__]' '.' '[' inner ']'
inner    := ':' special                                     ':Measure Names'
          | bare                                            'Multiple Values', 'Action (Region)'
          | [tablecalc ':'] derivation ':' field [':' modifier]* ':' typekey [':' instance]
```

- `field` may contain `:`; `]` inside names is written `]]`. **[verified]**
- `tablecalc` ∈ pcto, pcdf, diff, rank, cum, fVal (seen); mavg, ytd… **[assumed]**.
- `modifier` = `vt*` ("total using"), e.g. `vtsum`. **[verified]**
- `typekey` ∈ nk, ok, qk; `instance` = digits. **[verified]**
- Derivation table: `docs/PATTERNS.md` §3 (decoded from `<column-instance derivation>`). **[verified]**
- Prefer the sheet's `<column-instance name column derivation type>` when it exists: it is Tableau's own decode. **[verified]**
- Filter member values escape `%` and `\` with `\` (`'[ds].[usr:\% Diff:qk]'`); `%null%` is the null member. **[verified]**

Shelves: `(`, `)`, `/` nest, `*` cross, `+` separates measures on multiple/dual axes. **[verified]**

## 6. Worksheets

```
worksheet (name)
├── layout-options/title/formatted-text/run   ('Æ'+whitespace = line break; '<Sheet Name>' placeholder) [verified]
└── table
    ├── view
    │   ├── datasources/datasource(name)
    │   ├── datasource-dependencies(datasource)/column | column-instance [+ table-calc(type, ordering-type)/order]
    │   ├── filter (class categorical | quantitative | relative-date; context; filter-group)
    │   ├── manual-sort | computed-sort | natural-sort | shelf-sorts/shelf-sort-v2 | sort(class)
    │   ├── mapsources, slices, aggregation
    ├── panes/pane(id)
    │   ├── mark(class)
    │   ├── encodings/(color|size|text|lod|tooltip|shape|geometry|path|wedge-size|...)(column)
    │   ├── customized-label, customized-tooltip, style, reference-line(formula, scope, value-column)
    ├── rows, cols  (shelf text)
    ├── pages, annotations/annotation
```

Filters ([verified] unless marked):

| XML | Meaning |
| --- | --- |
| `groupfilter function='member' member='"X"'` | keep X |
| `union` of members | keep listed members |
| `level-members` (ui-enumeration all) | all values (a filter card that does not restrict) |
| `except` (level-members + members) | exclude listed members |
| `except` (level-members + `empty-level`) | exclude null/empty |
| `end count end='top'|'bottom'` > `order expression direction` > level-members | top/bottom N by expression |
| `range from to level` | range of a discrete ordinal (e.g. quarters 1–4) |
| `crossjoin` with `user:ui-action-filter` on column `[Action (…)]` | set by a dashboard action |
| `reorder-dimensionality` inside except | multi-field exclusion (`[Exclusions (…)]`) |
| `filter expression` | condition filter (1 wb) |
| `class='quantitative'` + `<min>`/`<max>` + `included-values` (in-range, non-null, all) | numeric/date range |
| `class='relative-date'` first-period, last-period, period-anchor, period-type-v2 | relative date |
| `class='categorical'` with no groupfilter on a set field | keep members of the set |
| `context='true'` | context filter |
| wildcard / top-N by formula / conditions on measures | **[assumed]** → recorded as unsupported |

Mark classes: Automatic, Pie, Bar, Circle, Square, Line, Area, Multipolygon, Text, Shape **[verified]**; Gantt,
Polygon, Density **[assumed]**. Chart-type inference rules: `tabbot.twb.describe.infer_chart_type` (e.g. Pie + `+`
on a shelf = donut; no shelves + size = treemap/packed bubbles; no shelves + text of measures = KPI).

## 7. Dashboards, stories, actions, windows

```
dashboard (name, [type='storyboard'])
├── layout-options/title, size(sizing-mode | min/max width/height)
├── datasource-dependencies (dashboard-scoped calcs)
├── zones/zone*   (nested)
│   zone name='<sheet>'                          sheet
│   zone type-v2 (or type in old files)          layout-basic, layout-flow, filter(param=<field ref>, mode),
│                                                paramctrl(param='[Parameters].[P]'), color, size, text, title,
│                                                bitmap, empty, dashboard-object, flipboard (story)
│   zone/formatted-text                          text content
│   zone/flipboard/story-points/story-point      story points
└── devicelayouts/devicelayout(name)/zones/...   a full phone/tablet copy: de-duplicate
```

Actions: `action(name, caption)/activation(type on-select|on-hover)`, `source(type sheet, dashboard, worksheet)/exclude-sheet`,
`command(command)/param(name, value)`. Commands: `tsc:tsl-filter` (filter), `tsc:brush` (highlight); URL actions use
`<link>`. Parameter and set actions **[assumed]**.

Windows: `window(class worksheet|dashboard, name, hidden)`. Hidden sheets appear only inside dashboards. **[verified]**

## 8. Extracts (`.hyper`)

- Open with `tableauhyperapi` (telemetry off). Schemas seen: `Extract` and `public`. **[verified]**
- Single-table extracts: `"Extract"."Extract"`. Multi-table extracts: one table per logical table, named
  `<source>_<32 HEX>` where the HEX matches the object-graph object id. **[verified 12 wb]**
- Column types seen: TEXT, BIG_INT, SMALL_INT, DOUBLE, DATE, TIMESTAMP. **[verified]**
- Dates can be stored as TEXT and converted by a calc (`DATE([x])`). Detect formats; do not assume. **[verified]**
- Rows are not necessarily one per entity (PV workbook: 36 per case): count distinct ids. **[verified]**

## 9. Known pitfalls

- Element truthiness: use `is not None`, never `el or []`.
- Names can contain `:`, `]`, `(`, `$`, `%`, `/` — never split on them blindly.
- Members, aliases and manual sorts can hold free text from source data: treat as sensitive.
- The sheet name, title and filter can disagree; a "%" title does not imply a percent calc.
- A calc can ignore a unit column; a filter can be copied onto charts where it does not belong.
- Fields can be referenced after they were removed from the data source.
- The last period of a time axis is often partial.
