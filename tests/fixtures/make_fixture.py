"""Build the synthetic test workbook. The generator lives in tabbot.demo_workbook (also used by the app's demo button).

Usage:  python tests/fixtures/make_fixture.py [out_dir]
"""

import sys
from pathlib import Path

from tabbot.demo_workbook import build

__all__ = ["build"]

if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "out"
    path = build(target)
    print(f"wrote {path} ({path.stat().st_size // 1024} KB) and expected.json")
