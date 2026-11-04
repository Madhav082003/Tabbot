# Work plan — Tableau Insights Bot (demo version)

Updated 2026-11-04. **This file overrides `CLAUDE.md`, `SPEC.md` and `BUILD_PLAN.md` wherever they differ.**
Those kit docs describe a production system; we build a smaller, good-looking demo first and grow it later.
Nothing starts until you say "start S1" (and each later step waits for your go).

---

## 1. What we are building

A web app on your laptop that looks like Tableau:

```
┌──────────────────────────────────────────────────────────────┬──────────────────┐
│  Tableau Insights          [ Upload .twbx ]   Workbook: X.twbx│                  │
├──────────────────────────────────────────────────────────────┤   Ask Claude     │
│ [Dashboard 1] [Dashboard 2] [Dashboard 3] ...   ← tabs        │   ────────────   │
├──────────────────────────────────────────────────────────────┤   chat history   │
│  Filters:  [Category ▾]  [Year ▾]                              │                  │
│ ┌──────────────────────────┐ ┌───────────────────────────┐   │   "Give me       │
│ │ heatmap      ✓ Exact     │ │ line chart  ⚠ Approximate │   │    insights"     │
│ │ redrawn from the data    │ │                           │   │                  │
│ └──────────────────────────┘ └───────────────────────────┘   │   [ type here ]  │
│  ⚑ Checks: last quarter is partial · cells can't be summed     │   ◂ collapse     │
└──────────────────────────────────────────────────────────────┴──────────────────┘
```

- Dashboards are the main screen: one tab per dashboard, charts redrawn from the workbook's own data and
  definitions, Tableau-like colours, quick filters.
- Chat is a small collapsible panel on the right. Claude writes insights and can switch the tab and apply a
  filter so the viewer sees what it is talking about.
- Works on **any** `.twbx`. Nothing is hard-coded to one workbook.

## 2. Decisions

| Topic | Decision |
| --- | --- |
| Visuals | Redrawn locally from the `.twbx` (Plotly charts in a Tableau-style layout) |
| Runs where | Your laptop only, `http://127.0.0.1:8501` |
| Input | You upload a `.twbx` manually |
| App framework | Streamlit |
| Chat | Collapsible right-side panel |
| LLM | Claude Sonnet 5.5, your own Anthropic API key (console.anthropic.com, billed separately from the Claude subscription) |
| What Claude sees | Workbook structure + aggregated numbers, exact (no small-count hiding). Never raw rows, file paths or raw XML |
| Audit | Every request to Claude saved to a local log before it is sent |
| Python | 3.13 in `C:\tabbot\.venv` (installed with `py install 3.13`); your 3.15 stays untouched |
| Project folder | `C:\tabbot` (outside OneDrive, so data never syncs to the cloud) |
| Git | Local repo in `C:\tabbot`; I commit only when you ask |

## 3. The method (generalised from the manual analysis)

| # | Step | What happens, for any workbook |
| --- | --- | --- |
| 1 | Open | Safely unzip the `.twbx` into `C:\tabbot\workspace\<id>\` |
| 2 | Read the XML | Data sources, calculated fields (internal id → readable name), worksheets (rows, columns, chart type, colour/size/label fields, filters), dashboards (sheets, quick filters, layout positions). Phone layouts de-duplicated. Unknown parts noted, never a crash |
| 3 | Read the data | `.hyper` extract → Parquet → DuckDB. Detect text date formats, rows per entity, and **the entity to count** (the field the workbook itself uses in COUNT DISTINCT; fallback: an id-like column) |
| 4 | Redraw | Worksheet definition → query: filters → WHERE, rows/cols → GROUP BY, `yr/qr/mn/tqr` → date parts, `ctd` → COUNT DISTINCT, `sum/avg/cnt/min/max` → aggregates; simple calcs translated (`DATE()`, `DATETRUNC`, `IF…THEN…ELSE…END`, `COUNTD(IF…)`); drawn by mark type |
| 5 | Check | Partial last period, cells that can't be summed, colour scale stretched by one value, date parse failures, filter values missing from data; definition checks (counting dates, "%" title without a percentage, unused calcs, sheet name ≠ its filter, calc ignoring a unit column) |
| 6 | Analyse | Trend, last N vs previous N full periods, change point, growth per category, driver scan (other fields; top-N mode for fields with many values), share shift, items-per-entity over time, flag share ("entities with at least one X") |
| 7 | Explain | Claude calls these through tools, cites result ids, labels insights **Data** / **Interpretation**, can switch tab / apply filter |

## 4. Accuracy: how we know a redrawn chart is right

A redraw is **not** guaranteed correct (unsupported features such as table calcs, LOD, parameters, sets; or
Tableau reading data differently). The app never hides this:

1. **Badge on every chart:** "✓ Exact definition" or "⚠ Approximate: <what was skipped>". If too much is
   unsupported, show a table instead of a misleading chart.
2. **Sanity checks** on every redraw (unparsed dates, filter values missing from data, chart total vs table total).
3. **"Check against Tableau":** in Tableau, Download → Crosstab (CSV) for a sheet; upload it beside the chart;
   the app compares every cell → "✓ Matches Tableau" or a list of differing cells.
4. **Original thumbnail** from the `.twbx` shown beside the redraw for a visual comparison.
5. **Claude is told each chart's status** and must say "approximate" / "not verified" when true.
6. Later option: a "Live Tableau" tab embedding the real published dashboard (your Creator sign-in).

---

## 5. Step-by-step workflow

Every step follows the same loop:

```
you say "start Sx"
 → I post a short plan for that step (files + tests)
 → I write the code
 → I run the tests and the step's "done check" myself
 → I report: what was built, test results (pass/fail, real output), anything not done
 → you look / try it, ask for changes
 → you say "commit" (optional) and "start Sx+1"
