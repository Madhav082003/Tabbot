import json
import os
import sys
from pathlib import Path

# SAFETY: tests never see a real API key (it may be a company key with real money behind it).
# An empty variable wins over .env (dotenv never overrides existing variables), so no test can reach the API.
# The single live test runs only when you set TABBOT_LIVE_TEST=1 yourself.
if os.environ.get("TABBOT_LIVE_TEST") != "1":
    os.environ["ANTHROPIC_API_KEY"] = ""
    os.environ["ANTHROPIC_AUTH_TOKEN"] = ""

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
sys.path.insert(0, str(FIXTURES))


@pytest.fixture(scope="session")
def fixture_dir(tmp_path_factory) -> Path:
    """Build the synthetic workbook once per test session into a temp folder."""
    import make_fixture

    out = tmp_path_factory.mktemp("fixture")
    make_fixture.build(out)
    return out


@pytest.fixture(scope="session")
def fixture_twbx(fixture_dir) -> Path:
    return fixture_dir / "fixture.twbx"


@pytest.fixture(scope="session")
def expected(fixture_dir) -> dict:
    return json.loads((fixture_dir / "expected.json").read_text(encoding="utf-8"))
