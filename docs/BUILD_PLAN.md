# Build plan

Ten milestones. Each one ends with green tests and a commit, so you can stop after any of them with something
that works. M1–M7 need no API key: the whole engine is testable offline. Claude is wired in at M8.

## How to run this with Claude Code in VS Code

1. Create an empty folder, e.g. `tableau-insights-bot/`, and copy this kit into it (`CLAUDE.md` at the root).
2. `git init` and commit the kit, so every milestone is a clean diff.
3. Open the folder in VS Code and open the Claude Code panel. It reads `CLAUDE.md` automatically.
4. Paste the prompt for M0 below. When it finishes, review the diff, run the "Done when" checks yourself, commit,
   then paste the next prompt.
5. If Claude Code proposes something that breaks the data boundary in `CLAUDE.md`, say no: the rule matters more
   than the feature.

Tip: for each milestone, ask it to show a short plan first and only then write code.

---

## M0 — Scaffold

**Build:** `pyproject.toml` (package `tabbot` under `src/`, deps and `[dev]` extras from CLAUDE.md), the folder layout,
`config.py` (loads `.env` and `config/privacy.yaml` into pydantic settings), `.gitignore`, `.env.example`,
`config/privacy.yaml`, `.streamlit/config.toml`, empty modules, `cli.py` with a `--help`.
**Tests:** `test_imports.py`; `test_no_network.py` (fails if any module other than `llm/provider.py` imports
`requests`, `httpx`, `urllib.request`, `socket`, `http.client`).
**Done when:** `pip install -e ".[dev]"`, `pytest -q`, `ruff check .` all pass.

> Read CLAUDE.md and docs/SPEC.md. Do milestone M0 from docs/BUILD_PLAN.md: scaffold the project exactly as the
> layout in CLAUDE.md, with config loading per SPEC §10 and the two tests listed. Show me a short plan first.

## M1 — Synthetic fixture workbook

