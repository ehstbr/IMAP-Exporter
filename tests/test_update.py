from __future__ import annotations

import json
import threading
import time
import unittest
from pathlib import Path

from mail_exporter import __version__
from mail_exporter.update import (
    CheckSource,
    InvalidManifestError,
    LATEST_RELEASE_URL,
    MAX_MANIFEST_BYTES,
    SemanticVersion,
    UPDATE_MANIFEST_URL,
    UpdateNetworkError,
    UpdateService,
    UpdateStatus,
    evaluate_manifest,
    fetch_manifest_bytes,
    parse_manifest_bytes,
)


ROOT = Path(__file__).resolve().parents[1]


def manifest_bytes(
    *,
    version: str = "0.5.1",
    mandatory: bool = False,
    **overrides: object,
) -> bytes:
    payload: dict[str, object] = {
        "schema_version": 1,
        "version": version,
        "mandatory": mandatory,
        "released_at": "2026-08-09T16:20:23Z",
        "summary": "A safe update with <plain> & text.",
        "changelog": ["First change.", "Second change."],
    }
    payload.update(overrides)
    return json.dumps(payload).encode("utf-8")


class SemanticVersionTests(unittest.TestCase):
    def test_numeric_components_are_not_compared_as_text(self) -> None:
        self.assertLess(SemanticVersion.parse("1.0.9"), SemanticVersion.parse("1.0.10"))
        self.assertLess(SemanticVersion.parse("1.9.0"), SemanticVersion.parse("1.10.0"))
        self.assertLess(SemanticVersion.parse("1.9.9"), SemanticVersion.parse("2.0.0"))

    def test_prerelease_precedence_follows_semver(self) -> None:
        ordered = [
            "1.0.0-alpha",
            "1.0.0-alpha.1",
            "1.0.0-alpha.beta",
            "1.0.0-beta",
            "1.0.0-beta.2",
            "1.0.0-beta.11",
            "1.0.0-rc.1",
            "1.0.0",
        ]
        parsed = [SemanticVersion.parse(item) for item in ordered]
        self.assertEqual(parsed, sorted(parsed))

    def test_build_metadata_does_not_change_precedence(self) -> None:
        self.assertEqual(
            SemanticVersion.parse("1.2.3+build.1"),
            SemanticVersion.parse("1.2.3+build.99"),
        )

    def test_invalid_versions_are_rejected(self) -> None:
        for value in ("v1.2.3", "1.2", "1.02.3", "1.2.3-01", "1.2.3 "):
            with self.subTest(value=value):
                with self.assertRaises(InvalidManifestError):
                    SemanticVersion.parse(value)


class ManifestValidationTests(unittest.TestCase):
    def test_valid_manifest_preserves_plain_text(self) -> None:
        manifest = parse_manifest_bytes(manifest_bytes())
        self.assertEqual(manifest.summary, "A safe update with <plain> & text.")
        self.assertEqual(manifest.changelog, ("First change.", "Second change."))
        self.assertFalse(manifest.mandatory)

    def test_policy_distinguishes_every_version_state(self) -> None:
        optional = parse_manifest_bytes(manifest_bytes(version="1.1.0"))
        mandatory = parse_manifest_bytes(
            manifest_bytes(version="1.1.0", mandatory=True)
        )
        current = parse_manifest_bytes(manifest_bytes(version="1.0.0", mandatory=True))
        older = parse_manifest_bytes(manifest_bytes(version="0.9.0"))
        self.assertEqual(
            evaluate_manifest(optional, "1.0.0", CheckSource.STARTUP).status,
            UpdateStatus.OPTIONAL_UPDATE_AVAILABLE,
        )
        self.assertEqual(
            evaluate_manifest(mandatory, "1.0.0", CheckSource.STARTUP).status,
            UpdateStatus.MANDATORY_UPDATE_REQUIRED,
        )
        self.assertEqual(
            evaluate_manifest(current, "1.0.0", CheckSource.STARTUP).status,
            UpdateStatus.UP_TO_DATE,
        )
        self.assertEqual(
            evaluate_manifest(older, "1.0.0", CheckSource.STARTUP).status,
            UpdateStatus.LOCAL_VERSION_NEWER,
        )

    def test_malformed_and_non_object_json_are_rejected(self) -> None:
        for raw in (b"{", b"[]", b"", b"\xff"):
            with self.subTest(raw=raw):
                with self.assertRaises(InvalidManifestError):
                    parse_manifest_bytes(raw)

    def test_every_required_field_is_enforced(self) -> None:
        original = json.loads(manifest_bytes())
        for field in (
            "schema_version",
            "version",
            "mandatory",
            "released_at",
            "summary",
            "changelog",
        ):
            payload = dict(original)
            del payload[field]
            with self.subTest(field=field):
                with self.assertRaises(InvalidManifestError):
                    parse_manifest_bytes(json.dumps(payload).encode())

    def test_policy_fields_require_exact_json_types(self) -> None:
        invalid = (
            {"schema_version": True},
            {"mandatory": "false"},
            {"mandatory": 1},
            {"changelog": "one item"},
            {"summary": ["not text"]},
        )
        for change in invalid:
            with self.subTest(change=change):
                with self.assertRaises(InvalidManifestError):
                    parse_manifest_bytes(manifest_bytes(**change))

    def test_unknown_schema_date_version_and_changelog_item_are_rejected(self) -> None:
        invalid = (
            {"schema_version": 2},
            {"released_at": "2026-08-09"},
            {"version": "1.0"},
            {"changelog": [{"text": "not plain text"}]},
        )
        for change in invalid:
            with self.subTest(change=change):
                with self.assertRaises(InvalidManifestError):
                    parse_manifest_bytes(manifest_bytes(**change))

    def test_manifest_size_limit_is_enforced(self) -> None:
        with self.assertRaises(InvalidManifestError):
            parse_manifest_bytes(b"x" * (MAX_MANIFEST_BYTES + 1))


