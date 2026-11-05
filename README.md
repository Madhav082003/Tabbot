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

## Chat with Claude

1. Create an API key at https://console.anthropic.com (separate from a Claude.ai subscription) and add credit.
2. Copy `.env.example` to `.env` and paste the key after `ANTHROPIC_API_KEY=`. `.env` is never committed.
3. Restart the app. The chat panel on the right is now active.

Claude only sees workbook structure and summary numbers computed on this machine - never data rows, file paths or
values of id/free-text fields. Every request is written to `workspace/audit/outbound.jsonl` before it is sent and can
be inspected in the app ("What was sent to Claude"). Set `DRY_RUN=true` in `.env` to log requests without sending.
Default model: `claude-opus-5-5`; set `CLAUDE_MODEL=claude-sonnet-5-5` for a cheaper, faster model.

### Spend safety

- Every API call is logged with its tokens and estimated cost in `workspace/audit/usage.jsonl`;
  `.venv\Scripts\tabbot usage` prints totals per day, and the chat panel shows today's spend against the limit.
- Before each request the app checks the limits in `.env` (per question, per day, calls per day, calls per
  question). A request that could pass one is **not sent**; the chat says why and the block is logged.
- Loop guard: the same tool call repeated within one question is not re-run, and a third repeat stops the question.
- Also set a monthly spend limit on the key in the Anthropic console - that is the hard cap.
- Tests never use your key: `pytest` hides it and skips the live test. To run the live test deliberately:
  `$env:TABBOT_LIVE_TEST="1"; .venv\Scripts\python -m pytest -m llm` (one question, capped at about $0.40).

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