**Build:** `tests/fixtures/make_fixture.py` per SPEC §11. Create the `.hyper` with `tableauhyperapi`
(`Inserter`, `TableDefinition`, `SqlType`), table `"Extract"."Extract"`. Columns: `CASE_ID`, `RECEIVED_DATE`
(text `DD-MON-YYYY`), `CATEGORY` (10 values), `SUBCATEGORY`, `CHANNEL` (3 values), `COUNTRY`, `GENDER`
(incl. null and `UNK`), `AGE`, `AGE_UNIT` (mostly Years, a few Months/Days), `SERIOUS`, `OUTCOME`, `NOTES`
(free text). 2–8 rows per case, 3,000 cases over 5 years. Plant the story: from the last 3 quarters, one
`CATEGORY` triples, driven by one `CHANNEL` value. Write a `.twb` by hand (follow TABLEAU_REFERENCE §3–6) with:
a heatmap sheet (CATEGORY × year/quarter, COUNTD case), gender pies, age-group pies carrying a copied gender
filter (L3), an Age Group calc ignoring AGE_UNIT (L5), a sheet counting distinct dates (L1), a "%" dashboard
with count labels (L2), a misnamed sheet (L4), an unused calc (L6), a `COUNT([SUBCATEGORY])` calc used on a
sheet (L7), a key calc that is not unique (L8), a large exclude filter (L12, L13), a local path in the
connection (L14), a dashboard whose name contradicts its filter (L15), a placeholder title (L11), hidden sheets
(L10), phone layouts, thumbnails, and data that ends mid-quarter (L9). Zip to `tests/fixtures/out/fixture.twbx` with the `Data/... Files/` path.
Also write `tests/fixtures/out/expected.json` with the planted facts (counts, the driver, each lint's sheet).
**Done when:** the script runs in under a minute and `expected.json` lists every planted item.

> Do M1 from docs/BUILD_PLAN.md. Read docs/TABLEAU_REFERENCE.md first so the .twb uses the verified element
> paths. Plant every listed problem exactly once and record them in expected.json.

## M2 — Safe unpacking and the workbook parser

**Build:** `workspace.py` (SPEC §5.1), `twb/fieldref.py`, `twb/model.py`, `twb/parser.py` (SPEC §5.2), with
`size_report`. `cli.py inspect <file>` prints a summary (sources, field and calc counts, sheets by mark type,
dashboards with de-duplicated sheet lists, hidden count, sizes).
**Tests:** zip-slip and zip-bomb rejection; field-ref parsing table (3-part, 4-part, unknown prefix); parser
counts vs `expected.json`; calc caption mapping incl. a `(copy)_` internal name; dashboards de-duplicated.
**Done when:** `python -m tabbot.cli inspect tests/fixtures/out/fixture.twbx` matches `expected.json`.

> Do M2 from docs/BUILD_PLAN.md. Use defusedxml for parsing. Unknown constructs go to model.unsupported, never
> exceptions. Add the tests listed.

## M3 — Redaction, size report and metadata lints

**Build:** `twb/redact.py` (SPEC §5.5); lints L1–L6 and L10–L15 in `twb/lints.py` (no data needed);
`cli.py lint <file>` and `cli.py llm-view <file>` (prints exactly what Claude would see).
**Tests:** each planted lint found on the right sheet, none on clean sheets; redacted view contains no path,
user name, fingerprint attribute, thumbnail data or sensitive member value; size caps respected.
**Done when:** `cli lint` lists the planted metadata problems and nothing else.

> Do M3. Lints must be generic: no field names from the fixture or reference workbook in lint code.

## M4 — Extract reader, profiling and data lints

**Build:** `data/hyper_reader.py` (chunked export to Parquet), `data/store.py` (DuckDB views), `data/profile.py`,
`data/dates.py`, `entity_grain`; auto-detection of sensitive and free-text columns (SPEC §10); lints L7–L9.
`cli.py profile <file>`.
**Tests:** row counts match; text dates detected; `NOTES` flagged free text; `CASE_ID` flagged id; grain matches;
L7–L9 found.
**Done when:** `cli profile` shows tables, rows, types, date formats and suggested sensitive columns.

> Do M4. Hyper telemetry off and log_dir inside the workspace. Never print data values in logs.

## M5 — Query layer and privacy gateway

**Build:** `query/spec.py`, `query/compiler.py`, `query/privacy.py` (SPEC §5.6). Results stored as Parquet with
a `result_id`.
**Tests:** compiled SQL for representative specs (snapshot); gateway rejects sensitive dimensions and filters;
small-cell suppression including derived values; row cap; a Hypothesis-style property test with random specs that
asserts no sensitive value ever appears in output.
**Done when:** all gateway tests pass and `cli query '<json spec>'` prints a guarded table.

> Do M5. Claude will never send SQL; only QuerySpec. Use bound parameters and quoted identifiers.

## M6 — View rebuild

**Build:** `views/rebuild.py` and `views/calc_translate.py` (built-in patterns only), checks `double_count`,
`partial_period`, `empty_rows` (SPEC §5.7). `cli.py rebuild <file> <worksheet>`.
**Tests:** heatmap sheet rebuild equals a pandas reference computation; a sheet using an unsupported construct
returns `approximate=True` with the reason; date calcs from text dates work.
**Done when:** the fixture heatmap rebuilds exactly and the checks report the planted partial quarter.

> Do M6. If a calc cannot be translated by the built-in patterns, report it as unsupported; do not guess.

## M7 — Analysis recipes

**Build:** `analysis/recipes.py`: trend, period_compare, share_shift, concentration, driver_scan, multiplicity,
checks (SPEC §5.8). `cli.py analyze <file> <worksheet>` runs them all.
**Tests:** `driver_scan` ranks the planted `CHANNEL` value first; `period_compare` numbers match a pandas reference;
partial-period pace correct; everything passes through the gateway.
**Done when:** `cli analyze` on the heatmap sheet tells the planted story in numbers.

> Do M7. Every recipe returns a small summary table plus notes, through the privacy gateway.

## M8 — Claude: tools, orchestrator, audit

**Build:** `llm/provider.py` (anthropic + bedrock per SPEC §7), `llm/audit.py`, `llm/tools.py` (SPEC §6; schemas
generated from pydantic; 3–4 sentence descriptions), `llm/prompts.py` (SPEC §8), `llm/orchestrator.py` (manual
loop, audit before send, dry-run, max rounds, parallel tool execution, `is_error` on failures, output caps).
`cli.py chat <file>` for a terminal chat.
**Tests (no key):** mock client — audit written before every call; tool_result blocks first and in one message;
errors become `is_error`; dry-run never calls the client. **LLM tests** (`-m llm`, need a key): "insights from the
heatmap dashboard" mentions the planted driver and labels Data/Interpretation.
**Done when:** `cli chat` answers "what's in this workbook?" and "insights from <dashboard>" on the fixture, and
`workspace/audit/outbound.jsonl` holds only redacted metadata and guarded aggregates.

> Do M8. Use the manual tool-use loop from SPEC §7, not the SDK tool runner, so every request is audited before it
> is sent. Do not set temperature/top_p/top_k. Model id comes from .env.

## M9 — Streamlit UI

**Build:** `ui/app.py` per SPEC §9: upload, sensitive-column confirmation step, chat, tabs (Workbook, Findings,
Results, Charts, Outbound log), `ui_show_chart` rendering with Plotly, delete-workspace button.
**Done when:** a full session on the fixture works in the browser at 127.0.0.1, and the Outbound log shows each
request.

> Do M9. Keep all engine logic out of the UI file; the UI only calls the orchestrator and reads stored results.

## M10 — Hardening on real workbooks

**Build:** run the parser and lints on 2–3 real, non-sensitive workbooks placed in `tests/fixtures/private/`
(git-ignored), fix crashes, move anything new into `unsupported`, update `docs/TABLEAU_REFERENCE.md` (mark newly
verified items), add regression tests with synthetic snippets of any new XML shapes.
**Done when:** `pytest -m private` passes and the reference doc is updated.

> Do M10 using the workbooks in tests/fixtures/private. Update TABLEAU_REFERENCE.md: move items from [assumed]
> to [verified] only when you have seen them.

## Later

LLM-assisted calc translation (SPEC §5.7 step 4) · table calcs and LOD · Bedrock end-to-end test · FastAPI +
React front end · Tableau Cloud via REST API or Tableau MCP.
