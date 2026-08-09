"""GitHub-hosted update manifest validation and version policy.

The module is deliberately independent from GTK.  It performs one small HTTPS
request, validates the complete schema and returns a typed result to the UI.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from functools import total_ordering
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import __version__


PROJECT_URL = "https://github.com/ehstbr/IMAP-Exporter"
RELEASES_URL = f"{PROJECT_URL}/releases"
LATEST_RELEASE_URL = f"{RELEASES_URL}/latest"
UPDATE_MANIFEST_URL = (
    "https://raw.githubusercontent.com/ehstbr/IMAP-Exporter/main/version.json"
)

SUPPORTED_SCHEMA_VERSION = 1
UPDATE_TIMEOUT_SECONDS = 5.0
MAX_MANIFEST_BYTES = 256 * 1024
MAX_SUMMARY_CHARS = 4_096
MAX_CHANGELOG_ITEMS = 200
MAX_CHANGELOG_ITEM_CHARS = 2_048

LOGGER = logging.getLogger("imap_exporter.update")


class UpdateStatus(str, Enum):
    CHECKING = "checking"
    UP_TO_DATE = "up_to_date"
    OPTIONAL_UPDATE_AVAILABLE = "optional_update_available"
    MANDATORY_UPDATE_REQUIRED = "mandatory_update_required"
    CHECK_FAILED = "check_failed"
    LOCAL_VERSION_NEWER = "local_version_newer"


class CheckSource(str, Enum):
    STARTUP = "startup"
    MANUAL = "manual"


class UpdateCheckError(ValueError):
    """Base class for safe, expected update-check failures."""


class InvalidManifestError(UpdateCheckError):
    """The remote document does not match the supported manifest contract."""


class UpdateNetworkError(UpdateCheckError):
    """The manifest could not be obtained safely over HTTPS."""


@total_ordering
@dataclass(frozen=True, eq=False)
class SemanticVersion:
    major: int
    minor: int
    patch: int
    prerelease: tuple[int | str, ...] = ()
    build: tuple[str, ...] = ()

    _PATTERN = re.compile(
        r"^(0|[1-9][0-9]*)\."
        r"(0|[1-9][0-9]*)\."
        r"(0|[1-9][0-9]*)"
        r"(?:-((?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-]"
        r"[0-9A-Za-z-]*)(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*"
        r"[A-Za-z-][0-9A-Za-z-]*))*))?"
        r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
    )

    @classmethod
    def parse(cls, value: str) -> "SemanticVersion":
        if not isinstance(value, str):
            raise InvalidManifestError("Version must be a string.")
        match = cls._PATTERN.fullmatch(value)
        if match is None:
            raise InvalidManifestError(f"Invalid semantic version: {value!r}.")
        prerelease: tuple[int | str, ...] = tuple(
            int(part) if part.isdigit() else part
            for part in (match.group(4) or "").split(".")
            if part
        )
        build = tuple((match.group(5) or "").split(".")) if match.group(5) else ()
        return cls(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
            prerelease,
            build,
        )

    def _precedence(self) -> tuple[int, int, int]:
        return self.major, self.minor, self.patch

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SemanticVersion):
            return NotImplemented
        return (
            self._precedence() == other._precedence()
            and self.prerelease == other.prerelease
        )

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemanticVersion):
            return NotImplemented
        if self._precedence() != other._precedence():
            return self._precedence() < other._precedence()
        if not self.prerelease:
            return False
        if not other.prerelease:
            return True
        for left, right in zip(self.prerelease, other.prerelease):
            if left == right:
                continue
            if isinstance(left, int) and isinstance(right, str):
                return True
            if isinstance(left, str) and isinstance(right, int):
                return False
            return left < right
        return len(self.prerelease) < len(other.prerelease)

    def __hash__(self) -> int:
        return hash((self.major, self.minor, self.patch, self.prerelease))


@dataclass(frozen=True)
class UpdateManifest:
    schema_version: int
    version: str
    semantic_version: SemanticVersion
    mandatory: bool
    released_at: datetime
    summary: str
    changelog: tuple[str, ...]


@dataclass(frozen=True)
class UpdateCheckResult:
    status: UpdateStatus
    source: CheckSource
    current_version: str
    remote_version: str | None = None
    manifest: UpdateManifest | None = None
    error: str | None = None


def _require_exact_type(
    payload: dict[str, Any], key: str, expected: type
) -> Any:
    if key not in payload:
        raise InvalidManifestError(f"Missing required field: {key}.")
    value = payload[key]
    if type(value) is not expected:
        raise InvalidManifestError(f"Invalid type for field: {key}.")
    return value


def parse_manifest_bytes(raw: bytes) -> UpdateManifest:
    if not isinstance(raw, bytes):
        raise InvalidManifestError("Manifest payload must be bytes.")
    if not raw or len(raw) > MAX_MANIFEST_BYTES:
        raise InvalidManifestError("Manifest is empty or exceeds the size limit.")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidManifestError("Manifest is not valid UTF-8 JSON.") from exc
    if not isinstance(payload, dict):
        raise InvalidManifestError("Manifest root must be a JSON object.")

    schema_version = _require_exact_type(payload, "schema_version", int)
    if schema_version != SUPPORTED_SCHEMA_VERSION:
        raise InvalidManifestError(
            f"Unsupported update manifest schema: {schema_version}."
        )
    version = _require_exact_type(payload, "version", str)
    if len(version) > 128:
        raise InvalidManifestError("Version field exceeds the size limit.")
    semantic_version = SemanticVersion.parse(version)
    mandatory = _require_exact_type(payload, "mandatory", bool)
    released_at_text = _require_exact_type(payload, "released_at", str)
    try:
        released_at = datetime.fromisoformat(
            released_at_text.replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise InvalidManifestError("released_at is not valid ISO 8601.") from exc
    if released_at.tzinfo is None:
        raise InvalidManifestError("released_at must include a timezone.")

    summary = _require_exact_type(payload, "summary", str).strip()
    if not summary or len(summary) > MAX_SUMMARY_CHARS:
        raise InvalidManifestError("Summary is empty or exceeds the size limit.")
    changelog_value = _require_exact_type(payload, "changelog", list)
    if len(changelog_value) > MAX_CHANGELOG_ITEMS:
        raise InvalidManifestError("Changelog exceeds the item limit.")
    changelog: list[str] = []
    for item in changelog_value:
        if type(item) is not str or not item.strip():
            raise InvalidManifestError("Every changelog item must be text.")
        cleaned = item.strip()
        if len(cleaned) > MAX_CHANGELOG_ITEM_CHARS:
            raise InvalidManifestError("A changelog item exceeds the size limit.")
        changelog.append(cleaned)

    return UpdateManifest(
        schema_version=schema_version,
        version=version,
        semantic_version=semantic_version,
        mandatory=mandatory,
        released_at=released_at,
        summary=summary,
        changelog=tuple(changelog),
    )


def evaluate_manifest(
    manifest: UpdateManifest,
    current_version: str,
    source: CheckSource,
) -> UpdateCheckResult:
    current = SemanticVersion.parse(current_version)
    if manifest.semantic_version > current:
        status = (
            UpdateStatus.MANDATORY_UPDATE_REQUIRED
            if manifest.mandatory
            else UpdateStatus.OPTIONAL_UPDATE_AVAILABLE
        )
    elif manifest.semantic_version == current:
        status = UpdateStatus.UP_TO_DATE
    else:
        status = UpdateStatus.LOCAL_VERSION_NEWER
    return UpdateCheckResult(
        status=status,
        source=source,
        current_version=current_version,
        remote_version=manifest.version,
        manifest=manifest,
    )


class _HTTPSOnlyRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        if urlparse(newurl).scheme.lower() != "https":
            raise UpdateNetworkError("The update manifest redirected outside HTTPS.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_manifest_bytes(
    url: str,
    timeout: float,
    user_agent: str,
    max_bytes: int,
) -> bytes:
    if urlparse(url).scheme.lower() != "https":
        raise UpdateNetworkError("The update manifest URL must use HTTPS.")
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": user_agent,
        },
        method="GET",
    )
    opener = build_opener(_HTTPSOnlyRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            final_url = response.geturl()
            if urlparse(final_url).scheme.lower() != "https":
                raise UpdateNetworkError(
                    "The update manifest response did not use HTTPS."
                )
            content_length = response.headers.get("Content-Length")
            if content_length is not None and int(content_length) > max_bytes:
                raise UpdateNetworkError("The update manifest is too large.")
            payload = response.read(max_bytes + 1)
    except UpdateNetworkError:
        raise
    except HTTPError as exc:
        raise UpdateNetworkError(f"HTTP error {exc.code}.") from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise UpdateNetworkError("The update manifest could not be downloaded.") from exc
    if len(payload) > max_bytes:
        raise UpdateNetworkError("The update manifest is too large.")
    return payload


Fetcher = Callable[[str, float, str, int], bytes]
ResultCallback = Callable[[UpdateCheckResult], None]


class UpdateService:
    """Single, coalescing update checker shared by startup and manual checks."""

    def __init__(
        self,
        *,
        current_version: str = __version__,
        manifest_url: str = UPDATE_MANIFEST_URL,
        timeout: float = UPDATE_TIMEOUT_SECONDS,
        fetcher: Fetcher = fetch_manifest_bytes,
    ) -> None:
        self.current_version = current_version
        self.manifest_url = manifest_url
        self.timeout = timeout
        self.fetcher = fetcher
        self._lock = threading.Lock()
        self._checking = False
        self._shutting_down = False
        self._callbacks: list[tuple[CheckSource, ResultCallback]] = []

    @property
    def is_checking(self) -> bool:
        with self._lock:
            return self._checking

    def shutdown(self) -> None:
        with self._lock:
            self._shutting_down = True
            self._callbacks.clear()

    def check(self, source: CheckSource) -> UpdateCheckResult:
        LOGGER.info(
            "Update check started source=%s manifest=%s current=%s",
            source.value,
            self.manifest_url,
            self.current_version,
        )
        try:
            payload = self.fetcher(
                self.manifest_url,
                self.timeout,
                f"IMAP-Exporter/{self.current_version} UpdateChecker",
                MAX_MANIFEST_BYTES,
            )
            manifest = parse_manifest_bytes(payload)
            result = evaluate_manifest(manifest, self.current_version, source)
            LOGGER.info(
                "Update check completed source=%s current=%s remote=%s status=%s",
                source.value,
                self.current_version,
                manifest.version,
                result.status.value,
            )
            return result
        except UpdateCheckError as exc:
            LOGGER.warning(
                "Update check failed source=%s detail=%s",
                source.value,
                exc,
            )
            return UpdateCheckResult(
                status=UpdateStatus.CHECK_FAILED,
                source=source,
                current_version=self.current_version,
                error=str(exc),
            )
        except Exception as exc:  # Defensive boundary for startup safety.
            LOGGER.exception("Unexpected update check error source=%s", source.value)
            return UpdateCheckResult(
                status=UpdateStatus.CHECK_FAILED,
                source=source,
                current_version=self.current_version,
                error=f"Unexpected update check error: {type(exc).__name__}.",
            )

    def check_async(
        self,
        source: CheckSource,
        callback: ResultCallback,
    ) -> bool:
        with self._lock:
            if self._shutting_down:
                return False
            self._callbacks.append((source, callback))
            if self._checking:
                LOGGER.info("Update check coalesced source=%s", source.value)
                return False
            self._checking = True

        def work() -> None:
            base_result = self.check(source)
            with self._lock:
                callbacks = list(self._callbacks)
                self._callbacks.clear()
                self._checking = False
                shutting_down = self._shutting_down
            if shutting_down:
                return
            for requested_source, requested_callback in callbacks:
                try:
                    requested_callback(
                        replace(base_result, source=requested_source)
                    )
                except Exception:
                    LOGGER.exception(
                        "Update result callback failed source=%s",
                        requested_source.value,
                    )

        threading.Thread(
            target=work,
            name="imap-exporter-update-check",
            daemon=True,
        ).start()
        return True
