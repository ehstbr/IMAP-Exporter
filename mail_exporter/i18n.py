from __future__ import annotations

import json
import locale
import os
from pathlib import Path
from typing import Any

from .paths import data_dir


DEFAULT_LANGUAGE = "pt_BR"
SUPPORTED_LANGUAGES = ("pt_BR", "en")
LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"
LANGUAGE_PREFERENCE_FILE = "language"

_language = DEFAULT_LANGUAGE
_messages: dict[str, str] = {}


def _normalize_language(value: str | None) -> str:
    normalized = (value or "").replace("-", "_").lower()
    return "pt_BR" if normalized.startswith("pt") else "en"


def _system_language() -> str:
    preferred = os.environ.get("LANGUAGE") or os.environ.get("LC_ALL")
    preferred = preferred or os.environ.get("LC_MESSAGES") or os.environ.get("LANG")
    if not preferred:
        try:
            preferred = locale.getlocale()[0]
        except (TypeError, ValueError):
            preferred = None
    return _normalize_language(preferred)


def _preference_path() -> Path:
    return data_dir() / LANGUAGE_PREFERENCE_FILE


def _load_messages(language: str) -> dict[str, str]:
    path = LOCALES_DIR / f"{language}.json"
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in payload.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def _initial_language() -> str:
    try:
        saved = _preference_path().read_text(encoding="utf-8").strip()
    except OSError:
        saved = ""
    if saved in SUPPORTED_LANGUAGES:
        return saved
    return _system_language()


def set_language(language: str, persist: bool = True) -> str:
    global _language, _messages
    normalized = language if language in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE
    _language = normalized
    _messages = _load_messages(normalized)
    if persist:
        _preference_path().write_text(normalized + "\n", encoding="utf-8")
    return normalized


def get_language() -> str:
    return _language


def tr(message: str, **values: Any) -> str:
    translated = _messages.get(message, message)
    return translated.format(**values) if values else translated


set_language(_initial_language(), persist=False)
