"""Check private-storage defaults and invalid configuration paths."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from spottoyt.cli import app
from spottoyt.config import data_directory


def test_default_storage_is_outside_project(monkeypatch, tmp_path):
    monkeypatch.delenv("SPOTTOYT_DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert data_directory() == tmp_path / "Spottoyt"
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert not (tmp_path / "Spottoyt").exists()


@pytest.mark.parametrize("value", ["", "   ", "relative/path"])
def test_reject_invalid_storage_override(monkeypatch, value):
    monkeypatch.setenv("SPOTTOYT_DATA_DIR", value)
    with pytest.raises(ValueError):
        data_directory()
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 2
    assert "Configuration error" in result.output


def test_explicit_absolute_storage_override(monkeypatch, tmp_path):
    monkeypatch.setenv("SPOTTOYT_DATA_DIR", str(tmp_path))
    assert data_directory() == Path(tmp_path)
