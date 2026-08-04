from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .i18n import tr


PROVIDERS_FILE = Path(__file__).resolve().parent.parent / "providers.json"


def load_provider_presets() -> list[dict[str, Any]]:
    try:
        payload = json.loads(PROVIDERS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = []
    if not isinstance(payload, list):
        payload = []

    providers: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            provider = {
                "id": str(item["id"]),
                "name": str(item["name"]),
                "host": str(item["host"]),
                "port": int(item.get("port", 993)),
                "security": str(item.get("security", "ssl")),
                "password_hint": str(item.get("password_hint", "")),
                "domains": [
                    str(domain).lower()
                    for domain in item.get("domains", [])
                    if isinstance(domain, str)
                ],
            }
        except (KeyError, TypeError, ValueError):
            continue
        if (
            provider["id"]
            and provider["name"]
            and provider["host"]
            and 1 <= provider["port"] <= 65535
        ):
            providers.append(provider)

    providers.append(
        {
            "id": "generic",
            "name": tr("Outro servidor IMAP"),
            "host": "",
            "port": 993,
            "security": "ssl",
            "password_hint": tr(
                "Use a senha IMAP fornecida pelo seu provedor. Se a conta usar "
                "verificação em duas etapas, procure pela opção de senha de "
                "aplicativo."
            ),
            "domains": [],
        }
    )
    return providers
