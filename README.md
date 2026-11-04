# Tableau Insights Bot — build kit

A local chatbot that explains any Tableau workbook, finds definition problems, rebuilds dashboard numbers from the
packaged extract and writes evidence-backed insights. Claude does the reasoning through tools; your data stays on
your machine and only redacted metadata and privacy-filtered aggregates are sent, every request logged locally.

## Start here

1. Make an empty folder, copy everything in this kit into it, `git init`, commit.
2. Open it in VS Code with the Claude Code extension.
3. Open `docs/BUILD_PLAN.md` and paste the M0 prompt into Claude Code. Continue milestone by milestone.

| File | For |
| --- | --- |
| `CLAUDE.md` | Rules Claude Code follows on every task (data boundary, stack, commands, layout) |
| `docs/SPEC.md` | What to build: architecture, engine modules, tools, orchestrator, prompt, UI, config, tests, security |
| `docs/TABLEAU_REFERENCE.md` | How `.twbx` / `.twb` / `.hyper` are structured, marked verified vs assumed |
| `docs/BUILD_PLAN.md` | Ten milestones with "done when" checks and copy-paste prompts |
| `config/privacy.yaml`, `.env.example`, `.streamlit/config.toml`, `.gitignore` | Ready-made config |
| `reference/prototype_scripts/` | Working scripts from a manual analysis, for API usage only |
