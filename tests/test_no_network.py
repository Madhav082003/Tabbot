"""Only tabbot/llm/provider.py may import network libraries (data boundary rule in CLAUDE.md)."""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "tabbot"
FORBIDDEN = {"requests", "httpx", "urllib.request", "http.client", "socket", "aiohttp", "urllib3"}
ALLOWED = {SRC / "llm" / "provider.py"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{a.name}" for a in node.names)
    return names


def test_no_network_imports_outside_provider():
    offenders = {}
    for py in SRC.rglob("*.py"):
        if py in ALLOWED:
            continue
        bad = {n for n in _imports(py) if n in FORBIDDEN or any(n.startswith(f + ".") for f in FORBIDDEN)}
        if bad:
            offenders[str(py.relative_to(SRC))] = sorted(bad)
    assert not offenders, f"network imports outside llm/provider.py: {offenders}"
