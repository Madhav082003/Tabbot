# Patterns in real Tableau workbooks

What 60 public workbooks look like inside, measured by `tools/mine_patterns.py` and `tools/parser_coverage.py`.
Counts are "workbooks containing it" (wb) and, where useful, total occurrences. Raw numbers:
`reports/patterns.json`, `reports/parser_coverage.json`. Sources: `reports/corpus_manifest.json`.

## 1. The corpus

| | |
| --- | --- |
| Workbooks | 60 public (22 health/clinical, 14 sales/retail, 7 other, 17 Tableau test and sample files) from 24 GitHub repos |
| Why GitHub | Tableau Public could not be reached from the build environment; many authors also commit their Tableau Public workbooks to GitHub |
| Health/clinical examples | hospital ER, hospital patient records (Synthea-style multi-table), NHS service performance, clinical-trials dashboard, US healthcare spending, global mortality, several COVID dashboards, diabetes/heart-failure analytics |
| Sales examples | several Superstore variants, Amazon, Walmart, online-retail transactions, sales performance |
| Tableau versions | XML `version` 8.2 (1), 9.3 (5), 10.0 (5), 10.1 (2), 18.1 (47). `18.1` covers every build from 2020.1 to 2025.2 |
| Size | 438 worksheets, 72 dashboards, 126 data sources, 414 calculated fields, 680 filters, 2,461 field references |
| Extracts | 20 packaged workbooks carry `.hyper` extracts (64 files; one package holds 21) |

A private pharmacovigilance workbook was used as a 61st test case. It is never shipped; its facts are only checked
by `tests/test_private_workbook.py` when you point it at the file.

## 2. Where the bytes are

| Section | Present in | Median share of file | Max share |
| --- | --- | --- | --- |
| `thumbnails` | 43 wb | **52%** | 86% |
| `datasources` | 59 wb | 24% | 87% |
| `worksheets` | 54 wb | 17% | 60% |
| `dashboards` | 43 wb | 6% | 27% |
| `windows` | 54 wb | 4% | 59% |

So for an LLM: drop thumbnails and windows first. Inside worksheets, filter member lists and manual-sort lists are
the bulk (one PV sheet: 161 KB manual sort + 90 KB filters out of 255 KB).

## 3. Field references

`[datasource].[derivation:field:type]`, but six shapes occur:

| Parts | wb | Example | Meaning |
| --- | --- | --- | --- |
| 3 | 53 (2,161 refs) | `sum:Sales:qk` | derivation, field, type |
| 1 | 42 (386) | `Multiple Values`, `Latitude (generated)`, `Action (Region)` | bare / generated field |
| 2 | 31 (167) | `:Measure Names` | special field |
| 4 | 19 (85) | `rank:sum:Sales:qk`, `ctd:CASEID:vtsum:qk`, `avg:F: GDP (curr $):qk` | table calc over aggregation, or "total using" modifier, or a colon inside the field name |
| 5 | 3 (11) | `pcto:cnt:BMI:qk:1` | table calc + aggregation + instance number |
| 6 | 2 (2) | `pcto:sum:F: GDP (curr $):qk:1` | all of the above |

Also seen: three-bracket object references `[ds].[__tableau_internal_object_id__].[cnt:Orders_<HEX>:qk]` (row count of a
logical table), `]]` escapes for `]` in names, and `\%` escapes inside filter member values.

**Tableau's own decode.** Each worksheet's `<datasource-dependencies>` lists `<column-instance name column derivation type>`,
which spells out every reference. Decoded across the corpus:

| Prefix | Derivation | Prefix | Derivation | Prefix | Derivation |
| --- | --- | --- | --- | --- | --- |
| `none` | None | `yr` | Year | `tyr` | Year-Trunc |
| `sum` | Sum | `qr` | Quarter | `tqr` | Quarter-Trunc |
| `avg` | Avg | `mn` | Month | `tmn` | Month-Trunc |
| `cnt` | Count | `wk` | Week | `twk` | Week-Trunc |
| `ctd` | CountD | `wd` | Weekday | `tdy` | Day-Trunc |
| `min`/`max`/`med` | Min/Max/Median | `md` | MDY | `hr` | Hour |
| `attr` | Attribute | `std`/`stp` | Stdev/StdevP | `io` | InOut (set) |
| `usr` | User (aggregating calc) | `pcto`, `pcdf`, `diff`, `rank`, `cum`, `fVal` | quick table calcs wrapping an aggregation | | |

Type keys: `nk` nominal (53 wb), `qk` quantitative (52), `ok` ordinal (34).

Shelf operators: `(` `)` 29 wb, `+` 19 wb (multiple / dual axes), `/` 17 wb (nesting), `*` 1 wb (crossing).

## 4. Data sources

