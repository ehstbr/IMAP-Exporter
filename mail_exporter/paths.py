from __future__ import annotations

import os
from pathlib import Path


APP_SLUG = "imap-exporter"
LEGACY_APP_SLUG = "gmail-header-exporter"


def data_dir() -> Path:
    override = os.environ.get("IMAP_EXPORTER_DATA_DIR")
    if override:
        path = Path(override)
        path.mkdir(parents=True, exist_ok=True)
        return path
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    current = base / APP_SLUG
    legacy = base / LEGACY_APP_SLUG
    path = legacy if legacy.exists() and not current.exists() else current
    path.mkdir(parents=True, exist_ok=True)
    return path


def database_path() -> Path:
    override = os.environ.get(
        "IMAP_EXPORTER_DB",
        os.environ.get("GMAIL_HEADER_EXPORTER_DB"),
    )
    return Path(override) if override else data_dir() / "dados.sqlite3"