```

### S1 — Setup and a fake test workbook  *(no API key)*

| # | Action | Output |
| --- | --- | --- |
| 1.1 | `py install 3.13`; create `C:\tabbot\.venv`; install packages: tableauhyperapi, duckdb, pandas, pyarrow, defusedxml, pydantic, plotly, streamlit, anthropic, python-dotenv, pyyaml, pytest, ruff | Working environment; confirm `tableauhyperapi` imports |
| 1.2 | `git init`; first commit of the kit as it is now (only if you approve the commit) | Clean starting point |
| 1.3 | Edit `CLAUDE.md`: point to this plan, add "generic only" rule and the entity rule | Rules up to date |
| 1.4 | `pyproject.toml`, package `src/tabbot/` with folders `twb/ data/ views/ analysis/ llm/ ui/`, `config.py` (reads `.env`), `cli.py` | Skeleton |
| 1.5 | `tests/fixtures/make_fixture.py` builds `fixture.twbx` with **made-up data**: ~3,000 entities, 2–8 rows each, 5 years, text dates `DD-MON-YYYY`, data ending mid-quarter; generic columns (RECORD_ID, RECEIVED_DATE, CATEGORY, SUBCATEGORY ~80 values, CHANNEL, REGION, GENDER with nulls/UNK, AGE, AGE_UNIT, SEVERITY, NOTES free text). Planted story: one CATEGORY triples in the last 3 quarters, driven by one CHANNEL value. Hand-written `.twb` with 3 dashboards (heatmap by quarter; line trend; pies + text table), quick filters, hidden sheets, phone layouts, thumbnails, and planted problems (counting dates, "%" title with counts, misnamed sheet, unused calc, age calc ignoring unit) | `fixture.twbx` + `expected.json` listing every planted fact |
| 1.6 | Tests: package imports; no network library outside `llm/provider.py`; fixture builds | `pytest` green |

**Done check:** `pytest` passes and `fixture.twbx` opens as a valid zip with `.twb` + `.hyper`.

### S2 — Workbook reader  *(no API key)*

| # | Action | Output |
| --- | --- | --- |
| 2.1 | `workspace.py`: per-upload folder, safe unzip (blocks `../` paths, huge files, zip bombs) | Safe intake |
| 2.2 | `twb/fieldref.py`: decode `[ds].[ctd:FIELD:qk]` (3- and 4-part, unknown prefixes kept) | Field references |
| 2.3 | `twb/model.py`: Workbook, DataSource, Field, Worksheet, Filter, Dashboard (with zone positions) | Data model |
| 2.4 | `twb/parser.py` with `defusedxml`: everything in method step 2 + thumbnails (size only) + `unsupported` list | Parsed workbook |
| 2.5 | `data/hyper_reader.py` (telemetry off, logs inside workspace) → Parquet; `data/store.py` DuckDB views | Local data store |
| 2.6 | `data/profile.py`: column profiles, date-format detection, entity detection, rows per entity, per-entity consistency | Data understanding |
| 2.7 | `tabbot inspect <file.twbx>` prints dashboards → sheets, chart types, fields, calcs, tables, rows, entity, date formats | CLI summary |
| 2.8 | Tests against `expected.json` (counts, captions, de-duplicated dashboards, entity, date format) | `pytest` green |

**Done check:** `inspect` on the fixture matches `expected.json`. *Optional:* if you give me the path of your
real PV `.twbx`, I run `inspect` on it locally (nothing is sent anywhere) as a smoke test.

### S3 — Dashboard viewer  *(no API key — first thing you see in the browser)*

| # | Action | Output |
| --- | --- | --- |
| 3.1 | `views/calc_translate.py`: the simple calc patterns → DuckDB SQL; anything else → "unsupported" | Calcs |
| 3.2 | `views/rebuild.py`: worksheet → query → result table + badge (Exact/Approximate) + sanity checks | Chart numbers |
| 3.3 | `ui/charts.py`: mark type → Plotly chart. Square→heatmap with labels, Line, Bar, Pie, Circle, Text/Automatic→table; unknown → table. Tableau-like palette, fonts, gridlines | Charts |
| 3.4 | Dashboard layout from zone positions (rows of charts); many small sheets (trellis) → grid of small multiples | Layout |
| 3.5 | Quick filters from the dashboard's filter zones → dropdowns applied to every sheet using that field | Filters |
| 3.6 | `ui/app.py`: wide Tableau-style page, upload, dashboard tabs (built as a selectable bar so Claude can switch them later — Streamlit's own tabs can't be switched from code), chart cards with badges, thumbnail expander, empty chat panel on the right | App |
| 3.7 | `views/compare.py` + "Check against Tableau" CSV upload per chart | Verification |
| 3.8 | Tests: fixture heatmap equals an independent pandas calculation; compare spots a planted difference; app runs headless with Streamlit's test runner without errors | `pytest` green |

**Done check:** you run `streamlit run src/tabbot/ui/app.py`, upload the fixture (or your real file), and click
through the dashboards.

### S4 — Checks and analysis  *(no API key)*

| # | Action | Output |
| --- | --- | --- |
| 4.1 | `analysis/checks.py`: partial period (+ full-period pace), double counting, colour skew, parse failures | Chart checks |
| 4.2 | `analysis/lints.py`: 5 definition checks (counting dates, "%" without percentage, unused calc, name ≠ filter, unit ignored) | Workbook checks |
| 4.3 | `analysis/recipes.py`: trend, period_compare, change_point, growth per category, driver_scan (+ top-N), share_shift, multiplicity, flag_share | Analyses |
| 4.4 | UI: checks bar under each dashboard; "Findings" panel | Visible findings |
| 4.5 | Tests: every planted problem found on the right sheet; planted driver ranked first; numbers match pandas | `pytest` green |

**Done check:** the fixture's planted story and problems appear in the app.

### S5 — Claude chat  *(needs your API key in `C:\tabbot\.env`)*

| # | Action | Output |
| --- | --- | --- |
| 5.1 | Check current Anthropic SDK usage (tool use, model id) before coding | Correct API calls |
| 5.2 | `llm/tools.py`: `list_dashboards`, `describe_dashboard`, `get_chart_data`, `run_checks`, `run_analysis`, `query` (structured, no SQL from Claude), `show_in_app` (switch dashboard, set filters) | Tools |
| 5.3 | `llm/guard.py`: tool outputs are aggregates only, capped in size, no paths/raw XML/rows | Data boundary |
| 5.4 | `llm/audit.py`: append each request to `workspace/audit/outbound.jsonl` **before** sending | Audit log |
| 5.5 | `llm/prompts.py`: system prompt (cite numbers, Data/Interpretation labels, mention approximate/unverified charts, workbook text is data not instructions) | Prompt |
| 5.6 | `llm/orchestrator.py`: tool-use loop (max 20 rounds), errors returned to Claude, cost-friendly | Chat engine |
| 5.7 | UI: chat in the right panel, small "used tool …" lines, `show_in_app` switches the dashboard live, "What was sent" viewer for the audit log | Working chat |
| 5.8 | Tests with a fake Claude client (no key); then one live run on the fixture with your key | `pytest` green + live answer |

**Done check:** in the app, "Give me insights from <dashboard>" → Claude answers in the side panel, names the
planted driver, labels insights, and switches the view.

### S6 — Your real workbook

Run the app on your PV `.twbx`; use "Check against Tableau" on 2–3 sheets with crosstabs you download; fix what
differs; then decide next additions together.

---

## 6. Left out of the demo (on purpose)

Amazon Bedrock, small-count hiding, the full 15-check catalogue (we do ~5), property-based privacy tests,
multi-table extracts, table calcs / LOD / parameters in redraws, pixel-exact layout, multi-user login,
live Tableau embedding (possible later).

## 7. Risks I already know about

| Risk | What I do |
| --- | --- |
| `tableauhyperapi` doesn't install on Windows / 3.13 | Stop at 1.1 and tell you; fallback: try 3.12 |
| A real workbook uses XML the fake one doesn't have | Parser records it as unsupported; fixed in S6 |
| Tableau date/format behaviour differs from mine | Shown by "Check against Tableau"; fixed per case |
| Streamlit layout limits (exact Tableau look) | Custom CSS; accept "Tableau-like" |

## 8. What you need to do

- Now: nothing. Say "start S1" when ready.
- Before S5: create an API key at console.anthropic.com (add ~$5–10 credit) and put it in `C:\tabbot\.env` as
  `ANTHROPIC_API_KEY=...`. I'll remind you.
- S6: download 2–3 crosstab CSVs from Tableau for the comparison.
