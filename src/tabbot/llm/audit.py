"""Local audit log of every request sent to Claude, written BEFORE the request is sent.

workspace/audit/outbound.jsonl - one JSON object per request: time, sha256, model, size, full request body.
No API key or HTTP headers are ever written (they are not part of the request body).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path


def _default(o):
    if hasattr(o, "to_dict"):
        return o.to_dict()
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json", exclude_none=True)
    return str(o)


class AuditLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, request: dict, dry_run: bool = False) -> dict:
        body = json.dumps(request, default=_default, ensure_ascii=False, sort_keys=True)
        entry = {
            "ts": dt.datetime.now().isoformat(timespec="seconds"),
            "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "model": request.get("model"),
            "chars": len(body),
            "approx_tokens": len(body) // 4,
            "dry_run": dry_run,
            "request": json.loads(body),
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def read(self, last: int = 20) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return [json.loads(x) for x in lines[-last:] if x.strip()]
