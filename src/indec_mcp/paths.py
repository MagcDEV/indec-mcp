"""Where the mirror file lives when nobody says otherwise."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]


def user_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "indec-mcp"


def in_checkout() -> bool:
    """True when running from a clone of the repo (editable install), not from a wheel."""
    return (_REPO / "pyproject.toml").is_file() and (_REPO / "src" / "indec_mcp").is_dir()


def default_db() -> Path:
    """`INDEC_DB` if set; else `data/` in a repo checkout; else the per-user data dir."""
    env = os.environ.get("INDEC_DB")
    if env:
        return Path(env)
    if in_checkout():
        return _REPO / "data" / "indec.duckdb"
    return user_data_dir() / "indec.duckdb"
