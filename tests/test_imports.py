import importlib

import pytest

MODULES = ["tabbot", "tabbot.config", "tabbot.cli", "tabbot.workspace", "tabbot.twb", "tabbot.data",
           "tabbot.views", "tabbot.analysis", "tabbot.llm", "tabbot.ui"]


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name):
    importlib.import_module(name)


def test_settings_load_defaults():
    from tabbot.config import load_settings

    s = load_settings()
    assert s.claude_model
    assert s.privacy.min_cell_count == 1  # demo decision: no small-count suppression
    assert s.workspace_dir.is_absolute()


def test_cli_version(capsys):
    from tabbot.cli import main

    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == "0.1.0"
