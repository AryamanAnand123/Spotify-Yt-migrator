"""Resolve private storage without creating files or reading credentials."""

import os
from pathlib import Path


def data_directory() -> Path:
    override = os.environ.get("SPOTTOYT_DATA_DIR")
    if override is not None:
        if not override.strip():
            raise ValueError("SPOTTOYT_DATA_DIR must not be empty.")
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise ValueError("SPOTTOYT_DATA_DIR must be an absolute path.")
        return path
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "Spottoyt" if base else Path.home() / ".local" / "share" / "spottoyt"
