"""A readable log of every chat question: what went to Claude and what came back.

workspace/logs/chat_YYYY-MM-DD.md     - for people: open it in any editor
workspace/logs/chat_YYYY-MM-DD.jsonl  - the same events, one JSON object per line (for tools)

Per question it records:
- the workbook and which data the app could read (packaged files, tables and row counts, notes);
- the question and the app context sent with it;
- every round: Claude's text, the tools Claude asked to run (with their inputs), and the exact tool
  results sent back to Claude - this is the ONLY workbook data Claude ever sees;
- the final answer, tokens and estimated cost, or why the question stopped.

Claude never receives the workbook or its data files; it only receives the tool results logged here.
The API key is never part of any of this. workspace/ is git-ignored, so the log stays on this computer.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

FENCE = "````"   # four backticks: tool results may themselves contain ``` fences


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _pretty(text: str) -> str:
    try:
        return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(text)


def data_status(lw) -> list[str]:
    """Plain lines: what data the app has for this workbook (and so what the tools can compute)."""
    out = [f"- {s}" for s in (lw.data_files or [])]
    for t in lw.tables or []:
        out.append(f"- table {t.name} from {t.source_file}: {t.rows:,} rows, {len(t.columns)} columns")
    if not lw.tables:
        out.append("- NO data table loaded: Claude can describe the structure but cannot compute numbers")
    out += [f"- note: {n}" for n in (lw.notes or [])]
    return out


class ChatLog:
    def __init__(self, log_dir: Path, session_id: str, workbook: str):
        self.dir = Path(log_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.session_id, self.workbook = session_id, workbook

    def _paths(self) -> tuple[Path, Path]:
        day = dt.date.today().isoformat()
        return self.dir / f"chat_{day}.md", self.dir / f"chat_{day}.jsonl"

    def _write(self, event: dict, md: str) -> None:
        md_path, js_path = self._paths()
        event = {"ts": _now(), "session": self.session_id, "workbook": self.workbook, **event}
        try:   # a log that cannot be written (file open elsewhere, disk full) must never break the chat
            with js_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
            with md_path.open("a", encoding="utf-8") as f:
                f.write(md.rstrip() + "\n\n")
        except OSError:
            pass

    # ------------------------------------------------------------------ events
    def question(self, qid: str, text: str, app_context: str | None, data: list[str], model: str,
                 dry_run: bool) -> None:
        md = [f"---\n\n## {_now()} · question {qid}",
              f"**Workbook:** {self.workbook}  ·  **session** {self.session_id}  ·  **model** {model}"
              + ("  ·  DRY RUN (nothing sent)" if dry_run else ""),
              "**Data the app could read for this workbook** (Claude never gets these files, only tool results):",
              "\n".join(data) or "- (none)",
              f"**Question:** {text}"]
        if app_context:
            md.append(f"**App context sent with it:** {app_context}")
        self._write({"event": "question", "qid": qid, "question": text, "app_context": app_context,
                     "data": data, "model": model, "dry_run": dry_run}, "\n\n".join(md))

    def sent(self, qid: str, rnd: int, chars: int, n_messages: int) -> None:
        self._write({"event": "sent", "qid": qid, "round": rnd, "chars": chars, "messages": n_messages},
                    f"### Round {rnd} → sent to Claude\n\n{n_messages} message(s) of history, "
                    f"{chars:,} characters (system prompt and tool list included; full body in audit/outbound.jsonl)")

    def received(self, qid: str, rnd: int, texts: list[str], tool_uses: list[dict], stop: str | None,
                 usage: dict, est_usd: float) -> None:
        md = [f"### Round {rnd} ← Claude replied (stop: {stop}; in {usage.get('input_tokens', 0):,} "
              f"+{usage.get('cache_read_input_tokens', 0):,} cached, out {usage.get('output_tokens', 0):,}, "
              f"≈ ${est_usd:.4f})"]
        md += [t for t in texts if t.strip()]
        for tu in tool_uses:
            md.append(f"Claude asks to run **{tu.get('name')}** with input `"
                      f"{json.dumps(tu.get('input') or {}, ensure_ascii=False)}`")
        self._write({"event": "received", "qid": qid, "round": rnd, "texts": texts, "stop": stop,
                     "tool_uses": [{"name": t.get("name"), "input": t.get("input")} for t in tool_uses],
                     "usage": usage, "est_usd": est_usd}, "\n\n".join(md))

    def tool_result(self, qid: str, rnd: int, name: str, inp: dict, out: str, is_error: bool) -> None:
        head = f"**Tool result sent back to Claude: {name}**" + (" (ERROR)" if is_error else "")
        self._write({"event": "tool_result", "qid": qid, "round": rnd, "name": name, "input": inp,
                     "is_error": is_error, "result": out},
                    f"{head}\n\n{FENCE}json\n{_pretty(out)}\n{FENCE}")

    def answer(self, qid: str, text: str, rounds: int, usage: dict, est_usd: float,
               stopped: str | None = None) -> None:
        md = [f"### Final answer ({rounds} round(s), ≈ ${est_usd:.4f} for this question)"]
        if stopped:
            md.append(f"**Stopped:** {stopped}")
        md.append(text or "_(no answer)_")
        self._write({"event": "answer", "qid": qid, "text": text, "rounds": rounds, "usage": usage,
                     "est_usd": est_usd, "stopped": stopped}, "\n\n".join(md))
