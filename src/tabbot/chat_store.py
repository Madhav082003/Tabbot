"""Saved chats: one JSON file per chat in <workspace>/chats/ (git-ignored, stays on this computer).

A chat keeps what the user saw (questions, answers, tool lines) and the conversation history sent to Claude, so it
can be read again at any time and continued with its context - continuing needs the same workbook open again
(matched by the file's sha256), because no copy of a workbook is kept after it is closed.

No network, no LLM: plain files. A file that cannot be read is skipped, never fatal.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

ID_RE = re.compile(r"^[0-9a-f]{12}$")


@dataclass
class SavedChat:
    id: str
    workbook: str                     # workbook name without extension, shown in the list
    source_name: str                  # the file name as uploaded
    digest: str                       # sha256 of that file: the chat can continue only on the same workbook
    created: str
    updated: str
    log: list[dict] = field(default_factory=list)        # what the chat panel shows: {role, text, tools, meta, about}
    messages: list[dict] = field(default_factory=list)   # conversation history sent to Claude (for continuing)

    @property
    def first_question(self) -> str:
        return next((m.get("text", "") for m in self.log if m.get("role") == "user"), "")

    @property
    def questions(self) -> int:
        return sum(1 for m in self.log if m.get("role") == "user")


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="microseconds")    # fine enough to order chats saved together


def new_chat_id() -> str:
    return uuid.uuid4().hex[:12]


class ChatStore:
    def __init__(self, folder: Path):
        self.dir = Path(folder)

    def _path(self, chat_id: str) -> Path:
        if not ID_RE.match(chat_id or ""):
            raise ValueError("invalid chat id")
        return self.dir / f"{chat_id}.json"

    def save(self, chat: SavedChat) -> SavedChat:
        self.dir.mkdir(parents=True, exist_ok=True)
        chat.updated = _now()
        tmp = self._path(chat.id).with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(chat), ensure_ascii=False, default=str), encoding="utf-8")
        tmp.replace(self._path(chat.id))          # atomic: a crash never leaves half a chat behind
        return chat

    def load(self, chat_id: str) -> SavedChat | None:
        try:
            data = json.loads(self._path(chat_id).read_text(encoding="utf-8"))
            return SavedChat(**{k: data[k] for k in SavedChat.__dataclass_fields__ if k in data})
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def list(self) -> list[SavedChat]:
        """Every saved chat, most recently updated first."""
        if not self.dir.exists():
            return []
        chats = [c for c in (self.load(p.stem) for p in self.dir.glob("*.json") if ID_RE.match(p.stem)) if c]
        return sorted(chats, key=lambda c: c.updated, reverse=True)

    def delete(self, chat_id: str) -> None:
        self._path(chat_id).unlink(missing_ok=True)


def start_chat(lw) -> SavedChat:
    """A new (not yet saved) chat for the open workbook."""
    now = _now()
    return SavedChat(id=new_chat_id(), workbook=lw.display_name, source_name=lw.source_name, digest=lw.digest,
                     created=now, updated=now)