class UpdateServiceTests(unittest.TestCase):
    def test_service_returns_optional_and_mandatory_results(self) -> None:
        optional = UpdateService(
            current_version="0.5.0",
            fetcher=lambda *_args: manifest_bytes(version="0.5.1"),
        ).check(CheckSource.STARTUP)
        required = UpdateService(
            current_version="0.5.0",
            fetcher=lambda *_args: manifest_bytes(
                version="0.6.0", mandatory=True
            ),
        ).check(CheckSource.MANUAL)
        self.assertEqual(optional.status, UpdateStatus.OPTIONAL_UPDATE_AVAILABLE)
        self.assertEqual(required.status, UpdateStatus.MANDATORY_UPDATE_REQUIRED)
        self.assertEqual(required.source, CheckSource.MANUAL)

    def test_network_http_timeout_and_unexpected_failures_are_fail_open(self) -> None:
        failures = (
            UpdateNetworkError("HTTP error 404."),
            UpdateNetworkError("HTTP error 500."),
            UpdateNetworkError("Timed out."),
            OSError("Offline"),
        )
        for failure in failures:
            def fail(*_args: object, current: Exception = failure) -> bytes:
                raise current

            with self.subTest(failure=failure):
                result = UpdateService(fetcher=fail).check(CheckSource.STARTUP)
                self.assertEqual(result.status, UpdateStatus.CHECK_FAILED)

    def test_request_contains_only_generic_update_information(self) -> None:
        captured: list[object] = []

        def fetcher(*args: object) -> bytes:
            captured.extend(args)
            return manifest_bytes(version=__version__)

        result = UpdateService(fetcher=fetcher).check(CheckSource.STARTUP)
        self.assertEqual(result.status, UpdateStatus.UP_TO_DATE)
        self.assertEqual(captured[0], UPDATE_MANIFEST_URL)
        self.assertEqual(captured[2], f"IMAP-Exporter/{__version__} UpdateChecker")
        self.assertNotIn("@", str(captured[2]))

    def test_simultaneous_checks_are_coalesced(self) -> None:
        release_fetch = threading.Event()
        calls = 0
        results: list[object] = []

        def fetcher(*_args: object) -> bytes:
            nonlocal calls
            calls += 1
            release_fetch.wait(2)
            return manifest_bytes(version="0.5.1")

        service = UpdateService(current_version="0.5.0", fetcher=fetcher)
        self.assertTrue(service.check_async(CheckSource.STARTUP, results.append))
        self.assertFalse(service.check_async(CheckSource.MANUAL, results.append))
        release_fetch.set()
        deadline = time.monotonic() + 2
        while len(results) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(calls, 1)
        self.assertEqual(len(results), 2)
        self.assertEqual(
            {item.source for item in results},
            {CheckSource.STARTUP, CheckSource.MANUAL},
        )

    def test_shutdown_suppresses_late_callbacks(self) -> None:
        release_fetch = threading.Event()
        callbacks: list[object] = []

        def fetcher(*_args: object) -> bytes:
            release_fetch.wait(2)
            return manifest_bytes()

        service = UpdateService(fetcher=fetcher)
        service.check_async(CheckSource.STARTUP, callbacks.append)
        service.shutdown()
        release_fetch.set()
        time.sleep(0.05)
        self.assertEqual(callbacks, [])

    def test_http_manifest_urls_are_rejected_before_network_access(self) -> None:
        with self.assertRaises(UpdateNetworkError):
            fetch_manifest_bytes("http://example.com/version.json", 1, "test", 100)


