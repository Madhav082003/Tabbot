# Tableau Insights Bot (demo)

Open any Tableau workbook (`.twbx`) on your laptop, see its dashboards redrawn from its own data in a
Tableau-style viewer, and (from step S5) ask Claude about them. Data stays on your machine.

Plan and status: `docs/WORK_PLAN.md`. Rules for Claude Code: `CLAUDE.md`.

## Run the app

```powershell
cd C:\tabbot
.venv\Scripts\streamlit run src\tabbot\ui\app.py
```

Opens at http://127.0.0.1:8501 (local only). Use **Open workbook** to load a `.twbx`, or **Try the demo workbook**.

## Engine commands (no app, no LLM)

```powershell
.venv\Scripts\tabbot inspect path\to\workbook.twbx   # what the bot understood: sheets, filters, calcs, data
.venv\Scripts\python -m pytest -q                    # tests (built on a synthetic workbook, no real data)
```

## Setup from scratch

```powershell
py install 3.13
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
```
