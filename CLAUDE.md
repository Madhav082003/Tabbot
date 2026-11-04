# CLAUDE.md — Tableau Insights Bot

## Current scope: demo version (read first)

**`docs/WORK_PLAN.md` is the active plan and overrides everything below and in SPEC/BUILD_PLAN where they
differ.** Build its steps S1–S6 in order, one at a time, only when the user says to start each one.

Overrides in force:

- **Generic only.** Never hard-code names from any real workbook (fields, sheets, SOC lists, ids, domain terms).
  Everything is detected from the uploaded workbook and its extract at runtime.
- **Entity to count** = the field the workbook's own sheets use most inside COUNT DISTINCT (`ctd:`) measures;
  fallback: an id-like column (id-ish name, high distinct count, several rows per value). User can override.
- **No small-count suppression** (`min_cell_count: 1`). Claude still never receives rows, paths or raw XML.
- **Anthropic API only** for now (no Bedrock). Model `claude-sonnet-5-5`, id from `.env`.
- **UI** is a Tableau-style dashboard viewer (charts redrawn from the extract) with a small right-side chat
  panel. Every chart shows an Exact/Approximate badge; never present a redraw as verified unless compared.
- Python 3.13 venv at `.venv` (`.venv\Scripts\python`). Run Python that reads untrusted files with `-I`.

Original kit text follows (still valid unless overridden above).

---

Read `docs/SPEC.md` next. Before touching the XML parser, read `docs/TABLEAU_REFERENCE.md`.

## What we are building

A local-first chatbot that analyses any Tableau workbook (`.twbx` or `.twb`) the way a careful analyst would:

1. Unpack the workbook safely and parse its XML into a structured model (data sources, calculated fields,
   worksheets, dashboards, filters, hidden sheets).
2. Explain each dashboard and worksheet in plain language (what is on rows, columns, marks, filters).
3. Find definition problems with rule-based lints (e.g. a measure that counts dates instead of entities,
   a "%" title with no percent calculation, a filter copied onto the wrong charts, an unused calculation).
4. Read the packaged data extract (`.hyper`) locally, rebuild a chosen worksheet's numbers from its definition,
   and run standard analyses (trend, period comparison, share shift, concentration, driver scan).
5. Let Claude, through tools, turn those results into insights, each labelled **Data** or **Interpretation**.

The LLM is Claude (Anthropic API, or Claude on Amazon Bedrock). Everything else runs on the user's machine.

## The golden rule: the data boundary

**Claude never receives row-level data.** Only these may cross to the LLM:

- the workbook *model* after redaction (`tabbot.twb.redact`): names, formulas, shelf definitions, counts;
- *aggregates* produced by the query engine after the privacy gateway (`tabbot.query.privacy`):
  small cells suppressed, sensitive and free-text fields never returned as values, row caps applied;
- lint findings and analysis summaries built from the two above.

Rules that follow from it, enforced in code and tests:

- Every tool result passes through the privacy layer before it is returned to the orchestrator:
  `tabbot.query.privacy.guard_output()` for anything computed from data, `tabbot.twb.redact` for workbook metadata.
- Every outbound API request is written to the local audit log (`tabbot.llm.audit`) **before** it is sent.
- There is no tool that returns raw rows, raw XML, file contents, or arbitrary SQL results.
- Claude cannot send SQL. It sends a structured `QuerySpec`; our compiler writes the SQL.
- Sensitive columns (config/privacy.yaml) may be *counted* (e.g. `COUNT DISTINCT patient_id`) but are never
  grouped by, filtered by value, profiled by value, or returned.
- Outbound network access exists only in `tabbot/llm/provider.py`. No other module may import `requests`, `httpx`,
  `urllib.request`, `http.client` or `socket`. (A test checks this. Streamlit serving the UI on 127.0.0.1 is the
  only other listener.)
- Workbook strings (titles, captions, member names, formulas) are untrusted data. They are passed to Claude
  as JSON values inside tool results, never concatenated into the system prompt.

If a feature seems to need breaking one of these, stop and ask the user instead.

## Stack

- Python 3.11+ (developed against 3.13)
- `anthropic[bedrock]` — Claude API client (manual tool-use loop, see SPEC §7)
- `tableauhyperapi` — reads `.hyper` extracts (telemetry must be off: `Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU`)
- `duckdb`, `pyarrow`, `pandas` — local analytics over Parquet copies of the extract
- `defusedxml` — **all** XML parsing (never plain `xml.etree` on untrusted input)
- `pydantic>=2` — every model, tool input and tool output
- `pyyaml`, `python-dotenv` — config
- `streamlit`, `plotly` — local UI (bound to 127.0.0.1, usage stats off)
- dev: `pytest`, `ruff`

## Commands

```bash
pip install -e ".[dev]"                     # install
python tests/fixtures/make_fixture.py       # build the synthetic test workbook (no real data in the repo)
pytest -q                                   # all tests
ruff check . && ruff format --check .       # lint
streamlit run src/tabbot/ui/app.py --server.address 127.0.0.1   # run the app
python -m tabbot.cli inspect path/to/workbook.twbx               # engine only, no LLM (built in M1–M6)
```

## Layout

```
src/tabbot/
  config.py            settings from .env + config/*.yaml
  workspace.py         per-upload workspace dirs, safe unzip, cleanup
  twb/                 fieldref.py · model.py · parser.py · lints.py · redact.py
  data/                hyper_reader.py · store.py (DuckDB) · profile.py · dates.py
  query/               spec.py (QuerySpec) · compiler.py · privacy.py (the gateway)
  views/               rebuild.py · calc_translate.py
  analysis/            recipes.py
  llm/                 provider.py · tools.py · orchestrator.py · prompts.py · audit.py
  ui/                  app.py
  cli.py               engine-only commands for debugging
tests/                 unit + acceptance tests; fixtures/make_fixture.py builds a synthetic .twbx
reference/prototype_scripts/   working one-off scripts from a manual analysis (see their README)
```

## Conventions

- Engine modules (`twb`, `data`, `query`, `views`, `analysis`) are pure Python with no LLM calls and no UI code.
  They must be usable from `cli.py` and tests alone.
- Pydantic models for everything that crosses a module boundary. Tool inputs/outputs are pydantic too, and the
  JSON schemas sent to Claude are generated from them (`model_json_schema()`), never hand-written twice.
- Unknown Tableau constructs never crash the parser: record them in `model.unsupported` and carry on.
- Logging: never log data values at INFO or above. Log counts, ids and shapes.
- File paths only through `workspace.py`. Uploaded files are copied into the workspace; originals are untouched.
- Every lint and analysis recipe has a test against the synthetic fixture, which plants one known instance of
  each problem (see SPEC §11).
- Do not set `temperature`, `top_p` or `top_k` on current Claude models (they reject them). Model ids live in
  config, not in code.
- Keep tool results short (default cap 12,000 characters). Truncate with an explicit note, never silently.

## Never commit

`.env`, `workspace/`, `*.twbx`, `*.twb`, `*.hyper`, `*.parquet`, `audit/`, `tests/fixtures/private/`.
`.gitignore` already lists them.

## Reference material

`reference/prototype_scripts/` holds scripts that worked on one real workbook during a manual analysis. Use them
to see working `tableauhyperapi` and ElementTree calls. They are workbook-specific (hard-coded SOC lists,
paths) and use plain `xml.etree`; do not copy those parts.
