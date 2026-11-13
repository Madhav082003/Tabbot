"""Copies of opened workbooks, so a saved chat can reopen its workbook and continue (KEEP_WORKBOOK_COPIES).

<workspace>/library/<sha256 of the file>/<original file name> - git-ignored, on this computer only. A copy is kept
only while some saved chat uses it: deleting the last chat of a workbook removes its copy, and copies no chat
uses are removed at startup. Nothing here is ever opened except by the app's own loader.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class WorkbookLibrary:
    def __init__(self, folder: Path):
        self.dir = Path(folder)

    def _slot(self, digest: str) -> Path:
        if not DIGEST_RE.match(digest or ""):
            raise ValueError("invalid workbook digest")
        return self.dir / digest

    def put(self, src: Path, digest: str) -> Path:
        """Keep a copy of the opened file (once per digest). Returns the copy's path."""
        slot = self._slot(digest)
        existing = self.get(digest)
        if existing is not None:
            return existing
        slot.mkdir(parents=True, exist_ok=True)
        target = slot / Path(src).name
        tmp = target.with_name(target.name + ".part")
        shutil.copy2(src, tmp)
        tmp.replace(target)                       # a half-copied file is never picked up
        return target

    def get(self, digest: str) -> Path | None:
        try:
            slot = self._slot(digest)
        except ValueError:
            return None
        files = [p for p in slot.glob("*") if p.is_file() and p.suffix.lower() in (".twbx", ".twb")] \
            if slot.exists() else []
        return files[0] if files else None

    def delete(self, digest: str) -> None:
        try:
            shutil.rmtree(self._slot(digest), ignore_errors=True)
        except ValueError:
            pass

    def prune(self, keep: set[str]) -> int:
        """Remove every copy whose digest is not in `keep`. Returns how many were removed."""
        if not self.dir.exists():
            return 0
        n = 0
        for slot in self.dir.iterdir():
            if slot.is_dir() and slot.name not in keep:
                shutil.rmtree(slot, ignore_errors=True)
                n += 1
        return n
