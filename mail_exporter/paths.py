from __future__ import annotations

import os
from pathlib import Path


APP_SLUG = "imap-exporter"


def data_dir() -> Path:
    override = os.environ.get("IMAP_EXPORTER_DATA_DIR")
    if override:
        path = Path(override)
        path.mkdir(parents=True, exist_ok=True)
        return path
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    path = base / APP_SLUG
    path.mkdir(parents=True, exist_ok=True)
    return path


def database_path() -> Path:
    override = os.environ.get("IMAP_EXPORTER_DB")
    return Path(override) if override else data_dir() / "imap-exporter.sqlite3"