| Construct | wb | Notes |
| --- | --- | --- |
| `connection class` | federated 54, hyper 38, textscan 19, excel-direct 19, sqlserver 9, dataengine 4 (old .tde), textclean 4, mysql 3, google-sheets 3, postgres 2, sqlproxy 1 (published source) | |
| `relation type` | table 55, collection 8, join 5, union 2, pivot 1 | no custom SQL in this corpus |
| Object model (`object-graph`) | 22 | relationships between logical tables in 12 wb |
| Multi-table extracts | 12 | tables named `<file>.csv_<32 HEX>` or `<Sheet>_<HEX>` = the logical-table id |
| Calculated fields | 45 wb, 1,118 `<calculation class='tableau'>` | kinds: row-level 210, aggregate 87, constant 67, table-calc 31, LOD 19 |
| Bins / groups / sets | 15 / 5 / 34 wb | `calculation class='bin'`, `'categorical-bin'`, `<group>` |
| Parameters | 12 wb | domain list 7, range 6, any 1 |
| Ad-hoc calcs scoped to one worksheet | 10 wb | live in the sheet's `<datasource-dependencies>`, not the data source |
| Unused calculated fields | 30 wb (half!) | |
| Physical fields listed twice | every extract | once under the connection, once under `<extract>` |

Most used calculation functions (wb): SUM 23, COUNTD 8, MAX 7, AVG 6, MIN 6, SPLIT 6, FLOAT 6, LEFT 6, LOOKUP 5,
INT 5, DATE 5, INDEX 4, TOTAL 4, TODAY 4, MAKEDATE 4, DATEDIFF 3, WINDOW_MAX 3, RANK 3. LOD: FIXED only (4 wb).

## 5. Worksheets

| Construct | wb / count |
| --- | --- |
| Mark classes | Automatic 53, Pie 16, Bar 15, Circle 14, Square 9, Line 9, Area 8, Multipolygon 7, Text 6, Shape 4 |
| Inferred chart types (438 sheets) | bar 88 (+36 dual-axis), KPI 57, text list 37, multi-measure table 36, map 30, treemap/bubbles 17, donut 17, line 16 (+6), heatmap 16, pie 16, text table 14, dot plot 10, scatter 9, area 6 (+6), empty/layout 19 |
| Measure Names / Values | 31 wb |
| Quick table calcs (`<table-calc>`) | 22 wb: PctTotal 16, Rank 5, Difference 2, CumTotal 1, PctDiff 1; computed along Rows 22, Columns 11, Field 11 |
| Reference lines | 8 wb: average 8, max, median, constant |
| Customised labels / tooltips | 25 / 17 wb |
| Placeholder title `<Sheet Name>` | 8 wb |

**Filters (680).** Classes: categorical 45 wb, quantitative 21, relative-date 1. Groupfilter functions: member 43,
level-members 40, union 29, crossjoin 28, end 16, order 16, except 16, range 3, empty-level 2, filter 1,
reorder-dimensionality 1. Interpreted modes: action 193, all-values 185, include 120, range 91, exclude 45, top 29,
set 11, exclude-null 2, relative 2, bottom 2. Context filters: 1 wb.

Filter attributes in the `user:` namespace: `ui-marker` 45 wb, `ui-enumeration` 45, `ui-domain` 39,
`ui-action-filter` 18, `ui-top-by-field` 16.

## 6. Dashboards, actions, windows

| Construct | wb / count |
| --- | --- |
| Zone `type-v2` | layout-basic 33, layout-flow 30, color 25, filter 23, text 17, bitmap 12, dashboard-object 7, empty 5, paramctrl 5, title 3, flipboard 1 (story) |
| Older `type` attribute instead of `type-v2` | 4 wb (pre-2020 files) |
| Quick-filter modes | dropdown 17, checkdropdown 12, compact 4, slider 4, radiolist 2, type_in 1, pattern 1 |
| Sizing | vscroll 36, fixed 14, automatic 14, range 3 |
| Phone/tablet layouts | 36 wb (each a full copy of the zones) |
| Actions | 30 wb, 110 actions: filter 70, highlight 25, URL 15; activation on-select 31 wb, on-hover 3 |
| Hidden worksheets | 5 wb |
| Stories (`dashboard type='storyboard'`) | 1 wb |

## 7. Quirks a parser must handle

1. **Feature-flag names** (46 wb). Tags and attributes can be prefixed `_.fcp.<Feature>.<true|false>...`, nested, and
   stored twice (a `false` and a `true` variant side by side), e.g. `_.fcp.ObjectModelEncapsulateLegacy.true...object-graph`,
   `_.fcp.SetMembershipControl.false...type` + `_.fcp.SetMembershipControl.true...type-v2`. Normalise once, keep `true`.
2. **`Æ` is a line break** in formatted text (≈500 runs: `Æ` + newline, space or tab). Not a stray character.
3. **Namespaced attributes** `{http://www.tableausoftware.com/xml/user}…` on filters, groups and columns.
4. **Generated fields** referenced but never declared: `Number of Records`, `Measure Names/Values`, `Multiple Values`,
   `Latitude/Longitude/Geometry (generated)`, `Action (…)`, `Exclusions (…)`, `Forecast Indicator`, `AdhocCluster:n`.
5. **Broken references** to fields that no longer exist in the data source (2 wb): report them, do not guess.
6. **Phone layouts** duplicate every zone; de-duplicate sheet lists.
7. **Old versions** (8.2–10.1) use `zone type` and lack many newer elements; never assume an element exists.

## 8. Parser results on this corpus

- 60/60 public workbooks + the private one parse with no failures in about 1 second total.
- 2,461 worksheet field references: 99.7% resolved to a declared field, generated field, set or parameter.
  The 8 left are all genuinely broken: two copies of one NHS workbook point at union columns the data source no
  longer declares.
- Every worksheet gets a chart type and a plain-language description; every dashboard a summary.