class UpdateIntegrationSourceTests(unittest.TestCase):
    def test_version_has_one_canonical_runtime_source(self) -> None:
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        build_source = (ROOT / "packaging/build-deb.sh").read_text(encoding="utf-8")
        self.assertIn("APP_VERSION = __version__", app_source)
        self.assertIn('mail_exporter/__init__.py', build_source)
        self.assertNotIn(f'APP_VERSION = "{__version__}"', app_source)

    def test_startup_and_about_share_one_service(self) -> None:
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertEqual(source.count("self.update_service = UpdateService("), 1)
        self.assertIn("CheckSource.STARTUP", source)
        self.assertIn("CheckSource.MANUAL", source)
        self.assertIn("request_manual_update_check", source)
        self.assertIn("GLib.idle_add", source)

    def test_automatic_startup_check_is_silent_and_does_not_gate_the_ui(self) -> None:
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        activation_start = source.index("    def do_activate(self) -> None:")
        activation_end = source.index(
            "    def _start_startup_update_check(self) -> bool:",
            activation_start,
        )
        activation = source[activation_start:activation_end]
        self.assertIn("window.present()", activation)
        self.assertIn("GLib.idle_add(self._start_startup_update_check)", activation)
        self.assertNotIn("set_sensitive(False)", activation)
        self.assertNotIn("set_runtime_allowed", source)
        self.assertNotIn("runtime_revealer", source)
        self.assertNotIn("runtime_spinner", source)
        self.assertNotIn('_("Verificando atualizações…")', source)

    def test_main_header_uses_one_vertically_centered_title(self) -> None:
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        build_start = source.index("    def _build_window(self) -> None:")
        build_end = source.index(
            "    def _build_main_menu(self, header: Gtk.HeaderBar) -> None:",
            build_start,
        )
        header_build = source[build_start:build_end]
        self.assertIn("title.set_valign(Gtk.Align.CENTER)", header_build)
        self.assertIn("header.set_title_widget(title)", header_build)
        self.assertNotIn("title_box", header_build)
        self.assertNotIn("subtitle", header_build)
        self.assertNotIn(
            "Metadados por padrão; conteúdo somente sob demanda",
            header_build,
        )

    def test_update_window_policy_does_not_use_always_on_top_or_auto_install(self) -> None:
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        update_source = (ROOT / "mail_exporter/update.py").read_text(encoding="utf-8")
        combined = source + update_source
        self.assertNotIn("keep_above", combined)
        self.assertNotIn("kill -9", combined)
        self.assertNotIn("dpkg -i", combined)
        self.assertIn("self.set_modal(mandatory)", source)
        self.assertIn("self.set_destroy_with_parent(False)", source)
        self.assertIn("self.set_transient_for(transient_for)", source)

    def test_update_window_uses_buttons_plain_text_and_a_separate_changelog_page(self) -> None:
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        start = source.index("class UpdateWindow(Gtk.Window):")
        end = source.index("\n\nclass MainWindow", start)
        window = source[start:end]

        self.assertNotIn("Gtk.LinkButton", window)
        self.assertNotIn("Gtk.Revealer", window)
        self.assertNotIn("set_selectable(True)", window)
        self.assertGreaterEqual(window.count("set_selectable(False)"), 2)
        self.assertIn('_("Baixar nova versão"),', window)
        self.assertIn('_("Agora não"),', window)
        self.assertIn('_("Fechar aplicativo"),', window)
        self.assertIn('footer.append(self.details_button)', window)
        self.assertIn('footer.append(release_button)', window)
        self.assertIn('footer.append(self.secondary_button)', window)
        self.assertLess(
            window.index('footer.append(self.details_button)'),
            window.index('footer.append(release_button)'),
        )
        self.assertLess(
            window.index('footer.append(release_button)'),
            window.index('footer.append(self.secondary_button)'),
        )
        self.assertIn('self.page_stack.add_named(overview_scroller, "overview")', window)
        self.assertIn('self.page_stack.add_named(changes_page, "changes")', window)
        self.assertIn('"software-update-available-symbolic"', window)
        self.assertIn('"dialog-warning-symbolic"', window)
        self.assertIn('"view-list-symbolic"', window)
        self.assertIn('"go-previous-symbolic"', window)
        self.assertIn('"application-exit-symbolic"', window)
        self.assertIn('"window-close-symbolic"', window)
        self.assertNotIn("update-icon-badge", window)
        self.assertNotIn('"imap-update-available-symbolic"', window)
        self.assertNotIn('"imap-update-critical-symbolic"', window)
        self.assertIn("self._toggle_changelog_page", window)
        self.assertIn("self._show_update_page(\"overview\")", window)
        self.assertIn("Gio.AppInfo.launch_default_for_uri", window)

        for icon_name in (
            "imap-update-available-symbolic.svg",
            "imap-update-critical-symbolic.svg",
        ):
            self.assertFalse(
                (
                    ROOT
                    / "assets/icons/hicolor/scalable/actions"
                    / icon_name
                ).is_file()
            )

    def test_update_footer_has_three_equal_width_actions(self) -> None:
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        start = source.index("class UpdateWindow(Gtk.Window):")
        end = source.index("\n\nclass MainWindow", start)
        window = source[start:end]
        css = (ROOT / "style.css").read_text(encoding="utf-8")

        self.assertIn(
            "footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)",
            window,
        )
        self.assertIn("footer.set_halign(Gtk.Align.FILL)", window)
        self.assertIn("footer.set_hexpand(True)", window)
        self.assertGreaterEqual(window.count(".set_hexpand(True)"), 4)
        self.assertIn("min-width: 140px", css)
        self.assertNotIn(".update-icon-badge", css)
        self.assertNotIn(".update-details-action", css)

    def test_update_footer_centers_icon_labels_and_optional_close_is_allowed(self) -> None:
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        helper_start = source.index("def icon_label(")
        helper_end = source.index("\n\ndef icon_only_button", helper_start)
        helpers = source[helper_start:helper_end]
        start = source.index("class UpdateWindow(Gtk.Window):")
        end = source.index("\n\nclass MainWindow", start)
        window = source[start:end]
        close_start = window.index("    def _on_close_request(")
        close_end = window.index("\n    def set_waiting_for_safe_exit", close_start)
        close_handler = window[close_start:close_end]

        self.assertIn("content.set_halign(Gtk.Align.CENTER)", helpers)
        self.assertIn("icon_label(icon_name, label, centered=True)", helpers)
        self.assertGreaterEqual(window.count("centered=True"), 2)
        self.assertIn('"software-update-available-symbolic"', window)
        self.assertIn('"window-close-symbolic"', window)
        self.assertIn('"application-exit-symbolic"', window)
        self.assertIn("self._on_close(self)", close_handler)
        self.assertIn("return False", close_handler)
        self.assertNotIn("self.destroy()", close_handler)
        self.assertLess(
            close_handler.index("if self._mandatory:"),
            close_handler.index("return False"),
        )

    def test_mandatory_policy_resumes_existing_work_and_blocks_new_work(self) -> None:
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        start = source.index("    def prepare_for_mandatory_update(")
        end = source.index("    def _about_text_page(", start)
        policy = source[start:end]
        self.assertIn("self.stack.set_sensitive(False)", source)
        self.assertIn("self.pause_event.clear()", policy)
        self.assertIn("self.cleanup_pause_event.clear()", policy)
        self.assertIn("self.attachment_analysis_pause_event.clear()", policy)
        self.assertNotIn("self.pause_event.set()", policy)
        self.assertNotIn("self.cleanup_pause_event.set()", policy)
        self.assertNotIn("self.attachment_analysis_pause_event.set()", policy)

    def test_repository_urls_and_manifest_match_release(self) -> None:
        manifest = json.loads((ROOT / "version.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], __version__)
        self.assertFalse(manifest["mandatory"])
        self.assertEqual(
            UPDATE_MANIFEST_URL,
            "https://raw.githubusercontent.com/ehstbr/IMAP-Exporter/main/version.json",
        )
        self.assertEqual(
            LATEST_RELEASE_URL,
            "https://github.com/ehstbr/IMAP-Exporter/releases/latest",
        )


if __name__ == "__main__":
    unittest.main()
