#!/usr/bin/env python3
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import weakref
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from mail_exporter import __version__
from mail_exporter.i18n import get_language, set_language, tr as _
from mail_exporter.update import (
    CheckSource,
    LATEST_RELEASE_URL,
    PROJECT_URL,
    UpdateCheckResult,
    UpdateService,
    UpdateStatus,
)

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango
except (ImportError, ValueError) as exc:
    print(
        _(
            "Não foi possível carregar a interface GTK 4.\n"
            "Execute este aplicativo em um sistema Linux com GTK 4 e "
            "PyGObject.\nDetalhe: {detail}"
        ).format(detail=exc),
        file=sys.stderr,
    )
    raise SystemExit(1)

from mail_exporter.db import Database
from mail_exporter.exporters import export_csv, export_ods
from mail_exporter.imap_service import (
    AttachmentDownloadCancelled,
    MailExtractor,
)
from mail_exporter.paths import database_path
from mail_exporter.providers import load_provider_presets
from mail_exporter.secrets import InvalidAccountPassword, SecretCipher, SecretError


APP_ID = "io.github.ehstbr.imapexporter"
APP_NAME = "IMAP Exporter"
APP_VERSION = __version__
MIT_LICENSE_FALLBACK = """MIT License

Copyright (c) 2026 Eduardo Henrique Silva Teixeira

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE."""
DEVELOPER_NAME = "Eduardo Henrique Silva Teixeira"
DEVELOPER_SITE = "https://eduhcommerce.com.br"
DEVELOPER_EMAIL = "contato@eduhcommerce.com.br"
ICON_THEME_PATH = Path(__file__).resolve().parent / "assets" / "icons"
RECOVERY_AUTH_HELPER = Path(
    "/usr/lib/imap-exporter/imap-exporter-authorize-delete"
)

SHARED_EMAIL_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "hotmail.com",
    "outlook.com",
    "live.com",
    "yahoo.com",
    "yahoo.com.br",
    "icloud.com",
    "me.com",
    "proton.me",
    "protonmail.com",
    "uol.com.br",
    "bol.com.br",
    "terra.com.br",
}


def set_margins(widget: Gtk.Widget, amount: int = 18) -> None:
    widget.set_margin_top(amount)
    widget.set_margin_bottom(amount)
    widget.set_margin_start(amount)
    widget.set_margin_end(amount)


def clear_box(box: Gtk.Box | Gtk.ListBox) -> None:
    child = box.get_first_child()
    while child is not None:
        following = child.get_next_sibling()
        box.remove(child)
        child = following


def human_size(value: int | None) -> str:
    amount = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return f"{amount:.1f} TB"


def icon_label(icon_name: str, label: str) -> Gtk.Box:
    content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    image = Gtk.Image.new_from_icon_name(icon_name)
    image.set_pixel_size(16)
    content.append(image)
    content.append(Gtk.Label(label=label))
    return content


def icon_button(label: str, icon_name: str) -> Gtk.Button:
    button = Gtk.Button()
    button.set_child(icon_label(icon_name, label))
    return button


def icon_only_button(icon_name: str) -> Gtk.Button:
    button = Gtk.Button()
    image = Gtk.Image.new_from_icon_name(icon_name)
    image.set_pixel_size(16)
    button.set_child(image)
    return button


def rounded_scroll_frame(scroller: Gtk.ScrolledWindow) -> Gtk.Frame:
    frame = Gtk.Frame()
    frame.set_hexpand(True)
    frame.set_vexpand(scroller.get_vexpand())
    frame.set_overflow(Gtk.Overflow.HIDDEN)
    frame.add_css_class("rounded-scroll-frame")
    frame.set_child(scroller)
    return frame


class AppDialog(Gtk.Window):
    __gsignals__ = {
        "response": (GObject.SignalFlags.RUN_LAST, None, (int,)),
    }

    def __init__(
        self,
        *,
        transient_for: Gtk.Window,
        title: str,
        default_width: int = 520,
        default_height: int = -1,
    ) -> None:
        super().__init__(title=title)
        self.set_transient_for(transient_for)
        application = transient_for.get_application()
        if application is not None:
            self.set_application(application)
        self.set_modal(True)
        self.set_destroy_with_parent(True)
        self.set_default_size(default_width, default_height)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(root)

        self.header = Gtk.HeaderBar()
        self.set_titlebar(self.header)

        self.content_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.content_area.set_hexpand(True)
        self.content_area.set_vexpand(True)
        root.append(self.content_area)

        self.footer_separator = Gtk.Separator(
            orientation=Gtk.Orientation.HORIZONTAL
        )
        root.append(self.footer_separator)
        self.footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.footer.set_halign(Gtk.Align.FILL)
        self.footer.set_hexpand(True)
        set_margins(self.footer, 16)
        root.append(self.footer)

        self.footer_start = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=10,
        )
        self.footer_start.set_hexpand(True)
        self.footer_end = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=10,
        )
        self.footer.append(self.footer_start)
        self.footer.append(self.footer_end)

        self.response_buttons: dict[int, Gtk.Button] = {}
        self.connect("close-request", self._on_close_request)

    def add_button(self, label: str, response: Gtk.ResponseType) -> Gtk.Button:
        response_id = int(response)
        button = Gtk.Button(label=label)
        button.connect(
            "clicked",
            lambda _button, value=response_id: self.emit("response", value),
        )
        self.footer_end.append(button)
        self.response_buttons[response_id] = button
        return button

    def add_start_button(self, label: str) -> Gtk.Button:
        button = Gtk.Button(label=label)
        self.footer_start.append(button)
        return button

    def get_content_area(self) -> Gtk.Box:
        return self.content_area

    def set_footer_visible(self, visible: bool) -> None:
        self.footer_separator.set_visible(visible)
        self.footer.set_visible(visible)

    def set_default_response(self, response: Gtk.ResponseType) -> None:
        button = self.response_buttons.get(int(response))
        if button is not None:
            self.set_default_widget(button)

    def present_with_focus(self, widget: Gtk.Widget) -> None:
        self.present()

        def apply_initial_focus() -> bool:
            if not self.get_visible():
                return False
            widget.grab_focus()
            return False

        GLib.idle_add(apply_initial_focus)

    def _on_close_request(self, _window: Gtk.Window) -> bool:
        self.emit("response", int(Gtk.ResponseType.CANCEL))
        return True


class UpdateWindow(Gtk.Window):
    """Application-owned update notice with distinct optional/required policy."""

    def __init__(
        self,
        *,
        application: Gtk.Application,
        transient_for: Gtk.Window | None,
        result: UpdateCheckResult,
        on_close: Callable[[Gtk.Window], None],
        on_quit: Callable[[], None],
    ) -> None:
        manifest = result.manifest
        if manifest is None:
            raise ValueError("An update window requires a validated manifest.")
        mandatory = result.status == UpdateStatus.MANDATORY_UPDATE_REQUIRED
        title = _("Atualização necessária") if mandatory else _("Atualização disponível")
        super().__init__(title=title, application=application)
        if transient_for is not None and transient_for.get_visible():
            self.set_transient_for(transient_for)
        self.set_destroy_with_parent(False)
        self.set_modal(mandatory)
        self.set_default_size(620, -1)
        self.set_size_request(480, -1)
        self._mandatory = mandatory
        self.remote_version = result.remote_version
        self._on_close = on_close
        self._on_quit = on_quit
        self._closing = False

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(root)
        header = Gtk.HeaderBar()
        self.set_titlebar(header)

        content_scroller = Gtk.ScrolledWindow()
        content_scroller.set_policy(
            Gtk.PolicyType.NEVER,
            Gtk.PolicyType.AUTOMATIC,
        )
        content_scroller.set_propagate_natural_height(True)
        content_scroller.set_max_content_height(620)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        set_margins(content, 22)
        content_scroller.set_child(content)
        root.append(content_scroller)

        icon = Gtk.Image.new_from_icon_name(APP_ID)
        icon.set_pixel_size(72)
        icon.set_halign(Gtk.Align.CENTER)
        content.append(icon)

        heading = Gtk.Label(label=title, wrap=True)
        heading.set_justify(Gtk.Justification.CENTER)
        heading.add_css_class("title-1")
        content.append(heading)

        policy_text = (
            _(
                "Esta versão precisa ser atualizada antes que o aplicativo "
                "possa continuar sendo usado."
            )
            if mandatory
            else _(
                "Uma nova versão está disponível. Você pode obtê-la agora ou "
                "continuar usando esta versão nesta sessão."
            )
        )
        policy = Gtk.Label(
            label=policy_text,
            wrap=True,
            justify=Gtk.Justification.CENTER,
        )
        policy.set_max_width_chars(68)
        policy.add_css_class("dim-label")
        content.append(policy)

        versions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=18,
        )
        versions.set_halign(Gtk.Align.CENTER)
        installed = Gtk.Label(
            label=f"{_('Versão instalada')}: {result.current_version}"
        )
        available = Gtk.Label(
            label=f"{_('Nova versão')}: {manifest.version}"
        )
        installed.add_css_class("heading")
        available.add_css_class("heading")
        versions.append(installed)
        versions.append(available)
        content.append(versions)

        local_date = manifest.released_at.astimezone()
        date_text = (
            local_date.strftime("%d/%m/%Y")
            if get_language() == "pt_BR"
            else local_date.strftime("%Y-%m-%d")
        )
        released = Gtk.Label(label=f"{_('Publicada em')}: {date_text}")
        released.set_halign(Gtk.Align.CENTER)
        released.add_css_class("dim-label")
        content.append(released)

        summary_heading = Gtk.Label(label=_("Resumo"), xalign=0)
        summary_heading.add_css_class("title-3")
        content.append(summary_heading)
        summary = Gtk.Label(label=manifest.summary, wrap=True, xalign=0)
        summary.set_selectable(True)
        summary.set_use_markup(False)
        content.append(summary)

        changelog_revealer = Gtk.Revealer()
        changelog_revealer.set_transition_type(
            Gtk.RevealerTransitionType.SLIDE_DOWN
        )
        changelog_revealer.set_transition_duration(180)
        changelog_revealer.set_reveal_child(False)
        changelog_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
        )
        changelog_scroller = Gtk.ScrolledWindow()
        changelog_scroller.set_policy(
            Gtk.PolicyType.NEVER,
            Gtk.PolicyType.AUTOMATIC,
        )
        changelog_scroller.set_min_content_height(120)
        changelog_scroller.set_max_content_height(260)
        changelog_scroller.set_propagate_natural_height(True)
        changelog_list = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
        )
        set_margins(changelog_list, 12)
        for item in manifest.changelog:
            row = Gtk.Label(label=f"• {item}", wrap=True, xalign=0)
            row.set_selectable(True)
            row.set_use_markup(False)
            changelog_list.append(row)
        if not manifest.changelog:
            empty = Gtk.Label(
                label=_("Nenhuma alteração adicional foi informada."),
                wrap=True,
                xalign=0,
            )
            empty.add_css_class("dim-label")
            changelog_list.append(empty)
        changelog_scroller.set_child(changelog_list)
        changelog_box.append(rounded_scroll_frame(changelog_scroller))
        changelog_revealer.set_child(changelog_box)

        details_button = Gtk.Button(label=_("Ver todas as alterações"))
        details_button.set_halign(Gtk.Align.START)
        details_button.add_css_class("flat")
        details_button.set_tooltip_text(_("Exibir o changelog completo"))

        def toggle_details(_button: Gtk.Button) -> None:
            expanded = not changelog_revealer.get_reveal_child()
            changelog_revealer.set_reveal_child(expanded)
            details_button.set_label(
                _("Ocultar alterações")
                if expanded
                else _("Ver todas as alterações")
            )

        details_button.connect("clicked", toggle_details)
        content.append(details_button)
        content.append(changelog_revealer)

        self.exit_status = Gtk.Label(wrap=True, xalign=0)
        self.exit_status.add_css_class("dim-label")
        self.exit_status.set_visible(False)
        content.append(self.exit_status)

        separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        root.append(separator)
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        footer.set_halign(Gtk.Align.END)
        set_margins(footer, 16)
        root.append(footer)

        release_button = Gtk.LinkButton.new_with_label(
            LATEST_RELEASE_URL,
            _("Obter atualização"),
        )
        release_button.set_tooltip_text(LATEST_RELEASE_URL)
        release_button.add_css_class("suggested-action")
        if mandatory:
            self.secondary_button = Gtk.Button(label=_("Sair"))
            self.secondary_button.connect("clicked", lambda _button: on_quit())
        else:
            self.secondary_button = Gtk.Button(
                label=_("Continuar usando esta versão")
            )
            self.secondary_button.connect("clicked", lambda _button: self.close())
        footer.append(self.secondary_button)
        footer.append(release_button)
        self.set_default_widget(release_button)

        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_controller)
        self.connect("close-request", self._on_close_request)
        self.connect("destroy", on_close)

    def _on_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        _state: Gdk.ModifierType,
    ) -> bool:
        if keyval != Gdk.KEY_Escape:
            return False
        if self._mandatory:
            self._on_quit()
        else:
            self.close()
        return True

    def _on_close_request(self, _window: Gtk.Window) -> bool:
        if self._closing:
            return False
        if self._mandatory:
            self._on_quit()
            return True
        self._closing = True
        self.destroy()
        return True

    def set_waiting_for_safe_exit(self) -> None:
        self.exit_status.set_text(
            _(
                "A operação atual será concluída com segurança antes de o "
                "aplicativo encerrar. Nenhuma nova operação será iniciada."
            )
        )
        self.exit_status.set_visible(True)
        self.secondary_button.set_label(_("Concluindo operação…"))
        self.secondary_button.set_sensitive(False)


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, application: Gtk.Application):
        super().__init__(application=application)
        self.set_title(f"{APP_NAME} {APP_VERSION}")
        self.set_default_size(980, 680)
        self.set_size_request(760, 520)
        Gtk.IconTheme.get_for_display(self.get_display()).add_search_path(
            str(ICON_THEME_PATH)
        )

        self.database = Database(database_path())
        self.extractor = MailExtractor(self.database)
        self.secret_cipher = SecretCipher()
        self.provider_presets = load_provider_presets()
        self.unlocked_accounts: dict[int, dict[str, str]] = {}
        self.recovery_removals_pending: set[int] = set()
        self.active_account: dict[str, Any] | None = None
        self.active_imap_password: str | None = None
        self.folder_checks: dict[int, Gtk.CheckButton] = {}
        self.selected_cleanup_senders: set[str] = set()
        self.selected_cleanup_domains: set[str] = set()
        self.selected_large_messages: set[int] = set()
        self.sender_rows: list[dict[str, Any]] = []
        self.domain_rows: list[dict[str, Any]] = []
        self.largest_rows: list[dict[str, Any]] = []
        self.sender_row_checks: dict[Gtk.ListBoxRow, Gtk.CheckButton] = {}
        self.domain_row_checks: dict[Gtk.ListBoxRow, Gtk.CheckButton] = {}
        self.largest_row_checks: dict[Gtk.ListBoxRow, Gtk.CheckButton] = {}
        self.largest_items_by_row: dict[
            Gtk.ListBoxRow, dict[str, Any]
        ] = {}
        self.largest_query_generation = 0
        self.updating_largest_extension_filter = False
        self.cleanup_selection_labels: list[Gtk.Label] = []
        self.selected_csv_buttons: list[Gtk.Button] = []
        self.selected_ods_buttons: list[Gtk.Button] = []
        self.cleanup_buttons: list[Gtk.Button] = []
        self.subject_preview_cache: dict[
            tuple[int, str, str], list[dict[str, Any]]
        ] = {}
        self.current_thread: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()
        self.is_paused = False
        self.current_sync_missing = 0
        self.cleanup_dialog: AppDialog | None = None
        self.cleanup_cancel_event = threading.Event()
        self.cleanup_pause_event = threading.Event()
        self.cleanup_is_paused = False
        self.cleanup_operation_active = False
        self.cleanup_mode = "move"
        self.cleanup_undo_items: list[dict[str, Any]] = []
        self.cleanup_undo_available_count = 0
        self.cleanup_moved_count = 0
        self.cleanup_undo_account: dict[str, Any] | None = None
        self.cleanup_undo_password: str | None = None
        self.cleanup_undo_trash: dict[str, Any] | None = None
        self.cleanup_reconciliation_error: str | None = None
        self.attachment_analysis_dialog: AppDialog | None = None
        self.attachment_analysis_cancel_event = threading.Event()
        self.attachment_analysis_pause_event = threading.Event()
        self.attachment_analysis_running = False
        self.export_operation_active = False

        self._build_window()
        self.refresh_accounts()

    def _build_window(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(root)

        header = Gtk.HeaderBar()
        self.back_button = icon_button(_("Voltar"), "imap-back-symbolic")
        self.back_button.add_css_class("portable-action")
        self.back_button.set_tooltip_text(_("Voltar para as contas"))
        self.back_button.connect("clicked", self._on_back)
        self.back_button.set_visible(False)
        header.pack_start(self.back_button)

        title = Gtk.Label(label=f"{_(APP_NAME)} {APP_VERSION}")
        title.add_css_class("title")
        title.set_valign(Gtk.Align.CENTER)
        header.set_title_widget(title)

        self.add_button = icon_button(_("Adicionar conta"), "imap-add-symbolic")
        self.add_button.set_tooltip_text(_("Cadastrar uma nova conta IMAP"))
        self.add_button.connect("clicked", self._show_add_account_dialog)
        header.pack_start(self.add_button)
        self._build_main_menu(header)
        self.set_titlebar(header)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.stack.set_transition_duration(180)
        self.stack.set_vexpand(True)
        root.append(self.stack)

        self._build_accounts_page()
        self._build_folders_page()
        self._build_progress_page()
        self._build_results_page()

    def _build_main_menu(self, header: Gtk.HeaderBar) -> None:
        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect(
            "activate",
            lambda _action, _parameter: self._show_about_dialog(),
        )
        self.add_action(about_action)

        self.rebuild_index_action = Gio.SimpleAction.new(
            "rebuild-index",
            None,
        )
        self.rebuild_index_action.connect(
            "activate",
            lambda _action, _parameter: self._confirm_rebuild_index(),
        )
        self.rebuild_index_action.set_enabled(False)
        self.add_action(self.rebuild_index_action)

        for action_name, language in (
            ("language-pt", "pt_BR"),
            ("language-en", "en"),
        ):
            action = Gio.SimpleAction.new(action_name, None)
            action.connect(
                "activate",
                lambda _action, _parameter, value=language: (
                    self._change_language(value)
                ),
            )
            self.add_action(action)

        language_menu = Gio.Menu()
        language_menu.append(_("Português"), "win.language-pt")
        language_menu.append(_("English"), "win.language-en")

        menu = Gio.Menu()
        menu.append_section(_("Idioma"), language_menu)
        maintenance_menu = Gio.Menu()
        maintenance_menu.append(
            _("Reconstruir índice local…"),
            "win.rebuild-index",
        )
        menu.append_section(_("Manutenção"), maintenance_menu)
        menu.append(_("Sobre"), "win.about")

        menu_button = Gtk.MenuButton()
        menu_icon = Gtk.Image.new_from_icon_name("imap-menu-symbolic")
        menu_icon.set_pixel_size(16)
        menu_button.set_child(menu_icon)
        menu_button.set_menu_model(menu)
        menu_button.set_tooltip_text(_("Menu principal"))
        header.pack_end(menu_button)

    def _change_language(self, language: str) -> None:
        if language == get_language():
            return
        set_language(language)
        self._show_notice(
            _("Idioma alterado"),
            _(
                "O idioma será aplicado completamente na próxima vez que o "
                "aplicativo for aberto."
            ),
        )

    def has_critical_operation(self) -> bool:
        return bool(
            (self.current_thread is not None and self.current_thread.is_alive())
            or self.cleanup_operation_active
            or self.attachment_analysis_running
            or self.export_operation_active
        )

    def prepare_for_mandatory_update(self) -> bool:
        """Block new work and let any current operation reach a safe boundary."""
        self.add_button.set_sensitive(False)
        self.back_button.set_sensitive(False)
        self.rebuild_index_action.set_enabled(False)
        self.stack.set_sensitive(False)
        if self.is_paused:
            self.is_paused = False
            self.pause_event.clear()
        if self.cleanup_is_paused:
            self.cleanup_is_paused = False
            self.cleanup_pause_event.clear()
        self.attachment_analysis_pause_event.clear()
        return self.has_critical_operation()

    def _about_text_page(self, text: str) -> Gtk.Frame:
        label = Gtk.Label(
            label=text,
            wrap=True,
            xalign=0,
            yalign=0,
        )
        set_margins(label, 18)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(
            Gtk.PolicyType.NEVER,
            Gtk.PolicyType.AUTOMATIC,
        )
        scroller.set_vexpand(True)
        scroller.set_child(label)
        return rounded_scroll_frame(scroller)

    @staticmethod
    def _read_packaged_document(filename: str, fallback: str) -> str:
        candidates = (
            Path(__file__).resolve().with_name(filename),
            Path("/usr/share/doc/imap-exporter") / filename,
        )
        for candidate in candidates:
            try:
                content = candidate.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if content:
                return content
        return fallback.strip()

    def _show_about_dialog(self) -> None:
        dialog = AppDialog(
            transient_for=self,
            title=_("Sobre"),
            default_width=700,
            default_height=610,
        )
        dialog.set_footer_visible(False)
        dialog.connect("response", lambda item, _response: item.destroy())

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        set_margins(content, 16)
        switcher = Gtk.StackSwitcher()
        switcher.set_halign(Gtk.Align.CENTER)
        pages = Gtk.Stack()
        pages.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        pages.set_transition_duration(160)
        pages.set_vexpand(True)
        switcher.set_stack(pages)
        content.append(switcher)
        content.append(pages)

        information = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
        )
        information.set_halign(Gtk.Align.CENTER)
        information.set_valign(Gtk.Align.CENTER)
        information.set_hexpand(True)
        information.set_vexpand(True)
        about_icon = Gtk.Image.new_from_icon_name(APP_ID)
        about_icon.set_pixel_size(96)
        about_icon.set_tooltip_text(_(APP_NAME))
        information.append(about_icon)
        name = Gtk.Label(label=_(APP_NAME))
        name.add_css_class("title-1")
        information.append(name)
        version = Gtk.Label(label=f"{_('Versão')} {APP_VERSION}")
        version.add_css_class("dim-label")
        information.append(version)
        update_controls = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
        )
        update_controls.set_halign(Gtk.Align.CENTER)
        check_update_button = Gtk.Button(label=_("Verificar atualizações"))
        check_update_button.set_tooltip_text(
            _("Consultar a versão mais recente no GitHub")
        )
        update_status = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        update_status.set_max_width_chars(58)
        update_status.add_css_class("dim-label")
        update_status.set_visible(False)
        update_controls.append(check_update_button)
        update_controls.append(update_status)
        information.append(update_controls)

        def check_updates(_button: Gtk.Button) -> None:
            application = self.get_application()
            if isinstance(application, HeaderExporterApplication):
                application.request_manual_update_check(
                    dialog,
                    check_update_button,
                    update_status,
                )

        check_update_button.connect("clicked", check_updates)
        description = Gtk.Label(
            label=_(
                "Aplicativo desktop para coletar, analisar e exportar "
                "metadados por IMAP. A sincronização usa cabeçalhos e a "
                "estrutura MIME; conteúdo e anexos só são consultados sob "
                "demanda."
            ),
            wrap=True,
            justify=Gtk.Justification.CENTER,
        )
        description.set_max_width_chars(64)
        information.append(description)
        developer = Gtk.Label(
            label=f"{_('Desenvolvido por')}: {DEVELOPER_NAME}"
        )
        developer.add_css_class("heading")
        information.append(developer)

        links = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        links.set_halign(Gtk.Align.CENTER)
        site = Gtk.LinkButton.new_with_label(
            DEVELOPER_SITE,
            _("Site do desenvolvedor"),
        )
        site.set_tooltip_text(DEVELOPER_SITE)
        email = Gtk.LinkButton.new_with_label(
            f"mailto:{DEVELOPER_EMAIL}",
            _("Enviar e-mail"),
        )
        email.set_tooltip_text(DEVELOPER_EMAIL)
        source = Gtk.LinkButton.new_with_label(
            PROJECT_URL,
            _("Código-fonte e contribuições"),
        )
        source.set_tooltip_text(PROJECT_URL)
        links.append(site)
        links.append(email)
        links.append(source)
        information.append(links)
        open_source = Gtk.Label(
            label=(
                f"{_('Projeto de código aberto')} · "
                f"{_('Este aplicativo é distribuído sob a Licença MIT.')}"
            ),
            wrap=True,
        )
        open_source.add_css_class("dim-label")
        information.append(open_source)
        pages.add_titled(
            information,
            "information",
            _("Informações"),
        )

        terms = _(
            "Ao usar este aplicativo, você reconhece que operações IMAP "
            "dependem do servidor, da conexão e das permissões da conta. "
            "Recursos de movimentação em lote podem alterar marcadores, "
            "pastas ou o estado das mensagens.\n\n"
            "Você é responsável por revisar as seleções, manter cópias de "
            "segurança quando necessárias, proteger suas credenciais, "
            "respeitar as políticas e limites do provedor e confirmar o "
            "resultado diretamente no webmail.\n\n"
            "O software é fornecido “como está”, sem garantia de "
            "disponibilidade, compatibilidade, precisão, recuperação ou "
            "adequação a uma finalidade específica. Na máxima extensão "
            "permitida pela legislação aplicável, o autor e os colaboradores "
            "não serão responsáveis por bloqueio ou suspensão de contas, "
            "perda de mensagens ou dados, movimentação ou exclusão indevida, "
            "cópias duplicadas, falhas de sincronização ou exportação, "
            "interrupção de atividade, perda de receita, lucros cessantes ou "
            "danos diretos, indiretos, incidentais ou consequenciais.\n\n"
            "O aplicativo não esvazia intencionalmente a Lixeira, mas "
            "servidores IMAP podem implementar comandos de formas diferentes. "
            "Anexos baixados podem conter conteúdo malicioso; revise a origem "
            "e não abra arquivos desconhecidos. Teste primeiro com pequenas "
            "seleções. Se você não concordar com estes termos, não utilize as "
            "funções que alteram mensagens ou baixam arquivos.\n\n"
            "Versões modificadas ou distribuídas por terceiros são de "
            "responsabilidade de seus respectivos distribuidores. Estes "
            "termos não afastam direitos ou responsabilidades que não possam "
            "ser legalmente excluídos."
        )
        pages.add_titled(
            self._about_text_page(terms),
            "terms",
            _("Termos de uso"),
        )

        privacy = _(
            "O IMAP Exporter não possui telemetria e não envia os metadados "
            "coletados ao desenvolvedor. A comunicação de rede é feita com o "
            "servidor IMAP configurado e com os endereços que você abrir "
            "voluntariamente.\n\n"
            "A senha local não é armazenada. A senha IMAP é gravada somente "
            "de forma criptografada e é aberta em memória enquanto a conta "
            "estiver desbloqueada. Metadados, configurações e histórico ficam "
            "no computador do usuário. Arquivos CSV e ODS são salvos apenas "
            "no local escolhido. Ao usar o leitor leve, o conteúdo da "
            "mensagem escolhida é solicitado ao servidor IMAP e mantido "
            "somente na memória da janela; ele não é salvo no banco de "
            "dados. A análise de anexos armazena apenas nomes, tipos, "
            "tamanhos e seções IMAP. Os bytes de um anexo só são solicitados "
            "quando você clica em Baixar e são gravados exclusivamente no "
            "local escolhido.\n\n"
            "Quem modificar o código pode alterar esse comportamento; "
            "verifique a procedência de versões obtidas fora do repositório "
            "oficial."
        )
        privacy += "\n\n" + _(
            "O verificador consulta automaticamente, uma vez por abertura, "
            "o arquivo version.json do repositório oficial no GitHub. A "
            "requisição não envia telemetria, identificadores, dados da conta "
            "ou metadados das mensagens."
        )
        pages.add_titled(
            self._about_text_page(privacy),
            "privacy",
            _("Privacidade"),
        )

        license_text = self._read_packaged_document(
            "LICENSE",
            MIT_LICENSE_FALLBACK,
        )
        license_intro = _(
            "A Licença MIT permite usar, copiar, modificar e distribuir este "
            "software, inclusive comercialmente, desde que o aviso de direitos "
            "autorais e a licença sejam mantidos. O software é fornecido sem "
            "garantias e com limitação de responsabilidade."
        )
        license_page = self._about_text_page(
            f"{license_intro}\n\n{_('Texto oficial da Licença MIT')}\n\n"
            f"{license_text}"
        )
        pages.add_titled(
            license_page,
            "license",
            _("Licença"),
        )

        notices_filename = (
            "THIRD_PARTY_NOTICES.en.md"
            if get_language() == "en"
            else "THIRD_PARTY_NOTICES.pt_BR.md"
        )
        notices_fallback = _(
            "O código do IMAP Exporter e os ícones próprios incluídos no "
            "pacote são distribuídos sob a Licença MIT.\n\n"
            "Python, GTK, PyGObject, OpenSSL e o tema de ícones Hicolor são "
            "dependências fornecidas separadamente pelo sistema operacional. "
            "Elas não são incorporadas ao código do aplicativo e conservam "
            "suas próprias licenças e avisos de direitos autorais.\n\n"
            "Em sistemas Debian e Ubuntu, os textos instalados podem ser "
            "consultados em /usr/share/doc/<nome-do-pacote>/copyright. O "
            "arquivo THIRD_PARTY_NOTICES que acompanha o IMAP Exporter contém "
            "os nomes dos componentes, suas funções e referências oficiais."
        )
        notices = self._read_packaged_document(
            notices_filename,
            notices_fallback,
        )
        pages.add_titled(
            self._about_text_page(notices),
            "components",
            _("Componentes"),
        )

        dialog.get_content_area().append(content)
        dialog.present()

    def _build_accounts_page(self) -> None:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        set_margins(page, 24)

        heading = Gtk.Label(label=_("Contas cadastradas"))
        heading.set_xalign(0)
        heading.add_css_class("title-1")
        page.append(heading)
        intro = Gtk.Label(
            label=_(
                "Cada conta mantém seu histórico separadamente. A senha IMAP fica "
                "criptografada e só é aberta com a senha local da conta."
            ),
            wrap=True,
            xalign=0,
        )
        intro.add_css_class("dim-label")
        page.append(intro)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        self.accounts_list = Gtk.ListBox()
        self.accounts_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.accounts_list.add_css_class("boxed-list")
        self.accounts_list.add_css_class("scroll-viewport-list")
        scroller.set_child(self.accounts_list)
        page.append(rounded_scroll_frame(scroller))
        self.stack.add_named(page, "accounts")

    def _build_folders_page(self) -> None:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        set_margins(page, 24)

        heading_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.folders_heading = Gtk.Label(label=_("Escolha as pastas"))
        self.folders_heading.set_xalign(0)
        self.folders_heading.set_hexpand(True)
        self.folders_heading.add_css_class("title-1")
        heading_row.append(self.folders_heading)
        self.reload_folders_button = icon_button(
            _("Recarregar"), "imap-refresh-symbolic"
        )
        self.reload_folders_button.add_css_class("portable-action")
        self.reload_folders_button.set_tooltip_text(
            _("Recarregar pastas e quantidades do servidor")
        )
        self.reload_folders_button.connect("clicked", self._reload_folders)
        heading_row.append(self.reload_folders_button)
        page.append(heading_row)

        self.folder_info = Gtk.Label(
            label=_(
                "No Gmail, “Todos os e-mails” reúne recebidos, enviados e "
                "arquivados. Em outros servidores, escolha as pastas equivalentes."
            ),
            wrap=True,
            xalign=0,
        )
        self.folder_info.add_css_class("dim-label")
        page.append(self.folder_info)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        self.folders_list = Gtk.ListBox()
        self.folders_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.folders_list.add_css_class("boxed-list")
        self.folders_list.add_css_class("scroll-viewport-list")
        scroller.set_child(self.folders_list)

        self.folder_content_stack = Gtk.Stack()
        self.folder_content_stack.set_transition_type(
            Gtk.StackTransitionType.CROSSFADE
        )
        self.folder_content_stack.set_transition_duration(180)
        self.folder_content_stack.set_vexpand(True)
        self.folder_content_stack.add_named(scroller, "folders")

        loading_page = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
        )
        loading_page.set_halign(Gtk.Align.CENTER)
        loading_page.set_valign(Gtk.Align.CENTER)
        loading_page.set_hexpand(True)
        loading_page.set_vexpand(True)
        loading_card = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
        )
        loading_card.set_halign(Gtk.Align.CENTER)
        loading_card.add_css_class("folder-loading-card")
        self.folder_spinner = Gtk.Spinner()
        self.folder_spinner.set_size_request(28, 28)
        self.folder_spinner.set_halign(Gtk.Align.CENTER)
        loading_card.append(self.folder_spinner)
        loading_heading = Gtk.Label(
            label=_("Atualizando pastas"),
            xalign=0.5,
        )
        loading_heading.add_css_class("title-2")
        loading_card.append(loading_heading)
        self.folder_loading_status = Gtk.Label(
            label=_("Consultando pastas…"),
            wrap=True,
            justify=Gtk.Justification.CENTER,
        )
        self.folder_loading_status.set_max_width_chars(54)
        self.folder_loading_status.add_css_class("dim-label")
        loading_card.append(self.folder_loading_status)
        loading_page.append(loading_card)
        self.folder_content_stack.add_named(loading_page, "loading")
        self.folder_content_stack.set_visible_child_name("folders")

        folders_frame = Gtk.Frame()
        folders_frame.set_hexpand(True)
        folders_frame.set_vexpand(True)
        folders_frame.set_overflow(Gtk.Overflow.HIDDEN)
        folders_frame.add_css_class("rounded-scroll-frame")
        folders_frame.set_child(self.folder_content_stack)
        page.append(folders_frame)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        controls.set_halign(Gtk.Align.END)
        self.start_button = icon_button(
            _("Sincronizar agora"),
            "imap-refresh-symbolic",
        )
        self.start_button.set_tooltip_text(
            _(
                "Comparar as pastas com o servidor e baixar somente "
                "cabeçalhos novos"
            )
        )
        self.start_button.add_css_class("suggested-action")
        self.start_button.connect("clicked", self._start_sync)
        controls.append(self.start_button)
        page.append(controls)

        self.stack.add_named(page, "folders")

    def _build_progress_page(self) -> None:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        set_margins(page, 32)

        heading = Gtk.Label(label=_("Sincronizando cabeçalhos"))
        heading.set_xalign(0)
        heading.add_css_class("title-1")
        page.append(heading)
        self.progress_phase = Gtk.Label(label=_("Preparando…"))
        self.progress_phase.set_xalign(0)
        self.progress_phase.add_css_class("heading")
        page.append(self.progress_phase)

        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_show_text(True)
        self.progress_bar.set_text(_("Aguardando"))
        page.append(self.progress_bar)

        metrics = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        self.progress_count = Gtk.Label(label=_("0 de 0"))
        self.progress_count.set_xalign(0)
        self.progress_speed = Gtk.Label(label=_("0 mensagens/s"))
        self.progress_speed.set_xalign(0)
        self.progress_changes = Gtk.Label(
            label=_("0 novas · 0 ausentes · 0 erros")
        )
        self.progress_changes.set_xalign(0)
        metrics.append(self.progress_count)
        metrics.append(self.progress_speed)
        metrics.append(self.progress_changes)
        page.append(metrics)

        details_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        self.progress_details_button = Gtk.ToggleButton(
            label=_("Mostrar detalhes")
        )
        self.progress_details_button.add_css_class("portable-action")
        self.progress_details_button.set_tooltip_text(
            _("Expandir ou recolher o log técnico da sincronização")
        )
        self.progress_details_button.connect(
            "toggled",
            self._toggle_progress_details,
        )
        details_row.append(self.progress_details_button)
        self.copy_progress_log_button = Gtk.Button(label=_("Copiar log"))
        self.copy_progress_log_button.add_css_class("portable-action")
        self.copy_progress_log_button.set_tooltip_text(
            _("Copiar o log técnico para a área de transferência")
        )
        self.copy_progress_log_button.connect(
            "clicked",
            self._copy_progress_log,
        )
        self.copy_progress_log_button.set_visible(False)
        details_row.append(self.copy_progress_log_button)
        page.append(details_row)

        log_scroller = Gtk.ScrolledWindow()
        log_scroller.set_vexpand(True)
        log_scroller.set_min_content_height(220)
        self.progress_log = Gtk.TextView()
        self.progress_log.set_editable(False)
        self.progress_log.set_cursor_visible(False)
        self.progress_log.set_monospace(True)
        self.progress_log.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.progress_log.add_css_class("log-view")
        log_scroller.set_child(self.progress_log)
        self.progress_log_revealer = Gtk.Revealer()
        self.progress_log_revealer.set_transition_type(
            Gtk.RevealerTransitionType.SLIDE_DOWN
        )
        self.progress_log_revealer.set_transition_duration(180)
        self.progress_log_revealer.set_reveal_child(False)
        self.progress_log_revealer.set_vexpand(True)
        self.progress_log_revealer.set_child(
            rounded_scroll_frame(log_scroller)
        )
        page.append(self.progress_log_revealer)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        actions.set_halign(Gtk.Align.END)
        self.pause_button = Gtk.Button(label=_("Pausar"))
        self.pause_button.set_tooltip_text(
            _("Pausar com segurança após concluir o lote atual")
        )
        self.pause_button.connect("clicked", self._toggle_pause)
        self.cancel_button = Gtk.Button(label=_("Cancelar"))
        self.cancel_button.set_tooltip_text(
            _("Cancelar com segurança após concluir o lote atual")
        )
        self.cancel_button.connect("clicked", self._cancel_sync)
        actions.append(self.pause_button)
        actions.append(self.cancel_button)
        page.append(actions)
        self.stack.add_named(page, "progress")

    def _build_results_page(self) -> None:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        set_margins(page, 18)

        heading_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
        )
        heading = Gtk.Label(label=_("Resultado da sincronização"))
        heading.set_xalign(0)
        heading.set_hexpand(True)
        heading.add_css_class("title-1")
        heading_row.append(heading)
        self.results_account = Gtk.Label(label="")
        self.results_account.set_xalign(1)
        self.results_account.set_ellipsize(Pango.EllipsizeMode.END)
        self.results_account.set_max_width_chars(44)
        self.results_account.add_css_class("dim-label")
        heading_row.append(self.results_account)
        page.append(heading_row)

        self.results_view_stack = Gtk.Stack()
        self.results_view_stack.set_transition_type(
            Gtk.StackTransitionType.CROSSFADE
        )
        self.results_view_stack.set_vexpand(True)
        switcher = Gtk.StackSwitcher()
        switcher.set_stack(self.results_view_stack)
        switcher.set_halign(Gtk.Align.START)
        page.append(switcher)

        summary_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        set_margins(summary_page, 6)

        summary_grid = Gtk.Grid(
            column_spacing=8,
            row_spacing=8,
            column_homogeneous=True,
            row_homogeneous=True,
        )
        summary_grid.set_hexpand(True)

        def summary_card(caption: str) -> tuple[Gtk.Frame, Gtk.Label]:
            frame = Gtk.Frame()
            frame.add_css_class("summary-card")
            content = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL,
                spacing=4,
            )
            set_margins(content, 12)
            value = Gtk.Label(label="0", xalign=0)
            value.add_css_class("summary-value")
            label = Gtk.Label(label=caption, xalign=0)
            label.add_css_class("dim-label")
            content.append(value)
            content.append(label)
            frame.set_child(content)
            return frame, value

        messages_card, self.summary_messages_value = summary_card(
            _("Mensagens ativas")
        )
        senders_card, self.summary_senders_value = summary_card(
            _("Remetentes únicos")
        )
        domains_card, self.summary_domains_value = summary_card(
            _("Domínios únicos")
        )
        volume_card, self.summary_volume_value = summary_card(
            _("Volume original associado")
        )
        summary_grid.attach(messages_card, 0, 0, 1, 1)
        summary_grid.attach(senders_card, 1, 0, 1, 1)
        summary_grid.attach(domains_card, 0, 1, 1, 1)
        summary_grid.attach(volume_card, 1, 1, 1, 1)
        summary_page.append(summary_grid)

        summary_details = Gtk.Frame()
        summary_details.add_css_class("summary-details")
        details_content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=4,
        )
        set_margins(details_content, 10)
        self.summary_period_label = Gtk.Label(
            label="",
            wrap=True,
            xalign=0,
        )
        self.summary_errors_label = Gtk.Label(label="", xalign=0)
        self.summary_errors_label.add_css_class("dim-label")
        details_content.append(self.summary_period_label)
        details_content.append(self.summary_errors_label)
        summary_details.set_child(details_content)
        summary_page.append(summary_details)

        summary_note = Gtk.Label(
            label=_(
                "Os rankings de limpeza ignoram mensagens enviadas pela própria "
                "conta, rascunhos e itens que já estão na Lixeira."
            ),
            wrap=True,
            xalign=0,
        )
        summary_note.add_css_class("dim-label")
        summary_page.append(summary_note)
        self.results_view_stack.add_titled(
            summary_page, "summary", _("Resumo")
        )

        senders_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        sender_toolbar = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
        )
        self.sender_search = Gtk.SearchEntry()
        self.sender_search.set_placeholder_text(
            _("Filtrar entre os maiores remetentes")
        )
        self.sender_search.set_width_chars(32)
        self.sender_search.set_hexpand(True)
        self.sender_search.set_tooltip_text(
            _(
                "A lista mostra até os 500 maiores remetentes por quantidade "
                "de mensagens."
            )
        )
        self.sender_search.connect(
            "search-changed", lambda _entry: self._render_sender_rows()
        )
        sender_toolbar.append(self.sender_search)
        select_senders = Gtk.Button(label=_("Selecionar exibidos"))
        select_senders.set_tooltip_text(
            _("Marcar todos os remetentes atualmente exibidos pelo filtro")
        )
        select_senders.connect(
            "clicked",
            self._select_visible_senders,
        )
        sender_toolbar.append(select_senders)
        clear_senders = Gtk.Button(label=_("Limpar"))
        clear_senders.set_tooltip_text(_("Desmarcar todos os remetentes"))
        clear_senders.connect(
            "clicked",
            self._clear_sender_selection,
        )
        sender_toolbar.append(clear_senders)
        senders_page.append(sender_toolbar)
        sender_scroller = Gtk.ScrolledWindow()
        sender_scroller.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC
        )
        sender_scroller.set_vexpand(True)
        self.sender_list = Gtk.ListBox()
        self.sender_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.sender_list.set_activate_on_single_click(True)
        self.sender_list.connect(
            "row-activated",
            self._sender_row_activated,
        )
        self.sender_list.add_css_class("boxed-list")
        self.sender_list.add_css_class("scroll-viewport-list")
        sender_scroller.set_child(self.sender_list)
        senders_page.append(rounded_scroll_frame(sender_scroller))
        senders_page.append(self._build_cleanup_action_bar())
        self.results_view_stack.add_titled(
            senders_page, "senders", _("Remetentes")
        )

        domains_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        domain_toolbar = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
        )
        self.domain_search = Gtk.SearchEntry()
        self.domain_search.set_placeholder_text(
            _("Filtrar entre os maiores domínios")
        )
        self.domain_search.set_width_chars(32)
        self.domain_search.set_hexpand(True)
        protected_domain_note = _(
            "Domínios compartilhados, como gmail.com, ficam protegidos "
            "contra seleção em bloco. Selecione seus remetentes "
            "individualmente."
        )
        self.domain_search.set_tooltip_text(protected_domain_note)
        self.domain_search.connect(
            "search-changed", lambda _entry: self._render_domain_rows()
        )
        domain_toolbar.append(self.domain_search)
        protected_info = Gtk.Image.new_from_icon_name("imap-lock-symbolic")
        protected_info.set_tooltip_text(protected_domain_note)
        domain_toolbar.append(protected_info)
        select_domains = Gtk.Button(label=_("Selecionar permitidos"))
        select_domains.set_tooltip_text(
            _("Marcar todos os domínios exibidos que não estejam protegidos")
        )
        select_domains.connect(
            "clicked",
            self._select_visible_domains,
        )
        domain_toolbar.append(select_domains)
        clear_domains = Gtk.Button(label=_("Limpar"))
        clear_domains.set_tooltip_text(_("Desmarcar todos os domínios"))
        clear_domains.connect(
            "clicked",
            self._clear_domain_selection,
        )
        domain_toolbar.append(clear_domains)
        domains_page.append(domain_toolbar)
        domain_scroller = Gtk.ScrolledWindow()
        domain_scroller.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC
        )
        domain_scroller.set_vexpand(True)
        self.domain_list = Gtk.ListBox()
        self.domain_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.domain_list.set_activate_on_single_click(True)
        self.domain_list.connect(
            "row-activated",
            self._domain_row_activated,
        )
        self.domain_list.add_css_class("boxed-list")
        self.domain_list.add_css_class("scroll-viewport-list")
        domain_scroller.set_child(self.domain_list)
        domains_page.append(rounded_scroll_frame(domain_scroller))
        domains_page.append(self._build_cleanup_action_bar())
        self.results_view_stack.add_titled(
            domains_page, "domains", _("Domínios")
        )

        largest_page = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
        )
        largest_toolbar = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
        )
        self.largest_search = Gtk.SearchEntry()
        self.largest_search.set_placeholder_text(
            _("Pesquisar por assunto, remetente ou nome do anexo")
        )
        self.largest_search.set_width_chars(26)
        self.largest_search.set_hexpand(True)
        self.largest_search.connect(
            "search-changed",
            lambda _entry: self._reload_largest_rows(),
        )
        largest_toolbar.append(self.largest_search)
        self.largest_mode = Gtk.DropDown.new_from_strings(
            [
                _("Todas as mensagens"),
                _("Com anexos"),
            ]
        )
        self.largest_mode.set_tooltip_text(
            _("Alternar entre tamanho total e mensagens com anexos confirmados")
        )
        self.largest_mode.connect(
            "notify::selected",
            lambda _item, _param: self._reload_largest_rows(),
        )
        largest_toolbar.append(self.largest_mode)

        self.largest_extension_values = [
            "pdf",
            "zip",
            "rar",
            "7z",
            "exe",
            "doc",
            "docx",
            "xls",
            "xlsx",
            "ods",
            "jpg",
            "png",
            "mp4",
        ]
        self.largest_extension_button = Gtk.MenuButton()
        self.largest_extension_button.set_label(_("Todas as extensões"))
        self.largest_extension_button.set_tooltip_text(
            _("Filtrar pela extensão identificada no anexo")
        )
        extension_popover = Gtk.Popover()
        extension_panel = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
        )
        set_margins(extension_panel, 12)
        extension_heading = Gtk.Label(
            label=_("Extensões dos anexos"),
            xalign=0,
        )
        extension_heading.add_css_class("heading")
        extension_panel.append(extension_heading)
        extension_hint = Gtk.Label(
            label=_("Marque uma ou mais extensões para combinar o filtro."),
            wrap=True,
            xalign=0,
        )
        extension_hint.add_css_class("dim-label")
        extension_panel.append(extension_hint)
        self.largest_all_extensions = Gtk.CheckButton(
            label=_("Todas as extensões")
        )
        self.largest_all_extensions.set_active(True)
        self.largest_all_extensions.connect(
            "toggled",
            self._largest_all_extensions_toggled,
        )
        extension_panel.append(self.largest_all_extensions)
        extension_grid = Gtk.Grid()
        extension_grid.set_column_spacing(16)
        extension_grid.set_row_spacing(6)
        self.largest_extension_checks: dict[str, Gtk.CheckButton] = {}
        for index, extension in enumerate(self.largest_extension_values):
            check = Gtk.CheckButton(label=extension.upper())
            check.connect(
                "toggled",
                self._largest_extension_toggled,
            )
            self.largest_extension_checks[extension] = check
            extension_grid.attach(check, index % 4, index // 4, 1, 1)
        extension_panel.append(extension_grid)
        custom_extension_label = Gtk.Label(
            label=_("Outra extensão"),
            xalign=0,
        )
        custom_extension_label.add_css_class("heading")
        extension_panel.append(custom_extension_label)
        self.largest_custom_extension = Gtk.Entry()
        self.largest_custom_extension.set_placeholder_text(
            _("Digite sem o ponto, por exemplo: eml")
        )
        self.largest_custom_extension.set_width_chars(20)
        self.largest_custom_extension.set_max_length(16)
        self.largest_custom_extension.set_tooltip_text(
            _("Digite uma extensão sem o ponto, por exemplo: eml")
        )
        self.largest_custom_extension.connect(
            "changed",
            self._largest_custom_extension_changed,
        )
        extension_panel.append(self.largest_custom_extension)
        extension_popover.set_child(extension_panel)
        self.largest_extension_button.set_popover(extension_popover)
        largest_toolbar.append(self.largest_extension_button)

        self.largest_size_values = [
            0,
            1 * 1024 * 1024,
            5 * 1024 * 1024,
            10 * 1024 * 1024,
            25 * 1024 * 1024,
            50 * 1024 * 1024,
        ]
        self.largest_size = Gtk.DropDown.new_from_strings(
            [
                _("Qualquer tamanho"),
                _("Acima de 1 MB"),
                _("Acima de 5 MB"),
                _("Acima de 10 MB"),
                _("Acima de 25 MB"),
                _("Acima de 50 MB"),
            ]
        )
        self.largest_size.set_tooltip_text(
            _("Definir o tamanho total mínimo da mensagem")
        )
        self.largest_size.connect(
            "notify::selected",
            lambda _item, _param: self._reload_largest_rows(),
        )
        largest_toolbar.append(self.largest_size)
        largest_page.append(largest_toolbar)

        largest_info_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        self.largest_analysis_status = Gtk.Label(
            label="",
            wrap=False,
            xalign=0,
            ellipsize=Pango.EllipsizeMode.END,
        )
        self.largest_analysis_status.set_hexpand(True)
        self.largest_analysis_status.add_css_class("dim-label")
        largest_info_row.append(self.largest_analysis_status)
        select_largest = Gtk.Button(label=_("Selecionar todos os exibidos"))
        select_largest.set_tooltip_text(
            _("Selecionar todos os exibidos")
        )
        select_largest.connect(
            "clicked",
            self._select_visible_largest,
        )
        largest_info_row.append(select_largest)
        clear_largest = Gtk.Button(label=_("Limpar seleção"))
        clear_largest.set_tooltip_text(
            _("Desmarcar todas as mensagens")
        )
        clear_largest.connect(
            "clicked",
            self._clear_largest_selection,
        )
        largest_info_row.append(clear_largest)
        self.analyze_attachments_button = icon_button(
            _("Completar análise"),
            "imap-refresh-symbolic",
        )
        self.analyze_attachments_button.set_tooltip_text(
            _(
                "Analisar somente mensagens pendentes após uma sincronização "
                "interrompida. Mensagens novas já são analisadas durante a "
                "sincronização."
            )
        )
        self.analyze_attachments_button.connect(
            "clicked",
            self._start_attachment_analysis,
        )
        largest_info_row.append(self.analyze_attachments_button)
        largest_page.append(largest_info_row)

        largest_scroller = Gtk.ScrolledWindow()
        largest_scroller.set_policy(
            Gtk.PolicyType.NEVER,
            Gtk.PolicyType.AUTOMATIC,
        )
        largest_scroller.set_vexpand(True)
        self.largest_list = Gtk.ListBox()
        self.largest_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.largest_list.set_activate_on_single_click(True)
        self.largest_list.connect(
            "row-activated",
            self._largest_row_activated,
        )
        self.largest_list.add_css_class("boxed-list")
        self.largest_list.add_css_class("scroll-viewport-list")
        largest_scroller.set_child(self.largest_list)
        largest_page.append(rounded_scroll_frame(largest_scroller))
        largest_page.append(self._build_largest_action_bar())
        self.results_view_stack.add_titled(
            largest_page,
            "largest",
            _("Maiores"),
        )

        page.append(self.results_view_stack)

        self.export_status = Gtk.Label(label="", wrap=True, xalign=0)
        self.export_status.add_css_class("dim-label")
        self.export_status.set_visible(False)
        page.append(self.export_status)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        accounts_button = icon_button(
            _("Voltar para as contas"), "imap-back-symbolic"
        )
        accounts_button.set_tooltip_text(_("Voltar para as contas"))
        accounts_button.connect("clicked", self._on_back)
        actions.append(accounts_button)
        action_spacer = Gtk.Box()
        action_spacer.set_hexpand(True)
        actions.append(action_spacer)
        self.csv_button = icon_button(_("Exportar CSV"), "imap-export-symbolic")
        self.csv_button.set_tooltip_text(
            _("Exportar todas as mensagens extraídas desta conta")
        )
        self.csv_button.connect("clicked", lambda _button: self._choose_export("csv"))
        self.ods_button = icon_button(_("Exportar ODS"), "imap-export-symbolic")
        self.ods_button.add_css_class("suggested-action")
        self.ods_button.set_tooltip_text(
            _("Exportar todas as mensagens e resumos em uma planilha")
        )
        self.ods_button.connect("clicked", lambda _button: self._choose_export("ods"))
        actions.append(self.csv_button)
        actions.append(self.ods_button)
        page.append(actions)
        self.stack.add_named(page, "results")

    def _build_cleanup_action_bar(self) -> Gtk.Box:
        cleanup_bar = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=10,
        )
        cleanup_bar.add_css_class("selection-action-bar")

        selection_label = Gtk.Label(
            label=_("Nenhuma mensagem selecionada"),
            wrap=True,
            xalign=0,
        )
        selection_label.set_hexpand(True)
        cleanup_bar.append(selection_label)
        self.cleanup_selection_labels.append(selection_label)

        selected_csv = icon_button(
            _("CSV da seleção"), "imap-export-symbolic"
        )
        selected_csv.set_tooltip_text(
            _(
                "Exportar somente as mensagens dos remetentes e domínios "
                "marcados"
            )
        )
        selected_csv.set_sensitive(False)
        selected_csv.connect(
            "clicked",
            lambda _button: self._choose_export(
                "csv",
                selection_only=True,
            ),
        )
        cleanup_bar.append(selected_csv)
        self.selected_csv_buttons.append(selected_csv)

        selected_ods = icon_button(
            _("ODS da seleção"), "imap-export-symbolic"
        )
        selected_ods.set_tooltip_text(
            _("Criar uma planilha somente com as mensagens selecionadas")
        )
        selected_ods.set_sensitive(False)
        selected_ods.connect(
            "clicked",
            lambda _button: self._choose_export(
                "ods",
                selection_only=True,
            ),
        )
        cleanup_bar.append(selected_ods)
        self.selected_ods_buttons.append(selected_ods)

        cleanup_button = icon_button(
            _("Mover para a Lixeira"),
            "imap-delete-symbolic",
        )
        cleanup_button.set_tooltip_text(
            _("Mover as mensagens selecionadas para a Lixeira do servidor")
        )
        cleanup_button.add_css_class("destructive-action")
        cleanup_button.set_sensitive(False)
        cleanup_button.connect("clicked", self._confirm_cleanup)
        cleanup_bar.append(cleanup_button)
        self.cleanup_buttons.append(cleanup_button)
        return cleanup_bar

    def _build_largest_action_bar(self) -> Gtk.Box:
        bar = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=10,
        )
        bar.add_css_class("selection-action-bar")
        self.largest_selection_label = Gtk.Label(
            label=_("Nenhuma mensagem selecionada"),
            wrap=True,
            xalign=0,
        )
        self.largest_selection_label.set_hexpand(True)
        bar.append(self.largest_selection_label)

        self.largest_csv_button = icon_button(
            _("CSV da seleção"),
            "imap-export-symbolic",
        )
        self.largest_csv_button.set_sensitive(False)
        self.largest_csv_button.connect(
            "clicked",
            lambda _button: self._choose_export(
                "csv",
                selection_only=True,
                message_ids=sorted(self.selected_large_messages),
            ),
        )
        bar.append(self.largest_csv_button)

        self.largest_ods_button = icon_button(
            _("ODS da seleção"),
            "imap-export-symbolic",
        )
        self.largest_ods_button.set_sensitive(False)
        self.largest_ods_button.connect(
            "clicked",
            lambda _button: self._choose_export(
                "ods",
                selection_only=True,
                message_ids=sorted(self.selected_large_messages),
            ),
        )
        bar.append(self.largest_ods_button)

        self.largest_cleanup_button = icon_button(
            _("Mover para a Lixeira"),
            "imap-delete-symbolic",
        )
        self.largest_cleanup_button.add_css_class("destructive-action")
        self.largest_cleanup_button.set_sensitive(False)
        self.largest_cleanup_button.connect(
            "clicked",
            self._confirm_large_cleanup,
        )
        bar.append(self.largest_cleanup_button)
        return bar

    def _append_empty_result_row(self, target: Gtk.ListBox, text: str) -> None:
        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        row.set_selectable(False)
        label = Gtk.Label(label=text, wrap=True, xalign=0)
        label.add_css_class("dim-label")
        set_margins(label, 18)
        row.set_child(label)
        target.append(row)

    def _render_sender_rows(self) -> None:
        clear_box(self.sender_list)
        self.sender_row_checks.clear()
        rows = self._visible_sender_rows()
        if not rows:
            self._append_empty_result_row(
                self.sender_list,
                _("Nenhum remetente encontrado.")
                if self.sender_rows
                else _("Carregando o ranking de remetentes…"),
            )
            return
        for item in rows:
            email = str(item["email"])
            row = Gtk.ListBoxRow()
            row.set_activatable(True)
            row.set_selectable(True)
            row.set_focusable(True)
            content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            set_margins(content, 7)
            check = Gtk.CheckButton()
            check.set_focusable(False)
            check.set_active(email in self.selected_cleanup_senders)
            check.connect("toggled", self._sender_cleanup_toggled, email)
            content.append(check)
            self._attach_cleanup_row_keyboard(row, check)
            self.sender_row_checks[row] = check

            labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            labels.set_hexpand(True)
            title = Gtk.Label(
                label=str(item.get("name") or email),
                xalign=0,
                ellipsize=Pango.EllipsizeMode.END,
            )
            title.add_css_class("heading")
            labels.append(title)
            detail = Gtk.Label(
                label=(
                    email
                    + " · "
                    + (
                        f'{str(item.get("first_date") or "")[:10]} a '
                        f'{str(item.get("last_date") or "")[:10]}'
                    )
                ),
                xalign=0,
                ellipsize=Pango.EllipsizeMode.END,
            )
            detail.add_css_class("dim-label")
            labels.append(detail)
            content.append(labels)

            amount = Gtk.Label(
                label=_("{messages} mensagens\n{size}").format(
                    messages=f'{int(item["messages"]):,}'.replace(",", "."),
                    size=human_size(item.get("total_size")),
                ),
                xalign=1,
            )
            amount.set_justify(Gtk.Justification.RIGHT)
            content.append(amount)
            preview = icon_button(_("Ver"), "imap-view-symbolic")
            preview.add_css_class("portable-action")
            preview.set_valign(Gtk.Align.CENTER)
            preview.set_has_tooltip(True)
            preview.connect(
                "query-tooltip",
                self._query_subject_tooltip,
                "sender",
                email,
                int(item["messages"]),
            )
            sender_name = str(item.get("name") or "").strip()
            sender_display = (
                f"{sender_name} <{email}>" if sender_name else email
            )
            preview.connect(
                "clicked",
                lambda _button,
                value=email,
                display=sender_display: self._show_subjects_dialog(
                    "sender", value, display
                ),
            )
            content.append(preview)
            row.set_child(content)
            self.sender_list.append(row)

    def _visible_sender_rows(self) -> list[dict[str, Any]]:
        query = self.sender_search.get_text().strip().lower()
        return [
            item
            for item in self.sender_rows
            if not query
            or query in str(item.get("email") or "").lower()
            or query in str(item.get("name") or "").lower()
            or query in str(item.get("domain") or "").lower()
        ]

    def _render_domain_rows(self) -> None:
        clear_box(self.domain_list)
        self.domain_row_checks.clear()
        rows = self._visible_domain_rows()
        if not rows:
            self._append_empty_result_row(
                self.domain_list,
                _("Nenhum domínio encontrado.")
                if self.domain_rows
                else _("Carregando o ranking de domínios…"),
            )
            return
        for item in rows:
            domain = str(item["domain"])
            protected = self._domain_is_protected(domain)
            row = Gtk.ListBoxRow()
            row.set_activatable(not protected)
            row.set_selectable(True)
            row.set_focusable(True)
            content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            set_margins(content, 7)
            check = Gtk.CheckButton()
            check.set_focusable(False)
            check.set_active(
                not protected and domain in self.selected_cleanup_domains
            )
            check.set_sensitive(not protected)
            check.set_tooltip_text(
                _(
                    "Domínio compartilhado: selecione os remetentes "
                    "individualmente."
                )
                if protected
                else _("Selecionar todas as mensagens deste domínio")
            )
            check.connect("toggled", self._domain_cleanup_toggled, domain)
            content.append(check)
            self._attach_cleanup_row_keyboard(row, check)
            self.domain_row_checks[row] = check

            labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            labels.set_hexpand(True)
            title = Gtk.Label(
                label=domain,
                xalign=0,
                ellipsize=Pango.EllipsizeMode.END,
            )
            title.add_css_class("heading")
            labels.append(title)
            detail_text = (
                _("Protegido · seleção somente por remetente")
                if protected
                else _("{senders} remetentes · {first} a {last}").format(
                    senders=f'{int(item["senders"]):,}'.replace(",", "."),
                    first=str(item.get("first_date") or "")[:10],
                    last=str(item.get("last_date") or "")[:10],
                )
            )
            detail = Gtk.Label(
                label=detail_text,
                xalign=0,
                ellipsize=Pango.EllipsizeMode.END,
            )
            detail.add_css_class("dim-label")
            labels.append(detail)
            content.append(labels)

            amount = Gtk.Label(
                label=_("{messages} mensagens\n{size}").format(
                    messages=f'{int(item["messages"]):,}'.replace(",", "."),
                    size=human_size(item.get("total_size")),
                ),
                xalign=1,
            )
            amount.set_justify(Gtk.Justification.RIGHT)
            content.append(amount)
            preview = icon_button(_("Ver"), "imap-view-symbolic")
            preview.add_css_class("portable-action")
            preview.set_valign(Gtk.Align.CENTER)
            preview.set_has_tooltip(True)
            preview.connect(
                "query-tooltip",
                self._query_subject_tooltip,
                "domain",
                domain,
                int(item["messages"]),
            )
            preview.connect(
                "clicked",
                lambda _button,
                value=domain: self._show_subjects_dialog(
                    "domain", value, value
                ),
            )
            content.append(preview)
            row.set_child(content)
            self.domain_list.append(row)

    def _visible_domain_rows(self) -> list[dict[str, Any]]:
        query = self.domain_search.get_text().strip().lower()
        return [
            item
            for item in self.domain_rows
            if not query or query in str(item.get("domain") or "").lower()
        ]

    def _selected_largest_extensions(self) -> list[str]:
        selected = [
            extension
            for extension, check in self.largest_extension_checks.items()
            if check.get_active()
        ]
        custom = (
            self.largest_custom_extension.get_text()
            .strip()
            .lower()
            .lstrip(".")
        )
        if (
            custom
            and re.fullmatch(r"[a-z0-9][a-z0-9+_-]{0,15}", custom)
            and custom not in selected
        ):
            selected.append(custom)
        return selected

    def _largest_all_extensions_toggled(
        self,
        check: Gtk.CheckButton,
    ) -> None:
        if (
            self.updating_largest_extension_filter
            or not check.get_active()
        ):
            return
        self.updating_largest_extension_filter = True
        for extension_check in self.largest_extension_checks.values():
            extension_check.set_active(False)
        self.largest_custom_extension.set_text("")
        self.updating_largest_extension_filter = False
        self._update_largest_extension_filter()

    def _largest_extension_toggled(
        self,
        _check: Gtk.CheckButton,
    ) -> None:
        if self.updating_largest_extension_filter:
            return
        self._update_largest_extension_filter()

    def _largest_custom_extension_changed(
        self,
        _entry: Gtk.Entry,
    ) -> None:
        if self.updating_largest_extension_filter:
            return
        self._update_largest_extension_filter()

    def _update_largest_extension_filter(self, reload_rows: bool = True) -> None:
        extensions = self._selected_largest_extensions()
        self.updating_largest_extension_filter = True
        self.largest_all_extensions.set_active(not extensions)
        self.updating_largest_extension_filter = False
        if not extensions:
            label = _("Todas as extensões")
            tooltip = _("Filtrar pela extensão identificada no anexo")
        elif len(extensions) <= 2:
            label = " + ".join(value.upper() for value in extensions)
            tooltip = _("Extensões selecionadas: {extensions}").format(
                extensions=", ".join(
                    value.upper() for value in extensions
                )
            )
        else:
            label = _("{count} extensões").format(count=len(extensions))
            tooltip = _("Extensões selecionadas: {extensions}").format(
                extensions=", ".join(
                    value.upper() for value in extensions
                )
            )
        self.largest_extension_button.set_label(label)
        self.largest_extension_button.set_tooltip_text(tooltip)
        if reload_rows:
            self._reload_largest_rows()

    def _reset_largest_extension_filter(self) -> None:
        self.updating_largest_extension_filter = True
        for check in self.largest_extension_checks.values():
            check.set_active(False)
        self.largest_custom_extension.set_text("")
        self.largest_all_extensions.set_active(True)
        self.updating_largest_extension_filter = False
        self._update_largest_extension_filter(reload_rows=False)

    def _reload_largest_rows(self) -> None:
        if not self.active_account:
            return
        account_id = int(self.active_account["id"])
        self.largest_query_generation += 1
        generation = self.largest_query_generation
        selected_size = int(self.largest_size.get_selected())
        minimum_size = (
            self.largest_size_values[selected_size]
            if 0 <= selected_size < len(self.largest_size_values)
            else 0
        )
        extensions = self._selected_largest_extensions()
        attachments_only = (
            int(self.largest_mode.get_selected()) == 1 or bool(extensions)
        )

        def work() -> None:
            try:
                rows = self.database.largest_messages(
                    account_id,
                    search=self.largest_search.get_text(),
                    extensions=extensions,
                    minimum_size=minimum_size,
                    attachments_only=attachments_only,
                    limit=500,
                )
                summary = self.database.attachment_analysis_summary(account_id)
                GLib.idle_add(success, rows, summary)
            except Exception as exc:
                GLib.idle_add(failure, str(exc))

        def success(
            rows: list[dict[str, Any]],
            summary: dict[str, int],
        ) -> bool:
            if (
                generation != self.largest_query_generation
                or not self.active_account
                or int(self.active_account["id"]) != account_id
            ):
                return False
            self.largest_rows = rows
            self._render_largest_rows()
            indexed = int(summary.get("indexed") or 0)
            total = int(summary.get("total") or 0)
            with_attachments = int(summary.get("with_attachments") or 0)
            attachment_size = human_size(summary.get("attachment_size"))
            status_text = _(
                "{indexed} de {total} mensagens analisadas · "
                "{messages} com anexos · {size} em anexos identificados"
            ).format(
                indexed=f"{indexed:,}".replace(",", "."),
                total=f"{total:,}".replace(",", "."),
                messages=f"{with_attachments:,}".replace(",", "."),
                size=attachment_size,
            )
            self.largest_analysis_status.set_text(status_text)
            self.largest_analysis_status.set_tooltip_text(status_text)
            self.analyze_attachments_button.set_sensitive(
                indexed < total and not self.attachment_analysis_running
            )
            return False

        def failure(detail: str) -> bool:
            if generation == self.largest_query_generation:
                status_text = _(
                    "Não foi possível montar o ranking: {detail}"
                ).format(
                    detail=detail
                )
                self.largest_analysis_status.set_text(status_text)
                self.largest_analysis_status.set_tooltip_text(status_text)
            return False

        threading.Thread(target=work, daemon=True).start()

    def _render_largest_rows(self) -> None:
        clear_box(self.largest_list)
        self.largest_row_checks.clear()
        self.largest_items_by_row.clear()
        if not self.largest_rows:
            self._append_empty_result_row(
                self.largest_list,
                _("Nenhuma mensagem encontrada para estes filtros."),
            )
            return
        for item in self.largest_rows:
            message_id = int(item["id"])
            row = Gtk.ListBoxRow()
            row.set_activatable(True)
            row.set_selectable(True)
            row.set_focusable(True)
            content = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL,
                spacing=8,
            )
            set_margins(content, 7)
            check = Gtk.CheckButton()
            check.set_focusable(False)
            check.set_active(message_id in self.selected_large_messages)
            check.connect(
                "toggled",
                self._largest_message_toggled,
                message_id,
            )
            content.append(check)
            self._attach_cleanup_row_keyboard(row, check)
            self.largest_row_checks[row] = check
            self.largest_items_by_row[row] = item

            labels = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL,
                spacing=2,
            )
            labels.set_hexpand(True)
            subject = str(item.get("subject") or "").strip() or _("Sem assunto")
            title = Gtk.Label(
                label=subject,
                wrap=False,
                xalign=0,
                ellipsize=Pango.EllipsizeMode.END,
            )
            title.add_css_class("heading")
            labels.append(title)
            sender_name = str(item.get("from_name") or "").strip()
            sender_email = str(item.get("from_email") or "").strip()
            sender = (
                f"{sender_name} <{sender_email}>"
                if sender_name and sender_email
                else sender_name or sender_email
            )
            details = [
                value
                for value in (
                    sender,
                    self._short_message_date(item.get("message_date")),
                    str(item.get("source_mailbox") or "").strip(),
                )
                if value
            ]
            if int(item.get("attachment_indexed") or 0):
                attachment_count = int(item.get("attachment_count") or 0)
                if attachment_count:
                    attachment_text = _(
                        "{count} anexo(s) · {size}"
                    ).format(
                        count=attachment_count,
                        size=human_size(item.get("attachment_size_bytes")),
                    )
                    names = str(item.get("attachment_names") or "").strip()
                    if names:
                        attachment_text += " · " + names
                else:
                    attachment_text = _("Nenhum anexo identificado")
            else:
                attachment_text = _("Anexos ainda não analisados")
            secondary_text = " · ".join(
                value
                for value in (" · ".join(details), attachment_text)
                if value
            )
            secondary = Gtk.Label(
                label=secondary_text,
                wrap=False,
                xalign=0,
                ellipsize=Pango.EllipsizeMode.END,
            )
            secondary.set_tooltip_text(secondary_text)
            secondary.add_css_class("dim-label")
            labels.append(secondary)
            content.append(labels)

            size_box = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL,
                spacing=1,
            )
            size_box.set_valign(Gtk.Align.CENTER)
            size_label = Gtk.Label(
                label=human_size(item.get("size_bytes")),
                xalign=1,
            )
            size_label.add_css_class("heading")
            size_box.append(size_label)
            size_caption = Gtk.Label(
                label=_("Tamanho total"),
                xalign=1,
            )
            size_caption.add_css_class("dim-label")
            size_box.append(size_caption)
            content.append(size_box)

            view_button = icon_button(_("Ver"), "imap-view-symbolic")
            view_button.set_valign(Gtk.Align.CENTER)
            view_button.set_tooltip_text(
                _("Abrir o leitor e os anexos desta mensagem")
            )
            view_button.connect(
                "clicked",
                lambda _button, value=message_id: (
                    self._open_message_reader(value, self)
                ),
            )
            content.append(view_button)
            row.set_child(content)
            self.largest_list.append(row)

    def _largest_message_toggled(
        self,
        check: Gtk.CheckButton,
        message_id: int,
    ) -> None:
        if check.get_active():
            self.selected_large_messages.add(message_id)
        else:
            self.selected_large_messages.discard(message_id)
        self._update_largest_preview()

    def _largest_row_activated(
        self,
        _list: Gtk.ListBox,
        row: Gtk.ListBoxRow,
    ) -> None:
        self._toggle_cleanup_check(self.largest_row_checks.get(row))

    def _select_visible_largest(self, _button: Gtk.Button) -> None:
        for row, check in self.largest_row_checks.items():
            if row.get_visible():
                check.set_active(True)

    def _clear_largest_selection(self, _button: Gtk.Button) -> None:
        self.selected_large_messages.clear()
        for check in self.largest_row_checks.values():
            check.set_active(False)
        self._update_largest_preview()

    def _update_largest_preview(self) -> None:
        if not self.active_account or not self.selected_large_messages:
            self.largest_selection_label.set_text(
                _("Nenhuma mensagem selecionada")
            )
            sensitive = False
        else:
            preview = self.database.message_cleanup_preview(
                int(self.active_account["id"]),
                sorted(self.selected_large_messages),
            )
            self.largest_selection_label.set_text(
                _("{messages} mensagens selecionadas · {size}").format(
                    messages=f'{int(preview["messages"]):,}'.replace(",", "."),
                    size=human_size(preview["total_size"]),
                )
            )
            sensitive = int(preview["messages"]) > 0
        self.largest_csv_button.set_sensitive(sensitive)
        self.largest_ods_button.set_sensitive(sensitive)
        self.largest_cleanup_button.set_sensitive(sensitive)

    def _start_attachment_analysis(self, _button: Gtk.Button) -> None:
        if not self.active_account or self.attachment_analysis_running:
            return
        account = self.active_account
        session = self._require_account_unlocked(account)
        if session is None:
            return
        targets = self.database.attachment_analysis_targets(
            int(account["id"])
        )
        if not targets:
            self._show_notice(
                _("Análise de anexos"),
                _("Todas as mensagens ativas já possuem a estrutura MIME indexada."),
            )
            self._reload_largest_rows()
            return

        dialog = AppDialog(
            transient_for=self,
            title=_("Completando análise de anexos"),
            default_width=720,
            default_height=500,
        )
        self.attachment_analysis_dialog = dialog
        self.attachment_analysis_cancel_event = threading.Event()
        self.attachment_analysis_pause_event = threading.Event()
        self.attachment_analysis_running = True
        self.analyze_attachments_button.set_sensitive(False)

        content = dialog.get_content_area()
        set_margins(content, 18)
        heading = Gtk.Label(
            label=_("Analisando somente mensagens pendentes"),
            wrap=True,
            xalign=0,
        )
        heading.add_css_class("title-2")
        content.append(heading)
        explanation = Gtk.Label(
            label=_(
                "Mensagens novas já recebem essa análise junto com o "
                "cabeçalho. Esta etapa completa registros interrompidos "
                "consultando apenas a estrutura MIME; nenhum "
                "anexo será baixado."
            ),
            wrap=True,
            xalign=0,
        )
        explanation.add_css_class("dim-label")
        content.append(explanation)
        status = Gtk.Label(
            label=_("Preparando…"),
            wrap=True,
            xalign=0,
        )
        content.append(status)
        progress_bar = Gtk.ProgressBar()
        progress_bar.set_show_text(True)
        content.append(progress_bar)

        details_bar = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        details_button = Gtk.ToggleButton(label=_("Mostrar detalhes"))
        details_button.add_css_class("portable-action")
        details_button.set_tooltip_text(
            _("Expandir ou recolher o log técnico da análise de anexos")
        )
        details_bar.append(details_button)
        copy_log_button = Gtk.Button(label=_("Copiar log"))
        copy_log_button.add_css_class("portable-action")
        copy_log_button.set_visible(False)
        copy_log_button.set_tooltip_text(
            _("Copiar o log técnico para a área de transferência")
        )
        details_bar.append(copy_log_button)
        content.append(details_bar)

        log_scroller = Gtk.ScrolledWindow()
        log_scroller.set_policy(
            Gtk.PolicyType.AUTOMATIC,
            Gtk.PolicyType.AUTOMATIC,
        )
        log_scroller.set_min_content_height(210)
        log_view = Gtk.TextView()
        log_view.set_editable(False)
        log_view.set_cursor_visible(False)
        log_view.set_monospace(True)
        log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        log_view.add_css_class("log-view")
        log_scroller.set_child(log_view)
        log_revealer = Gtk.Revealer()
        log_revealer.set_transition_type(
            Gtk.RevealerTransitionType.SLIDE_DOWN
        )
        log_revealer.set_transition_duration(180)
        log_revealer.set_reveal_child(False)
        log_revealer.set_vexpand(True)
        log_revealer.set_child(rounded_scroll_frame(log_scroller))
        content.append(log_revealer)
        log_buffer = log_view.get_buffer()

        pause = dialog.add_start_button(_("Pausar"))
        stop = dialog.add_start_button(_("Interromper"))
        close = dialog.add_button(_("Fechar"), Gtk.ResponseType.CLOSE)
        close.set_sensitive(False)

        def append_log(text: str) -> None:
            end = log_buffer.get_end_iter()
            log_buffer.insert(end, text.rstrip() + "\n")
            if log_revealer.get_reveal_child():
                log_view.scroll_to_iter(
                    log_buffer.get_end_iter(),
                    0.0,
                    False,
                    0.0,
                    1.0,
                )

        def toggle_details(button: Gtk.ToggleButton) -> None:
            expanded = button.get_active()
            log_revealer.set_reveal_child(expanded)
            copy_log_button.set_visible(expanded)
            button.set_label(
                _("Ocultar detalhes")
                if expanded
                else _("Mostrar detalhes")
            )
            if expanded:
                log_view.scroll_to_iter(
                    log_buffer.get_end_iter(),
                    0.0,
                    False,
                    0.0,
                    1.0,
                )

        def copy_log(button: Gtk.Button) -> None:
            start, end = log_buffer.get_bounds()
            text = log_buffer.get_text(start, end, False)
            display = Gdk.Display.get_default()
            if display is None or not text:
                return
            display.get_clipboard().set(text)
            button.set_label(_("Copiado"))

            def restore_label() -> bool:
                if button.get_visible():
                    button.set_label(_("Copiar log"))
                return False

            GLib.timeout_add(1400, restore_label)

        def toggle_pause(_button: Gtk.Button) -> None:
            if self.attachment_analysis_pause_event.is_set():
                self.attachment_analysis_pause_event.clear()
                pause.set_label(_("Pausar"))
                status.set_text(_("Continuando…"))
                append_log(_("Análise retomada."))
            else:
                self.attachment_analysis_pause_event.set()
                pause.set_label(_("Continuar"))
                status.set_text(_("Pausa solicitada; aguardando o lote atual…"))
                append_log(_("Pausa solicitada; aguardando o lote atual."))

        def request_stop() -> None:
            self.attachment_analysis_cancel_event.set()
            self.attachment_analysis_pause_event.clear()
            pause.set_sensitive(False)
            stop.set_sensitive(False)
            status.set_text(_("Interrompendo após concluir o lote atual…"))
            append_log(_("Interrupção solicitada; aguardando o lote atual."))

        def response(current: AppDialog, response_id: int) -> None:
            if self.attachment_analysis_running:
                request_stop()
                return
            current.destroy()
            if self.attachment_analysis_dialog is current:
                self.attachment_analysis_dialog = None

        def handle_progress(event: dict[str, Any]) -> bool:
            event_type = event.get("type")
            if event_type == "planned":
                total = int(event.get("total") or 0)
                status.set_text(
                    _("{total} mensagens aguardando análise").format(
                        total=f"{total:,}".replace(",", ".")
                    )
                )
                append_log(status.get_text())
            elif event_type == "batch":
                mailbox = str(event.get("mailbox") or "")
                text = _(
                    "Analisando {amount} mensagens de “{mailbox}” · "
                    "UIDs {first}–{last}"
                ).format(
                    amount=event.get("amount", 0),
                    mailbox=mailbox,
                    first=event.get("uid_first", 0),
                    last=event.get("uid_last", 0),
                )
                status.set_text(text)
                append_log(text)
            elif event_type == "progress":
                processed = int(event.get("processed") or 0)
                total = max(1, int(event.get("total") or 0))
                progress_bar.set_fraction(min(1.0, processed / total))
                progress_bar.set_text(
                    f"{processed:,} / {total:,}".replace(",", ".")
                )
                text = _(
                    "{indexed} mensagens analisadas · {attachments} anexos · "
                    "{errors} erros"
                ).format(
                    indexed=f'{int(event.get("indexed") or 0):,}'.replace(
                        ",", "."
                    ),
                    attachments=f'{int(event.get("attachments") or 0):,}'.replace(
                        ",", "."
                    ),
                    errors=f'{int(event.get("errors") or 0):,}'.replace(
                        ",", "."
                    ),
                )
                status.set_text(text)
                append_log(text)
            return False

        def finish(result: dict[str, Any]) -> bool:
            self.attachment_analysis_running = False
            pause.set_visible(False)
            stop.set_visible(False)
            close.set_sensitive(True)
            processed = int(result.get("processed") or 0)
            total = max(1, int(result.get("total") or 0))
            progress_bar.set_fraction(min(1.0, processed / total))
            if result.get("status") == "cancelled":
                title = _("Análise interrompida")
            else:
                title = _("Análise concluída")
            status.set_text(
                _(
                    "{title}: {indexed} mensagens · {attachments} anexos · "
                    "{errors} erros"
                ).format(
                    title=title,
                    indexed=f'{int(result.get("indexed") or 0):,}'.replace(
                        ",", "."
                    ),
                    attachments=f'{int(result.get("attachments") or 0):,}'.replace(
                        ",", "."
                    ),
                    errors=f'{int(result.get("errors") or 0):,}'.replace(
                        ",", "."
                    ),
                )
            )
            append_log(status.get_text())
            self._reload_largest_rows()
            return False

        def fail(detail: str) -> bool:
            self.attachment_analysis_running = False
            pause.set_visible(False)
            stop.set_visible(False)
            close.set_sensitive(True)
            status.set_text(_("A análise de anexos falhou."))
            append_log(detail)
            details_button.set_active(True)
            self._reload_largest_rows()
            return False

        def work() -> None:
            try:
                result = self.extractor.analyze_attachments(
                    account,
                    session["imap_password"],
                    targets,
                    lambda event: GLib.idle_add(handle_progress, event),
                    self.attachment_analysis_cancel_event,
                    self.attachment_analysis_pause_event,
                )
                GLib.idle_add(finish, result)
            except Exception as exc:
                GLib.idle_add(fail, str(exc))

        pause.connect("clicked", toggle_pause)
        stop.connect("clicked", lambda _button: request_stop())
        details_button.connect("toggled", toggle_details)
        copy_log_button.connect("clicked", copy_log)
        dialog.connect("response", response)
        dialog.present()
        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _short_message_date(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        return text.replace("T", " ")[:19]

    @staticmethod
    def _safe_attachment_filename(value: Any, fallback: str = "attachment") -> str:
        name = str(value or "").replace("\x00", "").strip()
        name = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        name = re.sub(r"[\x00-\x1f\x7f]", "_", name)
        name = re.sub(r"\s+", " ", name).strip(" .")
        if not name or name in {".", ".."}:
            name = fallback
        return name[:180]

    @staticmethod
    def _available_download_path(directory: Path, filename: str) -> Path:
        candidate = directory / filename
        if not candidate.exists():
            return candidate
        stem = candidate.stem
        suffix = candidate.suffix
        for index in range(2, 10000):
            alternate = directory / f"{stem} ({index}){suffix}"
            if not alternate.exists():
                return alternate
        raise RuntimeError(_("Não foi possível gerar um nome de arquivo livre."))

    def _save_attachment_bytes(
        self,
        account: dict[str, Any],
        imap_password: str,
        target: dict[str, Any],
        attachment: dict[str, Any],
        destination: Path,
        parent: Gtk.Window,
        status: Gtk.Label,
        button: Gtk.Button,
        progress_bar: Gtk.ProgressBar,
        cancel_event: threading.Event,
    ) -> None:
        setattr(button, "_attachment_cancel_event", cancel_event)
        button.set_child(
            Gtk.Image.new_from_icon_name("imap-cancel-symbolic")
        )
        button.set_tooltip_text(_("Cancelar este download"))
        button.set_sensitive(True)
        status.set_text(_("Baixando…"))
        status.set_tooltip_text("")
        progress_bar.set_fraction(0)

        def update_progress(received: int, total: int) -> bool:
            if total > 0:
                fraction = min(1.0, max(0.0, received / total))
                progress_bar.set_fraction(fraction)
                status.set_text(
                    _("Baixando… {percent}%").format(
                        percent=round(fraction * 100)
                    )
                )
            else:
                progress_bar.pulse()
                status.set_text(_("Baixando…"))
            return False

        def work() -> None:
            temporary: Path | None = None
            try:
                payload = self.extractor.fetch_attachment(
                    account,
                    imap_password,
                    target,
                    attachment,
                    progress=lambda received, total: GLib.idle_add(
                        update_progress,
                        received,
                        total,
                    ),
                    cancel_event=cancel_event,
                )
                if cancel_event.is_set():
                    raise AttachmentDownloadCancelled()
                destination.parent.mkdir(parents=True, exist_ok=True)
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f".{destination.name}.",
                    suffix=".part",
                    dir=destination.parent,
                )
                os.close(descriptor)
                temporary = Path(temporary_name)
                temporary.write_bytes(payload)
                if cancel_event.is_set():
                    raise AttachmentDownloadCancelled()
                temporary.replace(destination)
                GLib.idle_add(success)
            except AttachmentDownloadCancelled:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
                GLib.idle_add(cancelled)
            except Exception as exc:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
                GLib.idle_add(failure, str(exc))

        def restore_button() -> None:
            if getattr(button, "_attachment_cancel_event", None) is cancel_event:
                delattr(button, "_attachment_cancel_event")
            button.set_child(
                Gtk.Image.new_from_icon_name("imap-export-symbolic")
            )
            filename = self._safe_attachment_filename(
                attachment.get("filename"),
                f'attachment-{attachment.get("part_number") or "file"}',
            )
            button.set_tooltip_text(
                _("Baixar {filename}").format(filename=filename)
            )
            button.set_sensitive(True)

        def success() -> bool:
            progress_bar.set_fraction(0)
            status.set_text(_("Concluído"))
            status.set_tooltip_text(
                _("Salvo em {path}").format(path=str(destination))
            )
            restore_button()
            return False

        def cancelled() -> bool:
            progress_bar.set_fraction(0)
            status.set_text(_("Download cancelado"))
            status.set_tooltip_text("")
            restore_button()
            return False

        def failure(detail: str) -> bool:
            progress_bar.set_fraction(0)
            status.set_text(_("Falha no download"))
            status.set_tooltip_text(detail)
            restore_button()
            self._show_error(
                _("Não foi possível baixar o anexo"),
                detail,
                parent,
            )
            return False

        threading.Thread(target=work, daemon=True).start()

    def _choose_attachment_download(
        self,
        parent: Gtk.Window,
        account: dict[str, Any],
        imap_password: str,
        target: dict[str, Any],
        attachment: dict[str, Any],
        status: Gtk.Label,
        button: Gtk.Button,
        progress_bar: Gtk.ProgressBar,
    ) -> None:
        active_cancel = getattr(button, "_attachment_cancel_event", None)
        if active_cancel is not None:
            active_cancel.set()
            button.set_sensitive(False)
            status.set_text(_("Cancelando download…"))
            return
        filename = self._safe_attachment_filename(
            attachment.get("filename"),
            f'attachment-{attachment.get("part_number") or "file"}',
        )
        chooser = Gtk.FileChooserNative.new(
            _("Salvar anexo"),
            parent,
            Gtk.FileChooserAction.SAVE,
            _("Salvar"),
            _("Cancelar"),
        )
        chooser.set_current_name(filename)

        def response(
            current: Gtk.FileChooserNative,
            response_id: int,
        ) -> None:
            if response_id != Gtk.ResponseType.ACCEPT:
                current.destroy()
                return
            selected = current.get_file()
            path = selected.get_path() if selected else None
            current.destroy()
            if path:
                cancel_event = threading.Event()
                self._save_attachment_bytes(
                    account,
                    imap_password,
                    target,
                    attachment,
                    Path(path),
                    parent,
                    status,
                    button,
                    progress_bar,
                    cancel_event,
                )

        chooser.connect("response", response)
        chooser.show()

    def _choose_all_attachments_download(
        self,
        parent: Gtk.Window,
        account: dict[str, Any],
        imap_password: str,
        target: dict[str, Any],
        attachments: list[dict[str, Any]],
        status: Gtk.Label,
        button: Gtk.Button,
        download_rows: dict[
            str,
            tuple[Gtk.ProgressBar, Gtk.Label, Gtk.Button],
        ],
    ) -> None:
        active_cancel = getattr(button, "_attachment_cancel_event", None)
        if active_cancel is not None:
            active_cancel.set()
            button.set_sensitive(False)
            status.set_text(_("Cancelando downloads…"))
            return
        if any(
            getattr(row_button, "_attachment_cancel_event", None) is not None
            for _row_progress, _row_status, row_button in download_rows.values()
        ):
            status.set_text(
                _("Conclua ou cancele o download individual em andamento.")
            )
            return
        chooser = Gtk.FileChooserNative.new(
            _("Escolher pasta para os anexos"),
            parent,
            Gtk.FileChooserAction.SELECT_FOLDER,
            _("Selecionar"),
            _("Cancelar"),
        )

        def response(
            current: Gtk.FileChooserNative,
            response_id: int,
        ) -> None:
            if response_id != Gtk.ResponseType.ACCEPT:
                current.destroy()
                return
            selected = current.get_file()
            path = selected.get_path() if selected else None
            current.destroy()
            if not path:
                return
            directory = Path(path)
            cancel_event = threading.Event()
            setattr(button, "_attachment_cancel_event", cancel_event)
            button.set_child(
                icon_label("imap-cancel-symbolic", _("Cancelar todos"))
            )
            button.set_tooltip_text(_("Interromper o download em lote"))
            button.set_sensitive(True)
            for _progress, _status, row_button in download_rows.values():
                row_button.set_sensitive(False)
            status.set_text(_("Preparando os downloads…"))

            def row_widgets(
                attachment: dict[str, Any],
            ) -> tuple[Gtk.ProgressBar, Gtk.Label, Gtk.Button] | None:
                return download_rows.get(
                    str(attachment.get("part_number") or "")
                )

            def start_row(attachment: dict[str, Any]) -> bool:
                widgets = row_widgets(attachment)
                if widgets is None:
                    return False
                row_progress, row_status, row_button = widgets
                row_progress.set_fraction(0)
                row_status.set_text(_("Baixando…"))
                row_status.set_tooltip_text("")
                return False

            def update_row(
                attachment: dict[str, Any],
                received: int,
                total: int,
            ) -> bool:
                widgets = row_widgets(attachment)
                if widgets is None:
                    return False
                row_progress, row_status, _row_button = widgets
                if total > 0:
                    fraction = min(1.0, max(0.0, received / total))
                    row_progress.set_fraction(fraction)
                    row_status.set_text(
                        _("Baixando… {percent}%").format(
                            percent=round(fraction * 100)
                        )
                    )
                else:
                    row_progress.pulse()
                return False

            def finish_row(
                attachment: dict[str, Any],
                destination: Path,
            ) -> bool:
                widgets = row_widgets(attachment)
                if widgets is None:
                    return False
                row_progress, row_status, row_button = widgets
                row_progress.set_fraction(0)
                row_status.set_text(_("Concluído"))
                row_status.set_tooltip_text(
                    _("Salvo em {path}").format(path=str(destination))
                )
                return False

            def cancel_row(attachment: dict[str, Any]) -> bool:
                widgets = row_widgets(attachment)
                if widgets is None:
                    return False
                row_progress, row_status, _row_button = widgets
                row_progress.set_fraction(0)
                row_status.set_text(_("Download cancelado"))
                row_status.set_tooltip_text("")
                return False

            def fail_row(
                attachment: dict[str, Any],
                detail: str,
            ) -> bool:
                widgets = row_widgets(attachment)
                if widgets is None:
                    return False
                row_progress, row_status, row_button = widgets
                row_progress.set_fraction(0)
                row_status.set_text(_("Falha no download"))
                row_status.set_tooltip_text(detail)
                return False

            def work() -> None:
                saved: list[Path] = []
                current_attachment: dict[str, Any] | None = None
                try:
                    directory.mkdir(parents=True, exist_ok=True)
                    for index, attachment in enumerate(attachments, start=1):
                        if cancel_event.is_set():
                            raise AttachmentDownloadCancelled()
                        current_attachment = attachment
                        GLib.idle_add(
                            status.set_text,
                            _(
                                "Baixando anexo {current} de {total}…"
                            ).format(
                                current=index,
                                total=len(attachments),
                            ),
                        )
                        GLib.idle_add(start_row, attachment)
                        filename = self._safe_attachment_filename(
                            attachment.get("filename"),
                            f'attachment-{attachment.get("part_number") or index}',
                        )
                        destination = self._available_download_path(
                            directory,
                            filename,
                        )
                        payload = self.extractor.fetch_attachment(
                            account,
                            imap_password,
                            target,
                            attachment,
                            progress=lambda received,
                            total,
                            item=attachment: GLib.idle_add(
                                update_row,
                                item,
                                received,
                                total,
                            ),
                            cancel_event=cancel_event,
                        )
                        if cancel_event.is_set():
                            raise AttachmentDownloadCancelled()
                        descriptor, temporary_name = tempfile.mkstemp(
                            prefix=f".{destination.name}.",
                            suffix=".part",
                            dir=directory,
                        )
                        os.close(descriptor)
                        temporary = Path(temporary_name)
                        try:
                            temporary.write_bytes(payload)
                            if cancel_event.is_set():
                                raise AttachmentDownloadCancelled()
                            temporary.replace(destination)
                        finally:
                            temporary.unlink(missing_ok=True)
                        saved.append(destination)
                        GLib.idle_add(
                            finish_row,
                            attachment,
                            destination,
                        )
                        current_attachment = None
                    GLib.idle_add(success, len(saved))
                except AttachmentDownloadCancelled:
                    if current_attachment is not None:
                        GLib.idle_add(cancel_row, current_attachment)
                    GLib.idle_add(cancelled, len(saved))
                except Exception as exc:
                    if current_attachment is not None:
                        GLib.idle_add(
                            fail_row,
                            current_attachment,
                            str(exc),
                        )
                    GLib.idle_add(failure, len(saved), str(exc))

            def restore_controls() -> None:
                if (
                    getattr(button, "_attachment_cancel_event", None)
                    is cancel_event
                ):
                    delattr(button, "_attachment_cancel_event")
                button.set_child(
                    icon_label("imap-export-symbolic", _("Baixar todos"))
                )
                button.set_tooltip_text(_("Baixar todos os anexos"))
                button.set_sensitive(True)
                for _progress, _status, row_button in download_rows.values():
                    row_button.set_sensitive(True)

            def success(amount: int) -> bool:
                status.set_text(
                    _("{amount} anexos salvos em {path}").format(
                        amount=amount,
                        path=str(directory),
                    )
                )
                restore_controls()
                return False

            def cancelled(amount: int) -> bool:
                status.set_text(
                    _(
                        "Download em lote interrompido · {amount} "
                        "anexo(s) salvo(s)"
                    ).format(amount=amount)
                )
                restore_controls()
                return False

            def failure(amount: int, detail: str) -> bool:
                status.set_text(
                    _("{amount} anexos foram salvos antes da falha").format(
                        amount=amount
                    )
                )
                restore_controls()
                self._show_error(
                    _("Não foi possível concluir os downloads"),
                    detail,
                    parent,
                )
                return False

            threading.Thread(target=work, daemon=True).start()

        chooser.connect("response", response)
        chooser.show()

    def _query_subject_tooltip(
        self,
        _button: Gtk.Button,
        _x: int,
        _y: int,
        _keyboard_mode: bool,
        tooltip: Gtk.Tooltip,
        item_type: str,
        value: str,
        total_messages: int,
    ) -> bool:
        if not self.active_account:
            return False
        account_id = int(self.active_account["id"])
        cache_key = (account_id, item_type, value.strip().lower())
        rows = self.subject_preview_cache.get(cache_key)
        if rows is None:
            query = {
                "sender_email": value if item_type == "sender" else None,
                "domain": value if item_type == "domain" else None,
            }
            rows = self.database.subject_messages(
                account_id,
                **query,
                limit=7,
            )
            self.subject_preview_cache[cache_key] = rows

        lines = [_("Assuntos recentes:")]
        if not rows:
            lines.append(_("Nenhum assunto disponível."))
        else:
            for item in rows:
                subject = str(item.get("subject") or "").strip()
                if not subject:
                    subject = _("Sem assunto")
                if len(subject) > 96:
                    subject = subject[:93].rstrip() + "…"
                message_date = self._short_message_date(item.get("message_date"))
                lines.append(
                    f"• {subject}" + (f" · {message_date}" if message_date else "")
                )
            if total_messages > len(rows):
                lines.append(
                    _("{remaining} assunto(s) adicional(is).").format(
                        remaining=total_messages - len(rows)
                    )
                )
        lines.append(_("Clique para abrir a lista completa."))
        tooltip.set_text("\n".join(lines))
        return True

    def _show_subjects_dialog(
        self,
        item_type: str,
        value: str,
        display_name: str,
    ) -> None:
        if not self.active_account:
            return
        account_id = int(self.active_account["id"])
        page_size = 200
        dialog = AppDialog(
            transient_for=self,
            title=_("Assuntos das mensagens"),
            default_width=820,
            default_height=620,
        )
        content = dialog.get_content_area()
        set_margins(content, 18)
        heading_text = (
            _("Mensagens de {item}")
            if item_type == "sender"
            else _("Mensagens do domínio {item}")
        ).format(item=display_name)
        heading = Gtk.Label(
            label=heading_text,
            wrap=True,
            xalign=0,
        )
        heading.add_css_class("title-2")
        content.append(heading)

        note = Gtk.Label(
            label=_(
                "Os metadados abaixo já estão no índice local; o conteúdo "
                "só é consultado quando você abre uma mensagem."
            ),
            wrap=True,
            xalign=0,
        )
        note.add_css_class("dim-label")
        content.append(note)

        filters = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        search = Gtk.SearchEntry()
        search.set_placeholder_text(_("Pesquisar por assunto ou remetente"))
        search.set_hexpand(True)
        filters.append(search)

        filters.append(Gtk.Label(label=_("De")))
        date_from_entry = Gtk.Entry()
        date_from_entry.set_placeholder_text("DD/MM/AAAA")
        date_from_entry.set_width_chars(10)
        date_from_entry.set_max_width_chars(10)
        date_from_entry.set_max_length(10)
        date_from_entry.set_input_purpose(Gtk.InputPurpose.DIGITS)
        date_from_entry.set_tooltip_text(
            _("Data inicial no formato DD/MM/AAAA")
        )
        filters.append(date_from_entry)

        filters.append(Gtk.Label(label=_("Até")))
        date_to_entry = Gtk.Entry()
        date_to_entry.set_placeholder_text("DD/MM/AAAA")
        date_to_entry.set_width_chars(10)
        date_to_entry.set_max_width_chars(10)
        date_to_entry.set_max_length(10)
        date_to_entry.set_input_purpose(Gtk.InputPurpose.DIGITS)
        date_to_entry.set_tooltip_text(
            _("Data final no formato DD/MM/AAAA")
        )
        filters.append(date_to_entry)

        clear_dates = icon_button(_("Limpar datas"), "imap-cancel-symbolic")
        clear_dates.add_css_class("portable-action")
        clear_dates.set_tooltip_text(_("Remover o intervalo de datas"))
        clear_dates.set_sensitive(False)
        filters.append(clear_dates)
        content.append(filters)

        status_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
        )
        status = Gtk.Label(label="", wrap=True, xalign=0)
        status.set_hexpand(True)
        status.add_css_class("dim-label")
        status_row.append(status)
        open_hint = icon_label(
            "imap-view-symbolic",
            _("Duplo clique para ler"),
        )
        open_hint.add_css_class("message-open-hint")
        status_row.append(open_hint)
        content.append(status_row)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        message_list = Gtk.ListBox()
        message_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        message_list.set_activate_on_single_click(False)
        message_list.add_css_class("boxed-list")
        message_list.add_css_class("scroll-viewport-list")
        scroller.set_child(message_list)
        content.append(rounded_scroll_frame(scroller))

        load_more = dialog.add_start_button(_("Carregar mais"))
        load_more.set_child(
            icon_label("imap-add-symbolic", _("Carregar mais"))
        )
        load_more.set_tooltip_text(_("Carregar a próxima página de assuntos"))
        load_more.set_visible(False)
        dialog.add_button(_("Fechar"), Gtk.ResponseType.CLOSE)
        offset = 0
        total_matches = 0
        search_timeout_id: int | None = None
        message_by_row: dict[Gtk.ListBoxRow, dict[str, Any]] = {}

        def append_message_row(item: dict[str, Any]) -> None:
            row = Gtk.ListBoxRow()
            row.set_activatable(True)
            row.set_selectable(True)
            row.set_tooltip_text(
                _("Dê um duplo clique para abrir esta mensagem")
            )
            row_content = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL,
                spacing=3,
            )
            set_margins(row_content, 10)
            subject = str(item.get("subject") or "").strip() or _("Sem assunto")
            subject_label = Gtk.Label(
                label=subject,
                wrap=True,
                xalign=0,
            )
            subject_label.add_css_class("heading")
            row_content.append(subject_label)

            sender_name = str(item.get("from_name") or "").strip()
            sender_email = str(item.get("from_email") or "").strip()
            sender = (
                f"{sender_name} <{sender_email}>"
                if sender_name and sender_email
                else sender_name or sender_email
            )
            details = [
                detail
                for detail in (
                    self._short_message_date(item.get("message_date")),
                    sender,
                    str(item.get("source_mailbox") or "").strip(),
                    human_size(item.get("size_bytes")),
                )
                if detail
            ]
            detail_label = Gtk.Label(
                label=" · ".join(details),
                wrap=True,
                xalign=0,
            )
            detail_label.add_css_class("dim-label")
            row_content.append(detail_label)
            row.set_child(row_content)
            message_by_row[row] = item
            message_list.append(row)

        def parse_date_entry(entry: Gtk.Entry) -> str | None:
            text = entry.get_text().strip()
            entry.remove_css_class("error")
            if not text:
                return None
            try:
                return datetime.strptime(text, "%d/%m/%Y").date().isoformat()
            except ValueError:
                entry.add_css_class("error")
                raise

        def selected_date_range() -> tuple[str | None, str | None] | None:
            try:
                date_from = parse_date_entry(date_from_entry)
                date_to = parse_date_entry(date_to_entry)
            except ValueError:
                status.set_text(_("Use datas no formato DD/MM/AAAA."))
                return None
            if date_from and date_to and date_from > date_to:
                date_from_entry.add_css_class("error")
                date_to_entry.add_css_class("error")
                status.set_text(
                    _("A data inicial não pode ser posterior à data final.")
                )
                return None
            return date_from, date_to

        def load_page(reset: bool = False) -> None:
            nonlocal offset, total_matches
            date_range = selected_date_range()
            if date_range is None:
                load_more.set_visible(False)
                return
            date_from, date_to = date_range
            if reset:
                offset = 0
                clear_box(message_list)
                message_by_row.clear()
            query = {
                "sender_email": value if item_type == "sender" else None,
                "domain": value if item_type == "domain" else None,
            }
            if reset:
                total_matches = self.database.count_subject_messages(
                    account_id,
                    **query,
                    search=search.get_text(),
                    date_from=date_from,
                    date_to=date_to,
                )
            rows = self.database.subject_messages(
                account_id,
                **query,
                search=search.get_text(),
                date_from=date_from,
                date_to=date_to,
                limit=page_size,
                offset=offset,
            )
            for item in rows:
                append_message_row(item)
            offset += len(rows)
            if offset == 0:
                self._append_empty_result_row(
                    message_list,
                    _("Nenhuma mensagem encontrada para este filtro."),
                )
            has_more = offset < total_matches
            status_text = _(
                "{shown} de {total} mensagens exibidas"
            ).format(shown=offset, total=total_matches)
            if has_more:
                status_text += " · " + _(
                    "há mais resultados; use Carregar mais"
                )
            elif total_matches:
                status_text += " · " + _(
                    "todas as mensagens foram carregadas"
                )
            status.set_text(status_text)
            load_more.set_visible(has_more)

        def apply_search() -> bool:
            nonlocal search_timeout_id
            search_timeout_id = None
            load_page(reset=True)
            return False

        def on_filter_changed(_entry: Gtk.Editable) -> None:
            nonlocal search_timeout_id
            if search_timeout_id is not None:
                GLib.source_remove(search_timeout_id)
            clear_dates.set_sensitive(
                bool(
                    date_from_entry.get_text().strip()
                    or date_to_entry.get_text().strip()
                )
            )
            load_more.set_visible(False)
            search_timeout_id = GLib.timeout_add(300, apply_search)

        def clear_date_filter(_button: Gtk.Button) -> None:
            date_from_entry.set_text("")
            date_to_entry.set_text("")
            date_from_entry.remove_css_class("error")
            date_to_entry.remove_css_class("error")
            search.grab_focus()

        def close_dialog(
            _dialog: AppDialog,
            _response: int,
        ) -> None:
            nonlocal search_timeout_id
            if search_timeout_id is not None:
                GLib.source_remove(search_timeout_id)
                search_timeout_id = None
            dialog.destroy()

        search.connect("search-changed", on_filter_changed)
        date_from_entry.connect("changed", on_filter_changed)
        date_to_entry.connect("changed", on_filter_changed)
        clear_dates.connect("clicked", clear_date_filter)
        load_more.connect("clicked", lambda _button: load_page())
        message_list.connect(
            "row-activated",
            lambda _list, row: self._open_message_reader(
                int(message_by_row[row]["id"]),
                dialog,
            )
            if row in message_by_row
            else None,
        )
        dialog.connect("response", close_dialog)
        load_page(reset=True)
        dialog.present_with_focus(search)

    def _open_message_reader(
        self,
        message_id: int,
        parent: Gtk.Window,
    ) -> None:
        account = self.active_account
        if account is None:
            return
        session = self._require_account_unlocked(account)
        if session is None:
            return
        imap_password = session["imap_password"]
        target = self.database.message_reader_target(
            int(account["id"]),
            message_id,
        )
        if target is None:
            self._show_error(
                _("Mensagem não localizável"),
                _(
                    "Não há um UID atual para esta mensagem. Sincronize a "
                    "conta e tente novamente."
                ),
                parent,
            )
            return
        attachments = self.database.message_attachments(
            int(account["id"]),
            message_id,
        )

        dialog = AppDialog(
            transient_for=parent,
            title=_("Leitor de mensagem"),
            default_width=860,
            default_height=620,
        )
        dialog.set_modal(False)
        dialog.set_resizable(True)
        dialog.set_footer_visible(False)
        reader_closed = threading.Event()
        content = dialog.get_content_area()
        set_margins(content, 18)

        reader_stack = Gtk.Stack()
        reader_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        reader_stack.set_transition_duration(220)
        reader_stack.set_vexpand(True)
        reader_stack.add_css_class("message-reader-stack")
        content.append(reader_stack)

        loading_page = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
        )
        loading_page.set_halign(Gtk.Align.CENTER)
        loading_page.set_valign(Gtk.Align.CENTER)
        loading_page.set_hexpand(True)
        loading_page.set_vexpand(True)
        loading_card = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
        )
        loading_card.set_halign(Gtk.Align.CENTER)
        loading_card.add_css_class("message-reader-state-card")
        spinner = Gtk.Spinner()
        spinner.set_size_request(32, 32)
        spinner.set_halign(Gtk.Align.CENTER)
        spinner.start()
        loading_card.append(spinner)
        loading_heading = Gtk.Label(
            label=_("Abrindo mensagem"),
            xalign=0.5,
        )
        loading_heading.add_css_class("title-2")
        loading_card.append(loading_heading)
        loading_subject = Gtk.Label(
            label=str(target.get("subject") or "").strip() or _("Sem assunto"),
            wrap=True,
            justify=Gtk.Justification.CENTER,
        )
        loading_subject.set_max_width_chars(54)
        loading_subject.add_css_class("heading")
        loading_card.append(loading_subject)
        loading_status = Gtk.Label(
            label=_("Buscando a mensagem sem marcá-la como lida…"),
            wrap=True,
            justify=Gtk.Justification.CENTER,
        )
        loading_status.set_max_width_chars(54)
        loading_status.add_css_class("dim-label")
        loading_card.append(loading_status)
        loading_page.append(loading_card)
        reader_stack.add_named(loading_page, "loading")

        message_page = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
        )
        message_page.set_vexpand(True)

        overview = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=3,
        )
        overview.add_css_class("message-reader-overview")
        subject_label = Gtk.Label(
            label="",
            wrap=True,
            xalign=0,
        )
        subject_label.add_css_class("title-2")
        overview.append(subject_label)
        overview_meta = Gtk.Label(label="", wrap=True, xalign=0)
        overview_meta.add_css_class("dim-label")
        overview.append(overview_meta)
        message_page.append(overview)

        sections = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
        )
        sections.set_vexpand(True)
        message_page.append(sections)

        header_card = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=7,
        )
        header_card.add_css_class("message-reader-header")

        metadata_rows: dict[str, tuple[Gtk.Box, Gtk.Label]] = {}
        for key, title in (
            ("from", _("De")),
            ("to", _("Para")),
            ("cc", _("Cc")),
            ("date", _("Data")),
        ):
            row = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL,
                spacing=12,
            )
            row.add_css_class("message-reader-meta-row")
            key_label = Gtk.Label(label=title, xalign=0)
            key_label.set_size_request(54, -1)
            key_label.add_css_class("message-reader-meta-key")
            row.append(key_label)
            value_label = Gtk.Label(label="", wrap=True, xalign=0)
            value_label.set_hexpand(True)
            value_label.set_selectable(False)
            value_label.add_css_class("message-reader-meta-value")
            row.append(value_label)
            header_card.append(row)
            metadata_rows[key] = (row, value_label)

        details_expander = Gtk.Expander(label=_("Detalhes da mensagem"))
        details_expander.add_css_class("message-reader-section")
        details_expander.set_child(header_card)
        sections.append(details_expander)

        attachments_expander = Gtk.Expander()
        attachments_expander.add_css_class("message-reader-section")
        attachments_expander_label = Gtk.Label(
            label=_("Anexos"),
            xalign=0,
        )
        attachments_expander.set_label_widget(attachments_expander_label)
        attachments_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
        )
        attachments_box.add_css_class("message-reader-section-content")
        attachments_heading_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        download_all_status = Gtk.Label(label="", wrap=True, xalign=1)
        download_all_status.set_hexpand(True)
        download_all_status.add_css_class("dim-label")
        attachments_heading_row.append(download_all_status)
        download_all = icon_button(
            _("Baixar todos"),
            "imap-export-symbolic",
        )
        download_all.add_css_class("portable-action")
        download_all.set_tooltip_text(_("Baixar todos os anexos"))
        download_all.set_visible(len(attachments) > 1)
        attachments_heading_row.append(download_all)
        attachments_box.append(attachments_heading_row)

        attachments_content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
        )
        attachments_box.append(attachments_content)
        attachment_download_rows: dict[
            str,
            tuple[Gtk.ProgressBar, Gtk.Label, Gtk.Button],
        ] = {}

        def render_attachments(indexed: bool) -> None:
            clear_box(attachments_content)
            attachment_download_rows.clear()
            download_all_status.set_text("")
            download_all.set_visible(len(attachments) > 1)
            total_attachment_size = sum(
                int(item.get("size_bytes") or 0) for item in attachments
            )
            attachments_expander_label.set_text(
                (
                    _("Anexos · {count} arquivo(s) · {size}").format(
                        count=f"{len(attachments):,}".replace(",", "."),
                        size=human_size(total_attachment_size),
                    )
                    if attachments
                    else _("Anexos")
                )
            )
            if not attachments:
                attachments_empty = Gtk.Label(
                    label=(
                        _("Nenhum anexo identificado nesta mensagem.")
                        if indexed
                        else _(
                            "Identificando os anexos ao abrir a mensagem…"
                        )
                    ),
                    wrap=True,
                    xalign=0,
                )
                attachments_empty.add_css_class("dim-label")
                attachments_content.append(attachments_empty)
                return

            attachment_list = Gtk.ListBox()
            attachment_list.set_selection_mode(Gtk.SelectionMode.NONE)
            attachment_list.add_css_class("boxed-list")
            attachment_list.add_css_class("attachment-list")
            for attachment in attachments:
                attachment_row = Gtk.ListBoxRow()
                attachment_row.set_selectable(False)
                attachment_content = Gtk.Box(
                    orientation=Gtk.Orientation.HORIZONTAL,
                    spacing=8,
                )
                set_margins(attachment_content, 6)
                filename = self._safe_attachment_filename(
                    attachment.get("filename"),
                    f'attachment-{attachment.get("part_number") or "file"}',
                )
                filename_label = Gtk.Label(
                    label=filename,
                    xalign=0,
                    ellipsize=Pango.EllipsizeMode.MIDDLE,
                )
                filename_label.set_hexpand(True)
                filename_label.set_tooltip_text(filename)
                filename_label.add_css_class("heading")
                attachment_content.append(filename_label)
                attachment_detail = Gtk.Label(
                    label=" · ".join(
                        value
                        for value in (
                            human_size(attachment.get("size_bytes")),
                            (
                                str(attachment.get("extension") or "").upper()
                                or None
                            ),
                        )
                        if value
                    ),
                    xalign=1,
                )
                attachment_detail.set_tooltip_text(
                    str(attachment.get("content_type") or "")
                )
                attachment_detail.add_css_class("dim-label")
                attachment_content.append(attachment_detail)
                download_status = Gtk.Label(
                    label="",
                    xalign=1,
                    ellipsize=Pango.EllipsizeMode.END,
                )
                download_status.set_max_width_chars(24)
                download_status.add_css_class("dim-label")
                attachment_content.append(download_status)
                download = icon_only_button("imap-export-symbolic")
                download.add_css_class("compact-action")
                download.set_tooltip_text(
                    _("Baixar {filename}").format(filename=filename)
                )
                download_progress = Gtk.ProgressBar()
                download_progress.set_fraction(0)
                download_progress.set_show_text(False)
                download_progress.set_hexpand(True)
                download_progress.set_valign(Gtk.Align.FILL)
                download_progress.add_css_class(
                    "attachment-row-progress"
                )
                download.connect(
                    "clicked",
                    lambda button,
                    item=attachment,
                    item_status=download_status,
                    item_progress=download_progress: (
                        self._choose_attachment_download(
                            dialog,
                            account,
                            imap_password,
                            target,
                            item,
                            item_status,
                            button,
                            item_progress,
                        )
                    ),
                )
                attachment_content.append(download)
                attachment_overlay = Gtk.Overlay()
                attachment_overlay.set_child(download_progress)
                attachment_overlay.add_overlay(attachment_content)
                attachment_overlay.set_measure_overlay(
                    attachment_content,
                    True,
                )
                attachment_overlay.set_clip_overlay(
                    attachment_content,
                    True,
                )
                attachment_row.set_child(attachment_overlay)
                attachment_download_rows[
                    str(attachment.get("part_number") or "")
                ] = (download_progress, download_status, download)
                attachment_list.append(attachment_row)
            attachment_scroller = Gtk.ScrolledWindow()
            attachment_scroller.set_policy(
                Gtk.PolicyType.NEVER,
                Gtk.PolicyType.AUTOMATIC,
            )
            attachment_scroller.set_propagate_natural_height(True)
            attachment_scroller.set_max_content_height(180)
            attachment_scroller.set_overflow(Gtk.Overflow.HIDDEN)
            attachment_scroller.add_css_class("attachment-scroller")
            attachment_scroller.set_child(attachment_list)
            attachments_content.append(attachment_scroller)

            executable_extensions = {
                "exe",
                "msi",
                "bat",
                "cmd",
                "com",
                "scr",
                "appimage",
                "sh",
            }
            if any(
                str(item.get("extension") or "").lower()
                in executable_extensions
                for item in attachments
            ):
                warning = Gtk.Label(
                    label=_(
                        "Atenção: esta mensagem contém arquivo executável. "
                        "O aplicativo pode salvá-lo, mas nunca o abrirá."
                    ),
                    wrap=True,
                    xalign=0,
                )
                warning.add_css_class("warning")
                attachments_content.append(warning)

        render_attachments(bool(int(target.get("attachment_indexed") or 0)))
        attachments_expander.set_child(attachments_box)
        sections.append(attachments_expander)
        download_all.connect(
            "clicked",
            lambda button: self._choose_all_attachments_download(
                dialog,
                account,
                imap_password,
                target,
                attachments,
                download_all_status,
                button,
                attachment_download_rows,
            ),
        )

        body_scroller = Gtk.ScrolledWindow()
        body_scroller.set_policy(
            Gtk.PolicyType.AUTOMATIC,
            Gtk.PolicyType.AUTOMATIC,
        )
        body_scroller.set_vexpand(True)
        body_scroller.set_min_content_height(220)
        body_view = Gtk.TextView()
        body_view.set_editable(False)
        body_view.set_cursor_visible(False)
        body_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        body_view.set_left_margin(12)
        body_view.set_right_margin(12)
        body_view.set_top_margin(12)
        body_view.set_bottom_margin(12)
        body_view.add_css_class("message-reader")
        body_scroller.set_child(body_view)
        body_frame = Gtk.Frame()
        body_frame.set_vexpand(True)
        body_frame.set_overflow(Gtk.Overflow.HIDDEN)
        body_frame.add_css_class("message-reader-body-card")
        body_frame.set_child(body_scroller)

        content_expander = Gtk.Expander(label=_("Conteúdo da mensagem"))
        content_expander.add_css_class("message-reader-section")
        content_expander.set_child(body_frame)
        content_expander.set_expanded(True)
        content_expander.set_vexpand(True)
        sections.append(content_expander)

        section_expanders = (
            details_expander,
            attachments_expander,
            content_expander,
        )
        accordion_changing = False

        def section_expanded(
            current: Gtk.Expander,
            _param: GObject.ParamSpec,
        ) -> None:
            nonlocal accordion_changing
            if accordion_changing:
                return
            accordion_changing = True
            if current.get_expanded():
                for other in section_expanders:
                    if other is not current:
                        other.set_expanded(False)
            for section in section_expanders:
                section.set_vexpand(
                    section is content_expander and section.get_expanded()
                )
            accordion_changing = False

        for section in section_expanders:
            section.connect("notify::expanded", section_expanded)

        privacy_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        privacy_box.add_css_class("message-reader-privacy")
        privacy_icon = Gtk.Image.new_from_icon_name("imap-lock-symbolic")
        privacy_icon.set_pixel_size(16)
        privacy_icon.set_valign(Gtk.Align.START)
        privacy_box.append(privacy_icon)
        privacy_note = Gtk.Label(
            label=_(
                "O conteúdo é mantido apenas na memória desta janela. "
                "Imagens externas e scripts não são executados. Anexos só "
                "são gravados quando você escolhe baixá-los."
            ),
            wrap=True,
            xalign=0,
        )
        privacy_note.add_css_class("dim-label")
        privacy_note.set_hexpand(True)
        privacy_box.append(privacy_note)
        message_page.append(privacy_box)
        reader_stack.add_named(message_page, "message")

        subject_label.set_text(
            str(target.get("subject") or "").strip() or _("Sem assunto")
        )
        target_sender_name = str(target.get("from_name") or "").strip()
        target_sender_email = str(target.get("from_email") or "").strip()
        initial_values = {
            "from": (
                f"{target_sender_name} <{target_sender_email}>"
                if target_sender_name and target_sender_email
                else target_sender_name or target_sender_email
            ),
            "to": str(target.get("to_addresses") or "").strip(),
            "cc": str(target.get("cc_addresses") or "").strip(),
            "date": str(target.get("date_header_raw") or "").strip(),
        }

        def update_overview_meta(values: dict[str, str]) -> None:
            overview_meta.set_text(
                " · ".join(
                    value
                    for value in (
                        values.get("from", "").strip(),
                        values.get("date", "").strip(),
                    )
                    if value
                )
            )

        for key, value in initial_values.items():
            row, value_label = metadata_rows[key]
            value_label.set_text(value)
            row.set_visible(bool(value))
        update_overview_meta(initial_values)

        error_page = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
        )
        error_page.set_halign(Gtk.Align.CENTER)
        error_page.set_valign(Gtk.Align.CENTER)
        error_page.set_hexpand(True)
        error_page.set_vexpand(True)
        error_card = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
        )
        error_card.set_halign(Gtk.Align.CENTER)
        error_card.add_css_class("message-reader-state-card")
        error_icon = Gtk.Image.new_from_icon_name("imap-view-symbolic")
        error_icon.set_pixel_size(32)
        error_icon.set_halign(Gtk.Align.CENTER)
        error_card.append(error_icon)
        error_heading = Gtk.Label(
            label=_("Não foi possível abrir a mensagem."),
            wrap=True,
            justify=Gtk.Justification.CENTER,
        )
        error_heading.add_css_class("title-2")
        error_card.append(error_heading)
        error_detail = Gtk.Label(
            label="",
            wrap=True,
            justify=Gtk.Justification.CENTER,
        )
        error_detail.set_max_width_chars(58)
        error_detail.add_css_class("dim-label")
        error_card.append(error_detail)
        error_page.append(error_card)
        reader_stack.add_named(error_page, "error")
        reader_stack.set_visible_child_name("loading")

        def close_dialog(
            current_dialog: AppDialog,
            _response_id: int,
        ) -> None:
            reader_closed.set()
            batch_cancel = getattr(
                download_all,
                "_attachment_cancel_event",
                None,
            )
            if batch_cancel is not None:
                batch_cancel.set()
            for _progress, _status, row_button in (
                attachment_download_rows.values()
            ):
                row_cancel = getattr(
                    row_button,
                    "_attachment_cancel_event",
                    None,
                )
                if row_cancel is not None:
                    row_cancel.set()
            current_dialog.destroy()

        def success(message: dict[str, str]) -> bool:
            if reader_closed.is_set():
                return False
            spinner.stop()
            refreshed_attachments = self.database.message_attachments(
                int(account["id"]),
                message_id,
            )
            attachments.clear()
            attachments.extend(refreshed_attachments)
            render_attachments(True)
            subject_label.set_text(
                str(message.get("subject") or "").strip()
                or str(target.get("subject") or "").strip()
                or _("Sem assunto")
            )
            for key in ("from", "to", "cc", "date"):
                value = (
                    str(message.get(key) or "").strip()
                    or initial_values.get(key, "")
                )
                row, value_label = metadata_rows[key]
                value_label.set_text(value)
                row.set_visible(bool(value))
            update_overview_meta(
                {
                    key: (
                        str(message.get(key) or "").strip()
                        or initial_values.get(key, "")
                    )
                    for key in ("from", "date")
                }
            )
            body = str(message.get("body") or "").strip()
            body_view.get_buffer().set_text(
                body
                or _(
                    "Esta mensagem não possui uma parte de texto que o "
                    "leitor leve consiga exibir."
                )
            )
            reader_stack.set_visible_child_name("message")
            return False

        def failure(detail: str) -> bool:
            if reader_closed.is_set():
                return False
            spinner.stop()
            refreshed_attachments = self.database.message_attachments(
                int(account["id"]),
                message_id,
            )
            attachments.clear()
            attachments.extend(refreshed_attachments)
            render_attachments(True)
            body_view.get_buffer().set_text(
                _("Não foi possível carregar o corpo da mensagem.\n\n{detail}").format(
                    detail=_(detail)
                )
            )
            reader_stack.set_visible_child_name("message")
            return False

        def work() -> None:
            try:
                message = self.extractor.fetch_message_for_reader(
                    account,
                    imap_password,
                    target,
                )
                GLib.idle_add(success, message)
            except Exception as exc:
                GLib.idle_add(failure, str(exc))

        dialog.connect("response", close_dialog)
        dialog.present()
        threading.Thread(target=work, daemon=True).start()

    def _domain_is_protected(self, domain: str) -> bool:
        account_domain = ""
        if self.active_account and "@" in self.active_account["email"]:
            account_domain = (
                self.active_account["email"].rsplit("@", 1)[1].lower()
            )
        return (
            domain.lower() in SHARED_EMAIL_DOMAINS
            or domain.lower() == account_domain
        )

    def _attach_cleanup_row_keyboard(
        self,
        row: Gtk.ListBoxRow,
        check: Gtk.CheckButton,
    ) -> None:
        controller = Gtk.EventControllerKey()
        controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        controller.connect(
            "key-pressed",
            self._cleanup_row_key_pressed,
            check,
        )
        row.add_controller(controller)

    def _cleanup_row_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        _state: Gdk.ModifierType,
        check: Gtk.CheckButton,
    ) -> bool:
        if keyval != Gdk.KEY_space:
            return False
        self._toggle_cleanup_check(check)
        return True

    @staticmethod
    def _toggle_cleanup_check(check: Gtk.CheckButton | None) -> None:
        if check is not None and check.get_sensitive():
            check.set_active(not check.get_active())

    def _sender_row_activated(
        self,
        _list_box: Gtk.ListBox,
        row: Gtk.ListBoxRow,
    ) -> None:
        self._toggle_cleanup_check(self.sender_row_checks.get(row))

    def _domain_row_activated(
        self,
        _list_box: Gtk.ListBox,
        row: Gtk.ListBoxRow,
    ) -> None:
        self._toggle_cleanup_check(self.domain_row_checks.get(row))

    def _select_visible_senders(self, _button: Gtk.Button) -> None:
        self.selected_cleanup_senders.update(
            str(item["email"])
            for item in self._visible_sender_rows()
        )
        self._render_sender_rows()
        self._update_cleanup_preview()

    def _clear_sender_selection(self, _button: Gtk.Button) -> None:
        self.selected_cleanup_senders.clear()
        self._render_sender_rows()
        self._update_cleanup_preview()

    def _select_visible_domains(self, _button: Gtk.Button) -> None:
        self.selected_cleanup_domains.update(
            str(item["domain"])
            for item in self._visible_domain_rows()
            if not self._domain_is_protected(str(item["domain"]))
        )
        self._render_domain_rows()
        self._update_cleanup_preview()

    def _clear_domain_selection(self, _button: Gtk.Button) -> None:
        self.selected_cleanup_domains.clear()
        self._render_domain_rows()
        self._update_cleanup_preview()

    def _sender_cleanup_toggled(
        self, check: Gtk.CheckButton, email: str
    ) -> None:
        if check.get_active():
            self.selected_cleanup_senders.add(email)
        else:
            self.selected_cleanup_senders.discard(email)
        self._update_cleanup_preview()

    def _domain_cleanup_toggled(
        self, check: Gtk.CheckButton, domain: str
    ) -> None:
        if check.get_active():
            self.selected_cleanup_domains.add(domain)
        else:
            self.selected_cleanup_domains.discard(domain)
        self._update_cleanup_preview()

    def _update_cleanup_preview(self) -> None:
        if not self.active_account:
            return
        preview = self.database.cleanup_preview(
            int(self.active_account["id"]),
            sorted(self.selected_cleanup_senders),
            sorted(self.selected_cleanup_domains),
        )
        messages = int(preview["messages"])
        if messages:
            selection_text = _(
                "{messages} mensagens únicas selecionadas · {size}"
            ).format(
                messages=f"{messages:,}".replace(",", "."),
                size=human_size(preview["total_size"]),
            )
        else:
            selection_text = _("Nenhuma mensagem selecionada")
        for label in self.cleanup_selection_labels:
            label.set_text(selection_text)
        for button in (
            *self.cleanup_buttons,
            *self.selected_csv_buttons,
            *self.selected_ods_buttons,
        ):
            button.set_sensitive(messages > 0)

    def refresh_accounts(self) -> None:
        clear_box(self.accounts_list)
        accounts = self.database.list_accounts()
        if not accounts:
            row = Gtk.ListBoxRow()
            empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            set_margins(empty, 28)
            empty_mark = Gtk.Label(label="IMAP")
            empty_mark.add_css_class("app-mark")
            empty.append(empty_mark)
            title = Gtk.Label(label=_("Nenhuma conta cadastrada"))
            title.add_css_class("title-2")
            empty.append(title)
            text = Gtk.Label(
                label=_(
                    "Adicione uma conta IMAP para começar. Gmail e outros "
                    "provedores são compatíveis."
                ),
                wrap=True,
            )
            text.add_css_class("dim-label")
            empty.append(text)
            add = Gtk.Button(label=_("Adicionar primeira conta"))
            add.set_tooltip_text(_("Cadastrar uma nova conta IMAP"))
            add.set_halign(Gtk.Align.CENTER)
            add.add_css_class("suggested-action")
            add.connect("clicked", self._show_add_account_dialog)
            empty.append(add)
            row.set_child(empty)
            self.accounts_list.append(row)
            return

        for account in accounts:
            account_id = int(account["id"])
            unlocked = account_id in self.unlocked_accounts
            recovery_pending = account_id in self.recovery_removals_pending
            row = Gtk.ListBoxRow()
            content = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL,
                spacing=16,
            )
            set_margins(content, 14)

            info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            info.set_hexpand(True)
            name = Gtk.Label(label=account["display_name"], xalign=0)
            name.add_css_class("heading")
            info.append(name)
            address = Gtk.Label(
                label=f'{account["email"]} · {account["host"]}:{account["port"]}',
                xalign=0,
                ellipsize=Pango.EllipsizeMode.END,
            )
            address.add_css_class("dim-label")
            info.append(address)
            last_sync = account.get("last_sync_at") or _("Nunca sincronizada")
            details = Gtk.Label(
                label=_(
                    "{messages} mensagens · última sincronização: {last_sync}"
                ).format(
                    messages=f'{int(account["message_count"] or 0):,}'.replace(
                        ",", "."
                    ),
                    last_sync=last_sync,
                ),
                xalign=0,
                ellipsize=Pango.EllipsizeMode.END,
            )
            details.add_css_class("dim-label")
            info.append(details)
            content.append(info)

            actions = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL,
                spacing=8,
            )
            actions.set_halign(Gtk.Align.END)
            actions.set_valign(Gtk.Align.CENTER)
            actions.add_css_class("account-actions")

            lock = Gtk.Button(
                label=_("Bloquear") if unlocked else _("Desbloquear")
            )
            lock.add_css_class("portable-action")
            lock.set_tooltip_text(
                _("Bloquear esta conta e apagar as senhas da memória")
                if unlocked
                else _("Desbloquear esta conta com a senha local")
            )
            if not unlocked:
                lock.add_css_class("suggested-action")
            lock.connect(
                "clicked",
                lambda _button, item=account: self._toggle_account_lock(item),
            )
            actions.append(lock)

            extract = Gtk.Button(label=_("Sincronizar"))
            extract.add_css_class("portable-action")
            extract.set_tooltip_text(
                _("Sincronizar esta conta")
                if unlocked
                else _("Desbloqueie a conta para sincronizar")
            )
            if unlocked:
                extract.add_css_class("suggested-action")
            extract.set_sensitive(unlocked)
            extract.connect(
                "clicked",
                lambda _button, item=account: self._open_extraction(item),
            )
            actions.append(extract)
            edit = icon_only_button("imap-edit-symbolic")
            edit.add_css_class("compact-action")
            edit.set_tooltip_text(
                _("Editar as configurações da conta")
                if unlocked
                else _("Desbloqueie a conta para abrir as configurações")
            )
            edit.set_sensitive(unlocked)
            edit.connect(
                "clicked",
                lambda _button, item=account: self._show_edit_account_dialog(item),
            )
            actions.append(edit)
            export = icon_only_button("imap-results-symbolic")
            export.add_css_class("compact-action")
            export.set_tooltip_text(
                _("Abrir resultados e exportar os dados")
                if unlocked
                else _("Desbloqueie a conta para exportar")
            )
            export.set_sensitive(
                unlocked and bool(account["message_count"])
            )
            export.connect(
                "clicked",
                lambda _button, item=account: self._show_results(item),
            )
            actions.append(export)
            remove_icon = (
                "imap-delete-symbolic"
                if unlocked
                else "imap-admin-delete-symbolic"
            )
            remove = icon_only_button(remove_icon)
            remove.add_css_class("compact-action")
            remove.add_css_class("destructive-action")
            remove.set_tooltip_text(
                _("Aguardando autorização administrativa")
                if recovery_pending
                else (
                    _("Remover conta e dados locais")
                    if unlocked
                    else _(
                        "Remover esta conta com autorização administrativa"
                    )
                )
            )
            remove.set_sensitive(not recovery_pending)
            remove.connect(
                "clicked", lambda _button, item=account: self._confirm_remove(item)
            )
            actions.append(remove)
            content.append(actions)
            row.set_child(content)
            self.accounts_list.append(row)

    def _show_add_account_dialog(self, _button: Gtk.Button) -> None:
        dialog = AppDialog(
            transient_for=self,
            title=_("Adicionar conta IMAP"),
            default_width=560,
            default_height=590,
        )
        dialog.add_button(_("Cancelar"), Gtk.ResponseType.CANCEL)
        back_button = dialog.add_button(_("Voltar"), Gtk.ResponseType.REJECT)
        back_button.set_visible(False)
        primary_button = dialog.add_button(
            _("Continuar"), Gtk.ResponseType.ACCEPT
        )
        primary_button.add_css_class("suggested-action")
        dialog.set_default_response(Gtk.ResponseType.ACCEPT)

        wizard = Gtk.Stack()
        wizard.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        wizard.set_transition_duration(180)
        wizard.set_hexpand(True)
        wizard.set_vexpand(True)
        dialog.get_content_area().append(wizard)

        account_form = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
        )
        account_form.set_valign(Gtk.Align.START)
        set_margins(account_form, 16)
        account_step = Gtk.Label(
            label=_("Etapa 1 de 2 · Conta e servidor")
        )
        account_step.add_css_class("title-3")
        account_step.set_xalign(0)
        account_form.append(account_step)
        account_intro = Gtk.Label(
            label=_(
                "Informe o endereço da conta e os dados do servidor IMAP. "
                "Para Gmail, os valores padrão normalmente são suficientes."
            ),
            wrap=True,
            xalign=0,
        )
        account_intro.add_css_class("dim-label")
        account_form.append(account_intro)

        security_form = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
        )
        security_form.set_valign(Gtk.Align.START)
        set_margins(security_form, 16)
        security_step = Gtk.Label(label=_("Etapa 2 de 2 · Senhas"))
        security_step.add_css_class("title-3")
        security_step.set_xalign(0)
        security_form.append(security_step)
        security_intro = Gtk.Label(
            label=_(
                "Informe a senha IMAP ou a senha de aplicativo exigida pelo "
                "provedor. Depois, crie uma senha local diferente para "
                "criptografá-la. A senha local não será gravada."
            ),
            wrap=True,
            xalign=0,
        )
        security_intro.add_css_class("dim-label")
        security_form.append(security_intro)
        provider_password_hint = Gtk.Label(
            label="",
            wrap=True,
            xalign=0,
        )
        provider_password_hint.add_css_class("dim-label")
        security_form.append(provider_password_hint)

        fields: dict[str, Gtk.Entry] = {}

        def add_entry(
            container: Gtk.Box,
            key: str,
            label: str,
            value: str = "",
            secret: bool = False,
            tooltip: str = "",
        ) -> None:
            caption = Gtk.Label(label=_(label), xalign=0)
            container.append(caption)
            entry = Gtk.Entry()
            entry.set_text(value)
            entry.set_hexpand(True)
            if tooltip:
                entry.set_tooltip_text(_(tooltip))
            if secret:
                entry.set_visibility(False)
                entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
            fields[key] = entry
            container.append(entry)

        provider_label = Gtk.Label(label=_("Provedor"), xalign=0)
        account_form.append(provider_label)
        provider = Gtk.DropDown.new_from_strings(
            [_(item["name"]) for item in self.provider_presets]
        )
        provider.set_tooltip_text(
            _(
                "Escolha um provedor para preencher automaticamente o servidor "
                "e a porta, ou selecione Outro servidor IMAP."
            )
        )
        account_form.append(provider)
        add_entry(
            account_form,
            "display_name",
            "Nome para identificar a conta",
            tooltip="Nome usado para identificar esta conta no aplicativo",
        )
        add_entry(
            account_form,
            "email",
            "Endereço de e-mail",
            tooltip="Endereço usado para autenticação no servidor IMAP",
        )
        add_entry(
            account_form,
            "host",
            "Servidor IMAP",
            self.provider_presets[0]["host"],
            tooltip="Nome do servidor, por exemplo imap.gmail.com",
        )
        add_entry(
            account_form,
            "port",
            "Porta",
            str(self.provider_presets[0]["port"]),
            tooltip="Porta IMAP segura; normalmente 993",
        )
        add_entry(
            security_form,
            "imap_password",
            "Senha de app ou senha IMAP",
            secret=True,
            tooltip=(
                "Use a senha IMAP ou a senha específica de aplicativo indicada "
                "pelo provedor."
            ),
        )
        add_entry(
            security_form,
            "account_password",
            "Criar senha local para esta conta (mínimo de 8 caracteres)",
            secret=True,
            tooltip="Use no mínimo 8 caracteres. Esta senha não será armazenada.",
        )
        add_entry(
            security_form,
            "account_password_confirm",
            "Confirmar senha local",
            secret=True,
            tooltip="Repita a senha local criada acima",
        )
        show_security = Gtk.CheckButton(label=_("Mostrar senhas"))
        show_security.set_tooltip_text(
            _("Mostrar ou ocultar os campos de senha desta etapa")
        )
        show_security.connect(
            "toggled",
            lambda item: [
                fields[key].set_visibility(item.get_active())
                for key in (
                    "imap_password",
                    "account_password",
                    "account_password_confirm",
                )
            ],
        )
        security_form.append(show_security)

        wizard.add_named(account_form, "account")
        wizard.add_named(security_form, "security")
        wizard.set_visible_child_name("account")

        def selected_provider() -> dict[str, Any]:
            index = min(
                int(provider.get_selected()),
                len(self.provider_presets) - 1,
            )
            return self.provider_presets[index]

        def provider_changed(dropdown, _param):
            preset = selected_provider()
            fields["host"].set_text(str(preset["host"]))
            fields["port"].set_text(str(preset["port"]))
            provider_password_hint.set_text(_(preset["password_hint"]))

        provider.connect("notify::selected", provider_changed)
        provider_changed(provider, None)

        def account_values() -> tuple[dict[str, str], dict[str, Any]]:
            values = {
                key: fields[key].get_text().strip()
                for key in ("display_name", "email", "host", "port")
            }
            if (
                not values["display_name"]
                or not values["email"]
                or not values["host"]
            ):
                raise ValueError(
                    "Preencha o nome, o e-mail e o servidor IMAP."
                )
            port = int(values["port"])
            if not 1 <= port <= 65535:
                raise ValueError("Informe uma porta válida entre 1 e 65535.")
            preset = selected_provider()
            account = {
                "display_name": values["display_name"],
                "email": values["email"],
                "provider": preset["id"],
                "host": values["host"],
                "port": port,
                "security": preset["security"],
                "auth_type": "password",
            }
            return values, account

        def show_step(name: str) -> None:
            security = name == "security"
            wizard.set_visible_child_name(name)
            back_button.set_visible(security)
            primary_button.set_label(
                _("Testar e salvar") if security else _("Continuar")
            )
            if security:
                fields["imap_password"].grab_focus()
            else:
                fields["display_name"].grab_focus()

        def on_response(current_dialog: AppDialog, response: int) -> None:
            if response == Gtk.ResponseType.CANCEL:
                current_dialog.destroy()
                return
            if response == Gtk.ResponseType.REJECT:
                show_step("account")
                return
            if response != Gtk.ResponseType.ACCEPT:
                return
            if wizard.get_visible_child_name() == "account":
                try:
                    account_values()
                except ValueError as exc:
                    self._show_error(
                        "Revise os dados da conta",
                        str(exc),
                        current_dialog,
                    )
                    return
                show_step("security")
                return
            try:
                values, account = account_values()
                values.update(
                    {
                        key: fields[key].get_text().strip()
                        for key in (
                            "imap_password",
                            "account_password",
                            "account_password_confirm",
                        )
                    }
                )
                if not values["imap_password"]:
                    raise ValueError("Informe a senha de app ou senha IMAP.")
                if (
                    values["account_password"]
                    != values["account_password_confirm"]
                ):
                    raise ValueError("A confirmação da senha local não corresponde.")
                self.secret_cipher.validate_account_password(
                    values["account_password"]
                )
            except (ValueError, SecretError) as exc:
                self._show_error("Não foi possível salvar", str(exc), current_dialog)
                return
            primary_button.set_sensitive(False)
            back_button.set_sensitive(False)
            wizard.set_sensitive(False)

            def work() -> None:
                try:
                    encrypted = self.secret_cipher.encrypt(
                        values["imap_password"], values["account_password"]
                    )
                    self.extractor.test_connection(account, values["imap_password"])
                    account["encrypted_secret"] = encrypted
                    account_id = self.database.save_account(account)
                    GLib.idle_add(success, account_id)
                except Exception as exc:
                    GLib.idle_add(failure, str(exc))

            def success(_account_id: int) -> bool:
                current_dialog.destroy()
                self.refresh_accounts()
                return False

            def failure(detail: str) -> bool:
                primary_button.set_sensitive(True)
                back_button.set_sensitive(True)
                wizard.set_sensitive(True)
                self._show_error("Falha ao conectar", detail, current_dialog)
                return False

            threading.Thread(target=work, daemon=True).start()

        dialog.connect("response", on_response)
        dialog.present()

    def _show_edit_account_dialog(self, account: dict[str, Any]) -> None:
        session = self._require_account_unlocked(account)
        if session is None:
            return
        dialog = AppDialog(
            transient_for=self,
            title=_("Editar conta"),
            default_width=560,
        )
        change_local = dialog.add_start_button(_("Alterar senha local"))
        change_local.set_tooltip_text(
            _("Trocar a senha usada para desbloquear e criptografar esta conta")
        )
        change_local.connect(
            "clicked",
            lambda _button: self._show_change_local_password_dialog(
                account,
                dialog,
            ),
        )
        dialog.add_button(_("Cancelar"), Gtk.ResponseType.CANCEL)
        save_button = dialog.add_button(
            _("Testar e salvar alterações"),
            Gtk.ResponseType.ACCEPT,
        )
        save_button.add_css_class("suggested-action")
        save_button.set_tooltip_text(
            _(
                "Testar a conexão IMAP e salvar as configurações desta conta"
            )
        )

        form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        set_margins(form, 18)
        intro = Gtk.Label(
            label=_(
                "A conta já está desbloqueada. Informe uma nova senha IMAP "
                "somente se desejar substituí-la."
            ),
            wrap=True,
            xalign=0,
        )
        intro.add_css_class("dim-label")
        form.append(intro)
        fields: dict[str, Gtk.Entry] = {}

        def add_entry(
            key: str,
            label: str,
            value: str = "",
            secret: bool = False,
            tooltip: str = "",
        ) -> None:
            form.append(Gtk.Label(label=_(label), xalign=0))
            entry = Gtk.Entry()
            entry.set_text(value)
            entry.set_hexpand(True)
            if tooltip:
                entry.set_tooltip_text(_(tooltip))
            if secret:
                entry.set_visibility(False)
                entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
            fields[key] = entry
            form.append(entry)

        add_entry(
            "display_name",
            "Nome da conta",
            account["display_name"],
            tooltip="Nome usado para identificar esta conta no aplicativo",
        )
        add_entry(
            "email",
            "Endereço de e-mail",
            account["email"],
            tooltip="Endereço usado para autenticação no servidor IMAP",
        )
        add_entry(
            "host",
            "Servidor IMAP",
            account["host"],
            tooltip="Nome do servidor, por exemplo imap.gmail.com",
        )
        add_entry(
            "port",
            "Porta",
            str(account["port"]),
            tooltip="Porta IMAP segura; normalmente 993",
        )
        add_entry(
            "new_imap",
            "Nova senha IMAP (opcional)",
            secret=True,
            tooltip="Deixe vazio para manter a senha IMAP atual",
        )
        show_imap = Gtk.CheckButton(label=_("Mostrar senha"))
        show_imap.set_tooltip_text(_("Mostrar ou ocultar a nova senha IMAP"))
        show_imap.connect(
            "toggled",
            lambda item: fields["new_imap"].set_visibility(item.get_active()),
        )
        form.append(show_imap)
        dialog.get_content_area().append(form)

        def response(current_dialog: AppDialog, response_id: int) -> None:
            if response_id != Gtk.ResponseType.ACCEPT:
                current_dialog.destroy()
                return
            values = {key: entry.get_text().strip() for key, entry in fields.items()}
            try:
                if not values["display_name"] or not values["email"] or not values["host"]:
                    raise ValueError("Preencha o nome, o e-mail e o servidor IMAP.")
                port = int(values["port"])
                if not 1 <= port <= 65535:
                    raise ValueError("Informe uma porta válida entre 1 e 65535.")
            except ValueError as exc:
                self._show_error(
                    _("Não foi possível salvar"),
                    _(str(exc)),
                    current_dialog,
                )
                return
            save_button.set_sensitive(False)
            change_local.set_sensitive(False)
            for entry in fields.values():
                entry.set_sensitive(False)

            def work() -> None:
                try:
                    imap_password = (
                        values["new_imap"] or session["imap_password"]
                    )
                    encrypted = self.secret_cipher.encrypt(
                        imap_password,
                        session["local_password"],
                    )
                    updated = {
                        **account,
                        "id": account["id"],
                        "display_name": values["display_name"],
                        "email": values["email"],
                        "host": values["host"],
                        "port": port,
                    }
                    self.extractor.test_connection(updated, imap_password)
                    self.database.save_account(updated)
                    self.database.update_encrypted_secret(
                        account["id"], encrypted
                    )
                    GLib.idle_add(success, imap_password)
                except Exception as exc:
                    GLib.idle_add(failure, str(exc))

            def success(imap_password: str) -> bool:
                self.unlocked_accounts[int(account["id"])] = {
                    "imap_password": imap_password,
                    "local_password": session["local_password"],
                }
                current_dialog.destroy()
                self.refresh_accounts()
                return False

            def failure(detail: str) -> bool:
                save_button.set_sensitive(True)
                change_local.set_sensitive(True)
                for entry in fields.values():
                    entry.set_sensitive(True)
                self._show_error(
                    _("Não foi possível atualizar a conta"),
                    _(detail),
                    current_dialog,
                )
                return False

            threading.Thread(target=work, daemon=True).start()

        dialog.connect("response", response)
        dialog.present()

    def _show_change_local_password_dialog(
        self,
        account: dict[str, Any],
        parent: Gtk.Window,
    ) -> None:
        session = self._require_account_unlocked(account)
        if session is None:
            return
        dialog = AppDialog(
            transient_for=parent,
            title=_("Alterar senha local"),
            default_width=520,
        )
        dialog.add_button(_("Cancelar"), Gtk.ResponseType.CANCEL)
        save_button = dialog.add_button(
            _("Salvar nova senha"),
            Gtk.ResponseType.ACCEPT,
        )
        save_button.add_css_class("suggested-action")
        save_button.set_tooltip_text(
            _("Criptografar novamente a senha IMAP com a nova senha local")
        )
        dialog.set_default_response(Gtk.ResponseType.ACCEPT)

        form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        set_margins(form, 18)
        intro = Gtk.Label(
            label=_(
                "A nova senha será usada para desbloquear esta conta. Ela não "
                "será armazenada e não poderá ser recuperada."
            ),
            wrap=True,
            xalign=0,
        )
        intro.add_css_class("dim-label")
        form.append(intro)

        form.append(Gtk.Label(label=_("Nova senha local"), xalign=0))
        new_password = Gtk.Entry()
        new_password.set_visibility(False)
        new_password.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        new_password.set_activates_default(True)
        new_password.set_tooltip_text(
            _("Use no mínimo 8 caracteres. Esta senha não será armazenada.")
        )
        form.append(new_password)

        form.append(
            Gtk.Label(label=_("Confirmar nova senha local"), xalign=0)
        )
        confirmation = Gtk.Entry()
        confirmation.set_visibility(False)
        confirmation.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        confirmation.set_activates_default(True)
        confirmation.set_tooltip_text(_("Repita a nova senha local"))
        form.append(confirmation)
        show_local = Gtk.CheckButton(label=_("Mostrar senhas"))
        show_local.set_tooltip_text(
            _("Mostrar ou ocultar os campos da nova senha local")
        )
        show_local.connect(
            "toggled",
            lambda item: [
                entry.set_visibility(item.get_active())
                for entry in (new_password, confirmation)
            ],
        )
        form.append(show_local)
        dialog.get_content_area().append(form)

        def response(
            current_dialog: AppDialog,
            response_id: int,
        ) -> None:
            if response_id != Gtk.ResponseType.ACCEPT:
                current_dialog.destroy()
                return
            password = new_password.get_text()
            confirmed = confirmation.get_text()
            try:
                if password != confirmed:
                    raise ValueError(
                        "A confirmação da nova senha local não corresponde."
                    )
                self.secret_cipher.validate_account_password(password)
            except (ValueError, SecretError) as exc:
                self._show_error(
                    _("Não foi possível salvar"),
                    _(str(exc)),
                    current_dialog,
                )
                return

            save_button.set_sensitive(False)
            new_password.set_sensitive(False)
            confirmation.set_sensitive(False)

            def work() -> None:
                try:
                    encrypted = self.secret_cipher.encrypt(
                        session["imap_password"],
                        password,
                    )
                    self.database.update_encrypted_secret(
                        int(account["id"]),
                        encrypted,
                    )
                    GLib.idle_add(success)
                except Exception as exc:
                    GLib.idle_add(failure, str(exc))

            def success() -> bool:
                session["local_password"] = password
                current_dialog.destroy()
                self._show_notice(
                    _("Senha local alterada"),
                    _(
                        "A credencial IMAP foi criptografada novamente. Use a "
                        "nova senha local no próximo desbloqueio."
                    ),
                    parent,
                )
                return False

            def failure(detail: str) -> bool:
                save_button.set_sensitive(True)
                new_password.set_sensitive(True)
                confirmation.set_sensitive(True)
                self._show_error(
                    _("Não foi possível salvar"),
                    _(detail),
                    current_dialog,
                )
                return False

            threading.Thread(target=work, daemon=True).start()

        dialog.connect("response", response)
        dialog.present()
        new_password.grab_focus()

    def _account_session(
        self, account: dict[str, Any]
    ) -> dict[str, str] | None:
        return self.unlocked_accounts.get(int(account["id"]))

    def _require_account_unlocked(
        self, account: dict[str, Any]
    ) -> dict[str, str] | None:
        session = self._account_session(account)
        if session is None:
            self._show_error(
                "Conta bloqueada",
                "Use o botão de cadeado e digite a senha local antes de "
                "executar esta ação.",
            )
        return session

    def _toggle_account_lock(self, account: dict[str, Any]) -> None:
        account_id = int(account["id"])
        session = self.unlocked_accounts.pop(account_id, None)
        if session is None:
            self._unlock_account(account)
            return
        session.clear()
        if (
            self.active_account
            and int(self.active_account["id"]) == account_id
        ):
            self._clear_active_credentials()
        self.refresh_accounts()

    def _open_extraction(self, account: dict[str, Any]) -> None:
        session = self._require_account_unlocked(account)
        if session is None:
            return
        self.active_account = account
        self.active_imap_password = session["imap_password"]
        self._discover_folders()

    def _unlock_account(self, account: dict[str, Any]) -> None:
        dialog = AppDialog(
            transient_for=self,
            title=_("Desbloquear conta"),
            default_width=680,
        )
        dialog.add_button(_("Cancelar"), Gtk.ResponseType.CANCEL)
        unlock = dialog.add_button(_("Continuar"), Gtk.ResponseType.ACCEPT)
        unlock.add_css_class("suggested-action")
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        set_margins(content, 18)
        label = Gtk.Label(
            label=_(
                "Digite a senha local de “{account}”. As senhas ficarão "
                "somente na memória enquanto a conta estiver desbloqueada."
            ).format(account=account["display_name"]),
            wrap=True,
            xalign=0,
        )
        content.append(label)
        password = Gtk.Entry()
        password.set_visibility(False)
        password.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        password.set_activates_default(True)
        content.append(password)
        show_password = Gtk.CheckButton(label=_("Mostrar senha"))
        show_password.set_tooltip_text(
            _("Mostrar ou ocultar a senha local digitada")
        )
        show_password.connect(
            "toggled",
            lambda item: password.set_visibility(item.get_active()),
        )
        content.append(show_password)
        dialog.get_content_area().append(content)
        dialog.set_default_response(Gtk.ResponseType.ACCEPT)

        def response(current_dialog: AppDialog, response_id: int) -> None:
            if response_id != Gtk.ResponseType.ACCEPT:
                current_dialog.destroy()
                return
            unlock.set_sensitive(False)
            password.set_sensitive(False)
            local_password = password.get_text()

            def work() -> None:
                try:
                    imap_password = self.secret_cipher.decrypt(
                        account["encrypted_secret"], local_password
                    )
                    GLib.idle_add(
                        success, imap_password, local_password
                    )
                except Exception as exc:
                    GLib.idle_add(failure, str(exc))

            def success(
                imap_password: str, unlocked_local_password: str
            ) -> bool:
                current_dialog.destroy()
                self.unlocked_accounts[int(account["id"])] = {
                    "imap_password": imap_password,
                    "local_password": unlocked_local_password,
                }
                self.refresh_accounts()
                return False

            def failure(detail: str) -> bool:
                unlock.set_sensitive(True)
                password.set_sensitive(True)
                password.set_text("")
                self._show_error("Não foi possível desbloquear", detail, current_dialog)
                return False

            threading.Thread(target=work, daemon=True).start()

        dialog.connect("response", response)
        dialog.present()
        password.grab_focus()

    def _reload_folders(self, _button: Gtk.Button) -> None:
        self._discover_folders(is_reload=True)

    def _discover_folders(
        self,
        is_reload: bool = False,
        is_rebuild: bool = False,
    ) -> None:
        if not self.active_account or not self.active_imap_password:
            return
        self._show_page("folders")
        if is_reload and self.folder_checks:
            selected_ids = [
                mailbox_id
                for mailbox_id, check in self.folder_checks.items()
                if check.get_active()
            ]
            self.database.set_mailbox_selection(
                self.active_account["id"], selected_ids
            )
        else:
            clear_box(self.folders_list)
            self.folder_checks.clear()
        self.folder_spinner.start()
        if is_rebuild:
            self.folder_loading_status.set_text(
                _(
                    "Índice local removido. Atualizando as pastas antes da "
                    "nova sincronização…"
                )
            )
        else:
            self.folder_loading_status.set_text(
                _(
                    "Atualizando pastas e quantidades no servidor…"
                    if is_reload
                    else "Conectando e consultando as pastas do servidor…"
                )
            )
        self.folder_content_stack.set_visible_child_name("loading")
        self.start_button.set_sensitive(False)
        self.reload_folders_button.set_sensitive(False)
        self.folders_heading.set_text(
            _("Pastas de {account}").format(
                account=self.active_account["display_name"]
            )
        )

        account = self.active_account
        imap_password = self.active_imap_password

        def progress(event: dict[str, Any]) -> None:
            GLib.idle_add(
                self.folder_loading_status.set_text,
                event.get("text", _("Consultando pastas…")),
            )

        def work() -> None:
            try:
                folders = self.extractor.discover_mailboxes(
                    account, imap_password, progress
                )
                stored = self.database.replace_mailboxes(account["id"], folders)
                GLib.idle_add(success, stored)
            except Exception as exc:
                GLib.idle_add(failure, str(exc))

        def success(folders: list[dict[str, Any]]) -> bool:
            self.folder_spinner.stop()
            if is_rebuild:
                self.folder_info.set_text(
                    _(
                        "Índice local pronto para ser reconstruído. Confira "
                        "as pastas e clique em “Sincronizar agora” para baixar "
                        "novamente todos os metadados."
                    )
                )
            else:
                self.folder_info.set_text(
                    _(
                        (
                            "Pastas e quantidades atualizadas. "
                            if is_reload
                            else ""
                        )
                        + "Selecione “Todos os e-mails” para recebidos, "
                        "enviados e arquivados. Spam e Lixeira precisam ser "
                        "marcados separadamente."
                    )
                )
            self._populate_folders(folders)
            self.folder_content_stack.set_visible_child_name("folders")
            self.start_button.set_sensitive(True)
            self.reload_folders_button.set_sensitive(True)
            return False

        def failure(detail: str) -> bool:
            self.folder_spinner.stop()
            self.start_button.set_sensitive(bool(self.folder_checks))
            self.reload_folders_button.set_sensitive(True)
            print(
                _("Falha ao consultar as pastas: {detail}").format(
                    detail=detail
                ),
                file=sys.stderr,
            )
            self._show_error("Falha ao consultar as pastas", detail)
            if is_reload:
                self.folder_content_stack.set_visible_child_name("folders")
                self.folder_info.set_text(
                    _(
                        "Não foi possível atualizar. As quantidades anteriores "
                        "continuam exibidas."
                    )
                )
                return False
            self._clear_active_credentials()
            self._show_page("accounts")
            return False

        threading.Thread(target=work, daemon=True).start()

    def _populate_folders(self, folders: list[dict[str, Any]]) -> None:
        clear_box(self.folders_list)
        self.folder_checks.clear()
        for folder in folders:
            row = Gtk.ListBoxRow()
            content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            set_margins(content, 10)
            check = Gtk.CheckButton()
            check.set_active(bool(folder.get("selected")))
            check.connect("toggled", self._folder_toggled, folder)
            self.folder_checks[int(folder["id"])] = check
            content.append(check)
            labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            labels.set_hexpand(True)
            name = Gtk.Label(label=folder["remote_name"], xalign=0)
            name.add_css_class("heading")
            labels.append(name)
            special = folder.get("special_use") or _("Pasta comum")
            count = folder.get("messages_count")
            detail = (
                _("{special} · {count} mensagens").format(
                    special=special,
                    count=f"{int(count):,}".replace(",", "."),
                )
                if count is not None
                else _("{special} · quantidade indisponível").format(
                    special=special
                )
            )
            secondary = Gtk.Label(label=detail, xalign=0)
            secondary.add_css_class("dim-label")
            labels.append(secondary)
            content.append(labels)
            row.set_child(content)
            self.folders_list.append(row)

    def _folder_toggled(
        self, check: Gtk.CheckButton, folder: dict[str, Any]
    ) -> None:
        if not check.get_active():
            return
        special = (folder.get("special_use") or "").lower()
        if special == "\\all":
            for mailbox_id, other in self.folder_checks.items():
                if mailbox_id != int(folder["id"]):
                    other.set_active(False)
            return
        for item in self.database.get_mailboxes(self.active_account["id"]):
            if (item.get("special_use") or "").lower() == "\\all":
                all_check = self.folder_checks.get(int(item["id"]))
                if all_check:
                    all_check.set_active(False)

    def _toggle_progress_details(
        self,
        button: Gtk.ToggleButton,
    ) -> None:
        expanded = button.get_active()
        self.progress_log_revealer.set_reveal_child(expanded)
        self.copy_progress_log_button.set_visible(expanded)
        button.set_label(
            _("Ocultar detalhes") if expanded else _("Mostrar detalhes")
        )
        if expanded:
            buffer = self.progress_log.get_buffer()
            self.progress_log.scroll_to_iter(
                buffer.get_end_iter(),
                0.0,
                False,
                0.0,
                1.0,
            )

    def _copy_progress_log(self, button: Gtk.Button) -> None:
        buffer = self.progress_log.get_buffer()
        start, end = buffer.get_bounds()
        text = buffer.get_text(start, end, False)
        display = Gdk.Display.get_default()
        if display is None or not text:
            return
        display.get_clipboard().set(text)
        button.set_label(_("Copiado"))

        def restore_label() -> bool:
            if button.get_visible():
                button.set_label(_("Copiar log"))
            return False

        GLib.timeout_add(1400, restore_label)

    def _start_sync(self, _button: Gtk.Button) -> None:
        if not self.active_account or not self.active_imap_password:
            return
        selected_ids = [
            mailbox_id
            for mailbox_id, check in self.folder_checks.items()
            if check.get_active()
        ]
        if not selected_ids:
            self._show_error("Nenhuma pasta selecionada", "Selecione pelo menos uma pasta.")
            return
        self.database.set_mailbox_selection(self.active_account["id"], selected_ids)
        mailboxes = self.database.get_mailboxes(
            self.active_account["id"], selected_only=True
        )
        self.cancel_event.clear()
        self.pause_event.clear()
        self.is_paused = False
        self.pause_button.set_label(_("Pausar"))
        self.pause_button.set_sensitive(True)
        self.cancel_button.set_sensitive(True)
        self.progress_bar.set_fraction(0)
        self.progress_bar.set_text(_("Preparando"))
        self.progress_count.set_text(_("0 de 0"))
        self.progress_speed.set_text(_("0 mensagens/s"))
        self.progress_changes.set_text(
            _("0 novas · 0 ausentes · 0 erros")
        )
        self.current_sync_missing = 0
        self.progress_details_button.set_active(False)
        self.copy_progress_log_button.set_label(_("Copiar log"))
        self.progress_log.get_buffer().set_text("")
        account = self.active_account
        imap_password = self.active_imap_password
        self._append_log(
            _(
                "Sincronização iniciada para {account} · "
                "{folders} pasta(s) monitorada(s)."
            ).format(
                account=account["email"],
                folders=len(mailboxes),
            )
        )
        self._show_page("progress")

        def progress(event: dict[str, Any]) -> None:
            GLib.idle_add(self._handle_progress, event)

        def work() -> None:
            try:
                result = self.extractor.sync(
                    account,
                    imap_password,
                    mailboxes,
                    progress,
                    self.cancel_event,
                    self.pause_event,
                )
                GLib.idle_add(success, result)
            except Exception as exc:
                GLib.idle_add(failure, str(exc))

        def success(result: dict[str, Any]) -> bool:
            self.pause_button.set_sensitive(False)
            self.cancel_button.set_sensitive(False)
            if result["status"] == "cancelled":
                self.progress_phase.set_text(
                    _("Sincronização cancelada com segurança")
                )
                self._append_log(_("O estado confirmado anteriormente foi preservado."))
                self._clear_active_credentials()
                self.refresh_accounts()
            else:
                self.progress_bar.set_fraction(1)
                self.progress_bar.set_text(_("Concluído"))
                self._append_log(
                    _(
                        "Sincronização concluída: {checked} comparada(s) · "
                        "{inserted} nova(s) · {missing} ausente(s) · "
                        "{restored} restaurada(s) · {errors} erro(s)."
                    ).format(
                        checked=result.get("checked", 0),
                        inserted=result.get("inserted", 0),
                        missing=result.get("missing", 0),
                        restored=result.get("restored", 0),
                        errors=result.get("errors", 0),
                    )
                )
                self._show_results(account)
            return False

        def failure(detail: str) -> bool:
            self.pause_button.set_sensitive(False)
            self.cancel_button.set_sensitive(False)
            self._show_error("A sincronização foi interrompida", detail)
            self._append_log(_("ERROR: {detail}").format(detail=detail))
            self._clear_active_credentials()
            return False

        self.current_thread = threading.Thread(target=work, daemon=True)
        self.current_thread.start()

    def _handle_progress(self, event: dict[str, Any]) -> bool:
        kind = event.get("type")
        if kind in {"phase", "planned"}:
            self.progress_phase.set_text(
                event.get("text", _("Processando…"))
            )
            self._append_log(event.get("text", ""))
        elif kind == "capabilities":
            features = []
            if event.get("qresync"):
                features.append("QRESYNC")
            if event.get("condstore"):
                features.append("CONDSTORE")
            if event.get("gmail"):
                features.append("X-GM-EXT-1")
            self._append_log(
                _("Recursos detectados: {features}.").format(
                    features=", ".join(features) if features else _("IMAP básico")
                )
            )
        elif kind == "mailbox_plan":
            self._append_log(
                _(
                    "Comparação de “{mailbox}”: {messages} no servidor · "
                    "{new_messages} nova(s) · {missing} possível(is) ausente(s) · "
                    "UIDVALIDITY {uidvalidity}."
                ).format(
                    mailbox=event.get("mailbox", ""),
                    messages=event.get("messages", 0),
                    new_messages=event.get("new_messages", 0),
                    missing=event.get("missing_candidates", 0),
                    uidvalidity=event.get("uidvalidity") or "-",
                )
            )
        elif kind == "batch":
            self.progress_phase.set_text(
                _("Baixando novos cabeçalhos de “{mailbox}”…").format(
                    mailbox=event.get("mailbox", "")
                )
            )
            self._append_log(
                _(
                    "Iniciando lote {batch}/{batches} de “{mailbox}”: "
                    "{amount} mensagem(ns) · UIDs {first}–{last}."
                ).format(
                    batch=event.get("batch_number", 0),
                    batches=event.get("mailbox_batches", 0),
                    mailbox=event.get("mailbox", ""),
                    amount=event.get("batch_amount", 0),
                    first=event.get("uid_first", "-"),
                    last=event.get("uid_last", "-"),
                )
            )
        elif kind == "progress":
            processed = int(event.get("processed", 0))
            total = int(event.get("total", 0))
            fraction = processed / total if total else 0
            self.progress_bar.set_fraction(max(0, min(fraction, 1)))
            self.progress_bar.set_text(f"{fraction * 100:.1f}%")
            self.progress_count.set_text(
                f"{processed:,} de {total:,}".replace(",", ".")
            )
            self.progress_speed.set_text(
                _("{rate:.1f} mensagens/s").format(
                    rate=float(event.get("rate", 0))
                )
            )
            self.progress_changes.set_text(
                _("{new} novas · {missing} ausentes · {errors} erros").format(
                    new=f'{int(event.get("inserted", 0)):,}'.replace(",", "."),
                    missing=f"{self.current_sync_missing:,}".replace(",", "."),
                    errors=f'{int(event.get("errors", 0)):,}'.replace(",", "."),
                )
            )
            self._append_log(
                _(
                    "Lote {batch}/{batches} concluído: {parsed} cabeçalho(s) "
                    "interpretado(s) · {inserted} novo(s) · {updated} "
                    "atualizado(s) · {errors} erro(s) · total "
                    "{processed}/{total} · {rate:.1f} mensagem(ns)/s."
                ).format(
                    batch=event.get("batch_number", 0),
                    batches=event.get("mailbox_batches", 0),
                    parsed=event.get("records_parsed", 0),
                    inserted=event.get("batch_inserted", 0),
                    updated=event.get("batch_updated", 0),
                    errors=event.get("batch_errors", 0),
                    processed=processed,
                    total=total,
                    rate=float(event.get("rate", 0)),
                )
            )
        elif kind == "reconciled":
            self.current_sync_missing += int(event.get("missing", 0))
            self.progress_changes.set_text(
                _("{new} novas · {missing} ausentes · {errors} erros").format(
                    new=f'{int(event.get("inserted", 0)):,}'.replace(",", "."),
                    missing=f"{self.current_sync_missing:,}".replace(",", "."),
                    errors=f'{int(event.get("errors", 0)):,}'.replace(",", "."),
                )
            )
            self._append_log(
                _(
                    "Estado de “{mailbox}” atualizado: {current} presente(s) · "
                    "{missing} ausente(s) · {restored} restaurada(s)."
                ).format(
                    mailbox=event.get("mailbox", ""),
                    current=event.get("current", 0),
                    missing=event.get("missing", 0),
                    restored=event.get("restored", 0),
                )
            )
        return False

    def _append_log(self, text: str) -> None:
        if not text:
            return
        buffer = self.progress_log.get_buffer()
        timestamp = datetime.now().strftime("%H:%M:%S")
        buffer.insert(buffer.get_end_iter(), f"[{timestamp}] {text}\n")
        if self.progress_log_revealer.get_reveal_child():
            self.progress_log.scroll_to_iter(
                buffer.get_end_iter(),
                0.0,
                False,
                0.0,
                1.0,
            )

    def _toggle_pause(self, _button: Gtk.Button) -> None:
        self.is_paused = not self.is_paused
        if self.is_paused:
            self.pause_event.set()
            self.pause_button.set_label(_("Continuar"))
            self.progress_phase.set_text(_("Pausada após o último lote"))
            self._append_log(_("Pausa solicitada; aguardando o lote atual."))
        else:
            self.pause_event.clear()
            self.pause_button.set_label(_("Pausar"))
            self.progress_phase.set_text(_("Continuando…"))
            self._append_log(_("Sincronização retomada."))

    def _cancel_sync(self, _button: Gtk.Button) -> None:
        self.cancel_event.set()
        self.pause_event.clear()
        self.pause_button.set_sensitive(False)
        self.cancel_button.set_sensitive(False)
        self.progress_phase.set_text(_("Cancelando após o lote atual…"))
        self._append_log(
            _("Cancelamento solicitado; aguardando o lote atual.")
        )

    def _show_results(self, account: dict[str, Any]) -> None:
        if self._require_account_unlocked(account) is None:
            return
        self.active_account = account
        self.selected_cleanup_senders.clear()
        self.selected_cleanup_domains.clear()
        self.selected_large_messages.clear()
        self.sender_rows = []
        self.domain_rows = []
        self.largest_rows = []
        self.subject_preview_cache.clear()
        self.sender_search.set_text("")
        self.domain_search.set_text("")
        self.largest_search.set_text("")
        self.largest_mode.set_selected(0)
        self._reset_largest_extension_filter()
        self.largest_size.set_selected(0)
        self._render_sender_rows()
        self._render_domain_rows()
        self._render_largest_rows()
        self._update_largest_preview()
        self.results_view_stack.set_visible_child_name("summary")
        self.results_view_stack.set_sensitive(True)
        self.back_button.set_sensitive(True)
        for button in (
            *self.cleanup_buttons,
            *self.selected_csv_buttons,
            *self.selected_ods_buttons,
        ):
            button.set_sensitive(False)
        for label in self.cleanup_selection_labels:
            label.set_text(_("Nenhuma mensagem selecionada"))
        self._refresh_results_summary()
        self.export_status.set_text("")
        self.export_status.set_visible(False)
        self._clear_active_credentials(keep_account=True)
        self._show_page("results")
        self.refresh_accounts()
        self._load_result_rankings(int(account["id"]))
        self._reload_largest_rows()

    def _refresh_results_summary(self) -> None:
        if not self.active_account:
            return
        account = self.active_account
        summary = self.database.account_summary(account["id"])
        has_messages = int(summary["messages"] or 0) > 0
        self.csv_button.set_sensitive(has_messages)
        self.ods_button.set_sensitive(has_messages)
        account_title = f'{account["display_name"]} · {account["email"]}'
        self.results_account.set_text(account_title)
        self.results_account.set_tooltip_text(account_title)
        self.summary_messages_value.set_text(
            f'{int(summary["messages"] or 0):,}'.replace(",", ".")
        )
        self.summary_senders_value.set_text(
            f'{int(summary["senders"] or 0):,}'.replace(",", ".")
        )
        self.summary_domains_value.set_text(
            f'{int(summary["domains"] or 0):,}'.replace(",", ".")
        )
        self.summary_volume_value.set_text(
            human_size(summary.get("total_size"))
        )
        first_date = summary.get("first_date") or _("Não identificado")
        last_date = summary.get("last_date") or _("Não identificado")
        self.summary_period_label.set_text(
            _("Período das mensagens: {first} até {last}").format(
                first=first_date,
                last=last_date,
            )
        )
        self.summary_errors_label.set_text(
            _("{errors} erros registrados").format(
                errors=f'{int(summary["errors"] or 0):,}'.replace(",", ".")
            )
        )

    def _load_result_rankings(self, account_id: int) -> None:
        def work() -> None:
            try:
                senders = self.database.sender_summary(account_id, limit=500)
                domains = self.database.cleanup_domain_summary(
                    account_id, limit=500
                )
                GLib.idle_add(success, senders, domains)
            except Exception as exc:
                GLib.idle_add(failure, str(exc))

        def success(
            senders: list[dict[str, Any]],
            domains: list[dict[str, Any]],
        ) -> bool:
            if (
                not self.active_account
                or int(self.active_account["id"]) != account_id
            ):
                return False
            self.sender_rows = senders
            self.domain_rows = domains
            self.subject_preview_cache.clear()
            self._render_sender_rows()
            self._render_domain_rows()
            return False

        def failure(detail: str) -> bool:
            self.export_status.set_visible(True)
            self.export_status.set_text(
                _(
                    "CSV e ODS continuam disponíveis para todas as mensagens. "
                    "Não foi possível montar os rankings: {detail}"
                ).format(detail=detail)
            )
            return False

        threading.Thread(target=work, daemon=True).start()

    def _confirm_cleanup(self, _button: Gtk.Button) -> None:
        if not self.active_account:
            return
        account = self.active_account
        session = self._require_account_unlocked(account)
        if session is None:
            return
        account_id = int(account["id"])
        targets = self.database.cleanup_targets(
            account_id,
            sorted(self.selected_cleanup_senders),
            sorted(self.selected_cleanup_domains),
        )
        if not targets:
            self._show_error(
                "Nenhuma mensagem localizável",
                "As mensagens selecionadas ainda não possuem um UID de pasta "
                "atualizado. Sincronize novamente a conta e tente outra vez.",
            )
            return
        trash_mailbox = self.database.get_trash_mailbox(account_id)
        if not trash_mailbox:
            self._show_error(
                "Lixeira não identificada",
                "O servidor ainda não informou qual pasta é a Lixeira. Volte à "
                "tela de pastas, use Recarregar e tente novamente.",
            )
            return

        preview = self.database.cleanup_preview(
            account_id,
            sorted(self.selected_cleanup_senders),
            sorted(self.selected_cleanup_domains),
        )
        self._present_cleanup_confirmation(
            account,
            session["imap_password"],
            targets,
            trash_mailbox,
            preview,
        )

    def _confirm_large_cleanup(self, _button: Gtk.Button) -> None:
        if not self.active_account or not self.selected_large_messages:
            return
        account = self.active_account
        session = self._require_account_unlocked(account)
        if session is None:
            return
        account_id = int(account["id"])
        message_ids = sorted(self.selected_large_messages)
        targets = self.database.message_cleanup_targets(
            account_id,
            message_ids,
        )
        if not targets:
            self._show_error(
                _("Nenhuma mensagem localizável"),
                _(
                    "As mensagens selecionadas ainda não possuem um UID de "
                    "pasta atualizado. Sincronize novamente a conta e tente "
                    "outra vez."
                ),
            )
            return
        trash_mailbox = self.database.get_trash_mailbox(account_id)
        if not trash_mailbox:
            self._show_error(
                _("Lixeira não identificada"),
                _(
                    "O servidor ainda não informou qual pasta é a Lixeira. "
                    "Volte à tela de pastas, use Recarregar e tente novamente."
                ),
            )
            return
        preview = self.database.message_cleanup_preview(
            account_id,
            message_ids,
        )
        self._present_cleanup_confirmation(
            account,
            session["imap_password"],
            targets,
            trash_mailbox,
            preview,
        )

    def _present_cleanup_confirmation(
        self,
        account: dict[str, Any],
        imap_password: str,
        targets: list[dict[str, Any]],
        trash_mailbox: dict[str, Any],
        preview: dict[str, int],
    ) -> None:
        mapped = len(targets)
        mapped_size = sum(int(item.get("size_bytes") or 0) for item in targets)
        skipped = max(0, int(preview["messages"]) - mapped)

        dialog = AppDialog(
            transient_for=self,
            title=_("Confirmar limpeza"),
            default_width=620,
        )
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        set_margins(content, 20)
        heading = Gtk.Label(
            label=_("Mover {messages} mensagens para a Lixeira?").format(
                messages=f"{mapped:,}".replace(",", ".")
            ),
            wrap=True,
            xalign=0,
        )
        heading.add_css_class("title-3")
        content.append(heading)
        detail_text = _(
            "Volume original associado: {size}.\nDestino: “{destination}”.\n\n"
            "O aplicativo não esvaziará a Lixeira. Você poderá revisar ou "
            "restaurar as mensagens pelo webmail."
        ).format(
            size=human_size(mapped_size),
            destination=trash_mailbox["remote_name"],
        )
        if skipped:
            detail_text += _(
                "\n\n{messages} mensagens sem um UID atual serão ignoradas."
            ).format(messages=f"{skipped:,}".replace(",", "."))
        detail = Gtk.Label(label=detail_text, wrap=True, xalign=0)
        detail.add_css_class("dim-label")
        content.append(detail)
        reviewed = Gtk.CheckButton(
            label=_("Revisei a quantidade e confirmo esta seleção.")
        )
        content.append(reviewed)
        dialog.get_content_area().append(content)
        dialog.add_button(_("Cancelar"), Gtk.ResponseType.CANCEL)
        move = dialog.add_button(
            _("Mover para a Lixeira"), Gtk.ResponseType.ACCEPT
        )
        move.add_css_class("destructive-action")
        move.set_sensitive(False)
        reviewed.connect(
            "toggled",
            lambda item: move.set_sensitive(item.get_active()),
        )
        dialog.set_default_response(Gtk.ResponseType.CANCEL)

        def response(current_dialog: AppDialog, response_id: int) -> None:
            if response_id == Gtk.ResponseType.ACCEPT:
                current_dialog.destroy()
                self._run_cleanup(
                    account,
                    imap_password,
                    targets,
                    trash_mailbox,
                )
                return
            current_dialog.destroy()

        dialog.connect("response", response)
        dialog.present()

    def _build_cleanup_progress_dialog(self, total: int) -> None:
        if self.cleanup_dialog is not None:
            self.cleanup_dialog.destroy()

        dialog = AppDialog(
            transient_for=self,
            title=_("Movendo para a Lixeira"),
            default_width=700,
        )
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        set_margins(content, 20)

        self.cleanup_phase = Gtk.Label(
            label=_("Conectando ao servidor…"),
            wrap=True,
            xalign=0,
        )
        self.cleanup_phase.add_css_class("title-3")
        content.append(self.cleanup_phase)

        self.cleanup_progress_bar = Gtk.ProgressBar()
        self.cleanup_progress_bar.set_show_text(True)
        self.cleanup_progress_bar.set_text("0%")
        content.append(self.cleanup_progress_bar)

        self.cleanup_count_label = Gtk.Label(
            label=_("{moved} de {total} mensagens movidas").format(
                moved="0",
                total=f"{total:,}".replace(",", "."),
            ),
            xalign=0,
        )
        self.cleanup_count_label.add_css_class("dim-label")
        content.append(self.cleanup_count_label)
        self.cleanup_recovery_note = Gtk.Label(
            label="",
            wrap=True,
            xalign=0,
        )
        self.cleanup_recovery_note.add_css_class("dim-label")
        self.cleanup_recovery_note.set_visible(False)
        content.append(self.cleanup_recovery_note)

        details_bar = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        self.cleanup_details_button = Gtk.ToggleButton(
            label=_("Mostrar detalhes")
        )
        self.cleanup_details_button.set_tooltip_text(
            _("Expandir ou recolher o log técnico da limpeza")
        )
        self.cleanup_details_button.connect(
            "toggled",
            self._toggle_cleanup_details,
        )
        details_bar.append(self.cleanup_details_button)
        self.copy_cleanup_log_button = Gtk.Button(label=_("Copiar log"))
        self.copy_cleanup_log_button.set_visible(False)
        self.copy_cleanup_log_button.set_tooltip_text(
            _("Copiar o log técnico para a área de transferência")
        )
        self.copy_cleanup_log_button.connect(
            "clicked",
            self._copy_cleanup_log,
        )
        details_bar.append(self.copy_cleanup_log_button)
        content.append(details_bar)

        log_scroller = Gtk.ScrolledWindow()
        log_scroller.set_policy(
            Gtk.PolicyType.AUTOMATIC,
            Gtk.PolicyType.AUTOMATIC,
        )
        log_scroller.set_min_content_height(220)
        log_scroller.set_vexpand(True)
        self.cleanup_log = Gtk.TextView()
        self.cleanup_log.set_editable(False)
        self.cleanup_log.set_cursor_visible(False)
        self.cleanup_log.set_monospace(True)
        self.cleanup_log.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.cleanup_log.add_css_class("log-view")
        log_scroller.set_child(self.cleanup_log)
        self.cleanup_log_revealer = Gtk.Revealer()
        self.cleanup_log_revealer.set_transition_type(
            Gtk.RevealerTransitionType.SLIDE_DOWN
        )
        self.cleanup_log_revealer.set_transition_duration(180)
        self.cleanup_log_revealer.set_reveal_child(False)
        self.cleanup_log_revealer.set_vexpand(True)
        self.cleanup_log_revealer.set_child(
            rounded_scroll_frame(log_scroller)
        )
        content.append(self.cleanup_log_revealer)

        dialog.get_content_area().append(content)
        self.cleanup_pause_button = dialog.add_start_button(_("Pausar"))
        self.cleanup_pause_button.set_tooltip_text(
            _("Pausar com segurança após concluir o lote atual")
        )
        self.cleanup_pause_button.connect(
            "clicked",
            self._toggle_cleanup_pause,
        )
        self.cleanup_undo_button = dialog.add_start_button(
            _("Reverter movimentação")
        )
        self.cleanup_undo_button.add_css_class("suggested-action")
        self.cleanup_undo_button.set_tooltip_text(
            _("Restaurar com segurança os lotes movidos nesta operação")
        )
        self.cleanup_undo_button.set_visible(False)
        self.cleanup_undo_button.connect(
            "clicked",
            self._start_cleanup_undo,
        )
        self.cleanup_cancel_button = dialog.add_button(
            _("Interromper"),
            Gtk.ResponseType.CANCEL,
        )
        self.cleanup_cancel_button.set_tooltip_text(
            _("Interromper com segurança após concluir o lote atual")
        )
        dialog.connect("response", self._cleanup_dialog_response)

        self.cleanup_dialog = dialog
        self.cleanup_operation_active = True
        self.cleanup_mode = "move"
        self.cleanup_undo_items = []
        self.cleanup_undo_available_count = 0
        self.cleanup_moved_count = 0
        self.cleanup_undo_account = None
        self.cleanup_undo_password = None
        self.cleanup_undo_trash = None
        self.cleanup_reconciliation_error = None
        self.cleanup_cancel_event.clear()
        self.cleanup_pause_event.clear()
        self.cleanup_is_paused = False
        self._append_cleanup_log(
            _("Preparando a conexão e validando a Lixeira do servidor.")
        )
        dialog.present_with_focus(self.cleanup_pause_button)

    def _cleanup_dialog_response(
        self,
        dialog: AppDialog,
        _response_id: int,
    ) -> None:
        if self.cleanup_operation_active:
            self._cancel_cleanup()
            return
        dialog.destroy()
        if self.cleanup_dialog is dialog:
            self.cleanup_dialog = None
        self.cleanup_undo_items = []
        self.cleanup_undo_available_count = 0
        self.cleanup_moved_count = 0
        self.cleanup_undo_account = None
        self.cleanup_undo_password = None
        self.cleanup_undo_trash = None
        self.cleanup_reconciliation_error = None

    def _toggle_cleanup_details(
        self,
        button: Gtk.ToggleButton,
    ) -> None:
        expanded = button.get_active()
        self.cleanup_log_revealer.set_reveal_child(expanded)
        self.copy_cleanup_log_button.set_visible(expanded)
        button.set_label(
            _("Ocultar detalhes") if expanded else _("Mostrar detalhes")
        )
        if expanded:
            buffer = self.cleanup_log.get_buffer()
            self.cleanup_log.scroll_to_iter(
                buffer.get_end_iter(),
                0.0,
                False,
                0.0,
                1.0,
            )

    def _copy_cleanup_log(self, button: Gtk.Button) -> None:
        buffer = self.cleanup_log.get_buffer()
        start, end = buffer.get_bounds()
        text = buffer.get_text(start, end, False)
        display = Gdk.Display.get_default()
        if display is None or not text:
            return
        display.get_clipboard().set(text)
        button.set_label(_("Copiado"))

        def restore_label() -> bool:
            if button.get_visible():
                button.set_label(_("Copiar log"))
            return False

        GLib.timeout_add(1400, restore_label)

    def _append_cleanup_log(self, text: str) -> None:
        if not text or not hasattr(self, "cleanup_log"):
            return
        buffer = self.cleanup_log.get_buffer()
        timestamp = datetime.now().strftime("%H:%M:%S")
        buffer.insert(buffer.get_end_iter(), f"[{timestamp}] {text}\n")
        if self.cleanup_log_revealer.get_reveal_child():
            self.cleanup_log.scroll_to_iter(
                buffer.get_end_iter(),
                0.0,
                False,
                0.0,
                1.0,
            )

    def _toggle_cleanup_pause(self, _button: Gtk.Button) -> None:
        if not self.cleanup_operation_active:
            return
        self.cleanup_is_paused = not self.cleanup_is_paused
        if self.cleanup_is_paused:
            self.cleanup_pause_event.set()
            self.cleanup_pause_button.set_label(_("Continuar"))
            self.cleanup_phase.set_text(
                _("Pausa solicitada; aguardando o lote atual…")
            )
            self._append_cleanup_log(
                _("Pausa solicitada; aguardando o lote atual.")
            )
        else:
            self.cleanup_pause_event.clear()
            self.cleanup_pause_button.set_label(_("Pausar"))
            self.cleanup_phase.set_text(_("Continuando a limpeza…"))
            self._append_cleanup_log(_("Limpeza retomada."))

    def _cancel_cleanup(self) -> None:
        if not self.cleanup_operation_active:
            return
        self.cleanup_cancel_event.set()
        self.cleanup_pause_event.clear()
        self.cleanup_pause_button.set_sensitive(False)
        self.cleanup_cancel_button.set_sensitive(False)
        self.cleanup_phase.set_text(
            _("Interrompendo após concluir o lote atual…")
        )
        self._append_cleanup_log(
            _("Interrupção solicitada; aguardando o lote atual.")
        )

    def _handle_cleanup_progress(self, event: dict[str, Any]) -> bool:
        is_undo = event.get("type") == "undo"
        moved = int(
            event.get("restored", 0)
            if is_undo
            else event.get("moved", 0)
        )
        total = int(event.get("total", 0))
        fraction = moved / total if total else 0
        self.cleanup_progress_bar.set_fraction(max(0, min(fraction, 1)))
        self.cleanup_progress_bar.set_text(f"{fraction * 100:.1f}%")
        if is_undo:
            self.cleanup_count_label.set_text(
                _("{restored} de {total} mensagens restauradas").format(
                    restored=f"{moved:,}".replace(",", "."),
                    total=f"{total:,}".replace(",", "."),
                )
            )
        else:
            self.cleanup_count_label.set_text(
                _("{moved} de {total} mensagens movidas").format(
                    moved=f"{moved:,}".replace(",", "."),
                    total=f"{total:,}".replace(",", "."),
                )
            )
        text = str(event.get("text") or "")
        if text:
            self.cleanup_phase.set_text(text)
            self._append_cleanup_log(text)
        return False

    def _begin_cleanup_reconciliation(
        self,
        mailbox_names: list[str],
    ) -> bool:
        self.cleanup_pause_event.clear()
        self.cleanup_pause_button.set_sensitive(False)
        self.cleanup_pause_button.set_visible(False)
        self.cleanup_cancel_button.set_sensitive(False)
        self.cleanup_phase.set_text(_("Atualizando registros locais…"))
        self.cleanup_progress_bar.set_fraction(0)
        self.cleanup_progress_bar.set_text(_("Atualizando"))
        self.cleanup_count_label.set_text(
            _(
                "Conferindo {folders} pasta(s) envolvida(s), sem repetir a "
                "extração completa."
            ).format(folders=len(mailbox_names))
        )
        self._append_cleanup_log(
            _(
                "Mini sincronização automática iniciada nas pastas: {folders}."
            ).format(folders=", ".join(mailbox_names))
        )
        return False

    def _handle_cleanup_reconciliation_progress(
        self,
        event: dict[str, Any],
    ) -> bool:
        kind = event.get("type")
        if kind in {"phase", "planned"}:
            text = str(event.get("text") or "")
            if text:
                self._append_cleanup_log(text)
            if kind == "planned":
                checked = int(event.get("checked", 0))
                total = int(event.get("total", 0))
                self.cleanup_count_label.set_text(
                    _(
                        "{checked} mensagens conferidas · {total} cabeçalhos "
                        "para atualizar"
                    ).format(
                        checked=f"{checked:,}".replace(",", "."),
                        total=f"{total:,}".replace(",", "."),
                    )
                )
        elif kind == "mailbox_plan":
            self._append_cleanup_log(
                _(
                    "Mini sincronização de “{mailbox}”: {messages} no servidor "
                    "· {new_messages} cabeçalho(s) para atualizar."
                ).format(
                    mailbox=event.get("mailbox", ""),
                    messages=event.get("messages", 0),
                    new_messages=event.get("new_messages", 0),
                )
            )
        elif kind == "progress":
            processed = int(event.get("processed", 0))
            total = int(event.get("total", 0))
            fraction = processed / total if total else 0
            self.cleanup_progress_bar.set_fraction(
                max(0, min(fraction, 1))
            )
            self.cleanup_progress_bar.set_text(f"{fraction * 100:.1f}%")
        elif kind == "reconciled":
            self._append_cleanup_log(
                _(
                    "Pasta “{mailbox}” reconciliada: {current} presente(s) · "
                    "{missing} ausente(s) · {restored} restaurada(s)."
                ).format(
                    mailbox=event.get("mailbox", ""),
                    current=event.get("current", 0),
                    missing=event.get("missing", 0),
                    restored=event.get("restored", 0),
                )
            )
        return False

    def _reconcile_after_cleanup_action(
        self,
        account: dict[str, Any],
        password: str,
        source_names: set[str],
    ) -> dict[str, Any]:
        mailboxes = self.database.get_action_reconciliation_mailboxes(
            int(account["id"]),
            source_names,
        )
        if not mailboxes:
            raise RuntimeError(
                _(
                    "Nenhuma pasta local foi encontrada para a atualização "
                    "automática."
                )
            )
        GLib.idle_add(
            self._begin_cleanup_reconciliation,
            [str(mailbox["remote_name"]) for mailbox in mailboxes],
        )

        def progress(event: dict[str, Any]) -> None:
            GLib.idle_add(
                self._handle_cleanup_reconciliation_progress,
                event,
            )

        result = self.extractor.sync(
            account,
            password,
            mailboxes,
            progress,
            threading.Event(),
            threading.Event(),
            batch_size=500,
        )
        if result.get("status") != "completed":
            raise RuntimeError(
                _("A atualização automática não foi concluída.")
            )
        GLib.idle_add(
            self._append_cleanup_log,
            _(
                "Mini sincronização concluída; os UIDs locais já refletem o "
                "servidor."
            ),
        )
        return result

    @staticmethod
    def _cleanup_reconciliation_suffix(result: dict[str, Any]) -> str:
        error = str(result.get("reconciliation_error") or "").strip()
        if error:
            return _(
                "\n\nA atualização local automática não foi concluída: "
                "{detail}. Sincronize a conta antes de repetir uma ação sobre "
                "essas mensagens."
            ).format(detail=error)
        return _(
            "\n\nAs pastas envolvidas e os UIDs locais foram atualizados "
            "automaticamente."
        )

    def _finish_cleanup_dialog(
        self,
        status: str,
        moved: int,
        detail: str,
    ) -> None:
        self.cleanup_operation_active = False
        self.cleanup_pause_event.clear()
        self.cleanup_pause_button.set_sensitive(False)
        self.cleanup_pause_button.set_visible(False)
        self.cleanup_cancel_button.set_sensitive(True)
        self.cleanup_cancel_button.set_label(_("Fechar"))
        if status == "completed":
            self.cleanup_progress_bar.set_fraction(1)
            self.cleanup_progress_bar.set_text("100%")
            self.cleanup_phase.set_text(_("Limpeza concluída"))
            self._show_cleanup_recovery_options()
        elif status == "cancelled":
            self.cleanup_phase.set_text(
                _("Limpeza interrompida com segurança")
            )
            self._show_cleanup_recovery_options()
        elif status == "undo_completed":
            self.cleanup_progress_bar.set_fraction(1)
            self.cleanup_progress_bar.set_text("100%")
            self.cleanup_phase.set_text(_("Reversão concluída"))
            self.cleanup_undo_button.set_visible(False)
            self.cleanup_recovery_note.set_text(
                (
                    _(
                        "As mensagens confirmadas foram retiradas da Lixeira "
                        "e os UIDs locais foram atualizados automaticamente."
                    )
                    if self.cleanup_reconciliation_error is None
                    else _(
                        "As mensagens confirmadas foram retiradas da Lixeira, "
                        "mas a atualização local automática falhou. Elas "
                        "permanecerão fora dos rankings até a próxima "
                        "sincronização manual."
                    )
                )
            )
            self.cleanup_recovery_note.set_visible(True)
        elif status == "undo_cancelled":
            self.cleanup_phase.set_text(
                _("Reversão interrompida com segurança")
            )
            self.cleanup_undo_button.set_visible(False)
            self.cleanup_recovery_note.set_text(
                _(
                    "Parte das mensagens foi restaurada. As demais continuam "
                    "na Lixeira e podem ser recuperadas pelo webmail."
                )
            )
            self.cleanup_recovery_note.set_visible(True)
        else:
            self.cleanup_phase.set_text(
                _("A reversão foi interrompida")
                if self.cleanup_mode == "undo"
                else _("A limpeza foi interrompida")
            )
            self.cleanup_details_button.set_active(True)
            if self.cleanup_mode == "move":
                self._show_cleanup_recovery_options()
            else:
                self.cleanup_undo_button.set_visible(False)
                self.cleanup_recovery_note.set_text(
                    _(
                        "As mensagens ainda presentes na Lixeira podem ser "
                        "restauradas pela interface oficial do e-mail."
                    )
                )
                self.cleanup_recovery_note.set_visible(True)
        self.cleanup_count_label.set_text(detail)
        self._append_cleanup_log(detail)

    def _cleanup_undo_unavailable_reason(self) -> str:
        if self.cleanup_undo_items:
            return _(
                "Os dados temporários necessários para reverter esta operação "
                "não estão mais disponíveis. Restaure as mensagens pela "
                "Lixeira do webmail."
            )
        if not self.cleanup_moved_count:
            return _(
                "Nenhuma mensagem foi movida nesta operação, portanto não há "
                "uma ação para reverter."
            )
        if (
            self.cleanup_undo_available_count
            and self.cleanup_moved_count
            and self.cleanup_undo_available_count
            < self.cleanup_moved_count
        ):
            return _(
                "O servidor forneceu identificadores seguros para "
                "{available} de {moved} mensagens. A reversão automática foi "
                "desativada para não restaurar apenas parte da operação. "
                "Restaure as mensagens pela Lixeira do webmail."
            ).format(
                available=(
                    f"{self.cleanup_undo_available_count:,}"
                    .replace(",", ".")
                ),
                moved=f"{self.cleanup_moved_count:,}".replace(",", "."),
            )
        return _(
            "O servidor não forneceu identificadores suficientes para uma "
            "reversão automática segura. Restaure as mensagens diretamente "
            "pela Lixeira do webmail."
        )

    def _cleanup_undo_is_available(self) -> bool:
        return bool(
            self.cleanup_undo_items
            and self.cleanup_undo_account
            and self.cleanup_undo_password
            and self.cleanup_undo_trash
        )

    def _show_cleanup_recovery_options(self) -> None:
        automatic = self._cleanup_undo_is_available()
        self.cleanup_undo_button.set_visible(True)
        self.cleanup_undo_button.set_sensitive(True)
        if automatic:
            self.cleanup_undo_button.set_tooltip_text(
                _(
                    "Restaurar com segurança os lotes movidos nesta operação"
                )
            )
            message = _(
                "A reversão automática está disponível para todos os lotes "
                "confirmados nesta operação. Você também pode restaurar as "
                "mensagens pela Lixeira do webmail."
            )
        else:
            self.cleanup_undo_button.set_tooltip_text(
                _(
                    "Exibir por que a reversão automática não está disponível"
                )
            )
            message = self._cleanup_undo_unavailable_reason()
        retention = _(
            "O aplicativo não esvaziou a Lixeira. Muitos provedores mantêm as "
            "mensagens nela por cerca de 30 dias, mas o prazo exato depende "
            "da política do serviço e da conta."
        )
        self.cleanup_recovery_note.set_text(f"{message}\n\n{retention}")
        self.cleanup_recovery_note.set_visible(True)

    def _start_cleanup_undo(self, _button: Gtk.Button) -> None:
        if not self._cleanup_undo_is_available():
            retention = _(
                "O aplicativo não esvaziou a Lixeira. Muitos provedores "
                "mantêm as mensagens nela por cerca de 30 dias, mas o prazo "
                "exato depende da política do serviço e da conta."
            )
            self._show_notice(
                _("Reversão automática indisponível"),
                f"{self._cleanup_undo_unavailable_reason()}\n\n{retention}",
                parent=self.cleanup_dialog,
            )
            return

        count = len(self.cleanup_undo_items)
        dialog = AppDialog(
            transient_for=self.cleanup_dialog or self,
            title=_("Confirmar reversão"),
            default_width=540,
        )
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        set_margins(content, 20)
        heading = Gtk.Label(
            label=_(
                "Reverter a movimentação de {count} mensagens?"
            ).format(count=f"{count:,}".replace(",", ".")),
            wrap=True,
            xalign=0,
        )
        heading.add_css_class("title-3")
        content.append(heading)
        detail = Gtk.Label(
            label=_(
                "O aplicativo procurará essas mensagens na Lixeira e tentará "
                "restaurar os marcadores ou a pasta de origem. Confirme para "
                "iniciar a reversão."
            ),
            wrap=True,
            xalign=0,
        )
        detail.add_css_class("dim-label")
        content.append(detail)
        dialog.get_content_area().append(content)
        dialog.add_button(_("Cancelar"), Gtk.ResponseType.CANCEL)
        confirm = dialog.add_button(
            _("Reverter movimentação"),
            Gtk.ResponseType.ACCEPT,
        )
        confirm.add_css_class("suggested-action")
        dialog.set_default_response(Gtk.ResponseType.CANCEL)

        def response(current_dialog: AppDialog, response_id: int) -> None:
            current_dialog.destroy()
            if response_id == Gtk.ResponseType.ACCEPT:
                self._run_cleanup_undo()

        dialog.connect("response", response)
        dialog.present()

    def _run_cleanup_undo(self) -> None:
        if not self._cleanup_undo_is_available():
            return
        account = self.cleanup_undo_account
        password = self.cleanup_undo_password
        trash_mailbox = self.cleanup_undo_trash
        assert account is not None
        assert password is not None
        assert trash_mailbox is not None
        undo_items = list(self.cleanup_undo_items)
        source_names = {
            str(item.get("source_mailbox") or "").strip()
            for item in undo_items
            if str(item.get("source_mailbox") or "").strip()
        }
        restored_message_ids: set[int] = set()
        self.cleanup_operation_active = True
        self.cleanup_mode = "undo"
        self.cleanup_cancel_event.clear()
        self.cleanup_pause_event.clear()
        self.cleanup_undo_button.set_visible(False)
        self.cleanup_recovery_note.set_visible(False)
        self.cleanup_cancel_button.set_label(_("Interromper"))
        self.cleanup_cancel_button.set_sensitive(True)
        self.cleanup_progress_bar.set_fraction(0)
        self.cleanup_progress_bar.set_text("0%")
        self.cleanup_phase.set_text(_("Revertendo a movimentação…"))
        self.cleanup_count_label.set_text(
            _("{restored} de {total} mensagens restauradas").format(
                restored="0",
                total=f"{len(undo_items):,}".replace(",", "."),
            )
        )
        self._append_cleanup_log(
            _("Iniciando a reversão dos lotes confirmados.")
        )

        def progress(event: dict[str, Any]) -> None:
            message_ids = [
                int(value) for value in event.get("message_ids", [])
            ]
            if message_ids:
                restored_message_ids.update(message_ids)
                self.database.mark_messages_restored(message_ids)
            GLib.idle_add(self._handle_cleanup_progress, event)

        def work() -> None:
            try:
                result = self.extractor.restore_from_trash(
                    account,
                    password,
                    undo_items,
                    trash_mailbox,
                    progress,
                    cancel_event=self.cleanup_cancel_event,
                )
                self.database.mark_messages_restored(result["message_ids"])
                try:
                    result["reconciliation"] = (
                        self._reconcile_after_cleanup_action(
                            account,
                            password,
                            source_names,
                        )
                    )
                    self.cleanup_reconciliation_error = None
                except Exception as sync_exc:
                    self.cleanup_reconciliation_error = str(sync_exc)
                    result["reconciliation_error"] = str(sync_exc)
                GLib.idle_add(success, result)
            except Exception as exc:
                reconciliation_error = ""
                if restored_message_ids:
                    try:
                        self._reconcile_after_cleanup_action(
                            account,
                            password,
                            source_names,
                        )
                        self.cleanup_reconciliation_error = None
                    except Exception as sync_exc:
                        reconciliation_error = str(sync_exc)
                        self.cleanup_reconciliation_error = (
                            reconciliation_error
                        )
                GLib.idle_add(
                    failure,
                    str(exc),
                    reconciliation_error,
                )

        def success(result: dict[str, Any]) -> bool:
            restored = int(result["restored"])
            self._show_results(account)
            if result.get("status") == "cancelled":
                detail = _(
                    "{restored} mensagens foram restauradas antes da interrupção."
                ).format(
                    restored=f"{restored:,}".replace(",", ".")
                )
                detail += self._cleanup_reconciliation_suffix(result)
                self._finish_cleanup_dialog(
                    "undo_cancelled",
                    restored,
                    detail,
                )
            else:
                detail = _(
                    "{restored} mensagens foram restauradas com sucesso."
                ).format(
                    restored=f"{restored:,}".replace(",", ".")
                )
                detail += self._cleanup_reconciliation_suffix(result)
                self._finish_cleanup_dialog(
                    "undo_completed",
                    restored,
                    detail,
                )
            return False

        def failure(
            detail: str,
            reconciliation_error: str,
        ) -> bool:
            self._show_results(account)
            if reconciliation_error:
                detail += _(
                    "\n\nA atualização local automática também falhou: "
                    "{detail}. Sincronize a conta antes de tentar novamente."
                ).format(detail=reconciliation_error)
            self._finish_cleanup_dialog("failed", 0, detail)
            return False

        threading.Thread(target=work, daemon=True).start()

    def _run_cleanup(
        self,
        account: dict[str, Any],
        imap_password: str,
        targets: list[dict[str, Any]],
        trash_mailbox: dict[str, Any],
    ) -> None:
        self._build_cleanup_progress_dialog(len(targets))
        committed_message_ids: set[int] = set()
        committed_undo_items: list[dict[str, Any]] = []
        source_names = {
            str(target.get("mailbox_name") or "").strip()
            for target in targets
            if str(target.get("mailbox_name") or "").strip()
        }

        def progress(event: dict[str, Any]) -> None:
            committed_ids = {
                int(value) for value in event.get("message_ids", [])
            }
            if committed_ids:
                committed_message_ids.update(committed_ids)
                self.database.mark_messages_trashed(committed_ids)
            committed_undo_items.extend(event.get("undo_items", []))
            GLib.idle_add(self._handle_cleanup_progress, event)

        def work() -> None:
            try:
                result = self.extractor.move_to_trash(
                    account,
                    imap_password,
                    targets,
                    trash_mailbox,
                    progress,
                    cancel_event=self.cleanup_cancel_event,
                    pause_event=self.cleanup_pause_event,
                )
                self.database.mark_messages_trashed(result["message_ids"])
                try:
                    result["reconciliation"] = (
                        self._reconcile_after_cleanup_action(
                            account,
                            imap_password,
                            source_names,
                        )
                    )
                    self.cleanup_reconciliation_error = None
                except Exception as sync_exc:
                    self.cleanup_reconciliation_error = str(sync_exc)
                    result["reconciliation_error"] = str(sync_exc)
                GLib.idle_add(success, result)
            except Exception as exc:
                reconciliation_error = ""
                if committed_message_ids:
                    try:
                        self._reconcile_after_cleanup_action(
                            account,
                            imap_password,
                            source_names,
                        )
                        self.cleanup_reconciliation_error = None
                    except Exception as sync_exc:
                        reconciliation_error = str(sync_exc)
                        self.cleanup_reconciliation_error = (
                            reconciliation_error
                        )
                GLib.idle_add(
                    failure,
                    str(exc),
                    reconciliation_error,
                )

        def success(result: dict[str, Any]) -> bool:
            moved = int(result["moved"])
            self.cleanup_moved_count = moved
            self.cleanup_undo_available_count = int(
                result.get("undo_available", 0)
            )
            if result.get("undo_supported"):
                self.cleanup_undo_items = list(result.get("undo_items", []))
            self.cleanup_undo_account = account
            self.cleanup_undo_password = imap_password
            self.cleanup_undo_trash = trash_mailbox
            self._show_results(account)
            if result.get("status") == "cancelled":
                detail = _(
                    "{moved} mensagens foram movidas antes da interrupção. "
                    "As demais continuam disponíveis."
                ).format(moved=f"{moved:,}".replace(",", "."))
                detail += self._cleanup_reconciliation_suffix(result)
                self._finish_cleanup_dialog("cancelled", moved, detail)
            else:
                detail = _(
                    "{moved} mensagens foram movidas para a Lixeira. "
                    "A Lixeira não foi esvaziada."
                ).format(moved=f"{moved:,}".replace(",", "."))
                detail += self._cleanup_reconciliation_suffix(result)
                self._finish_cleanup_dialog("completed", moved, detail)
            return False

        def failure(
            detail: str,
            reconciliation_error: str,
        ) -> bool:
            self.cleanup_moved_count = len(committed_message_ids)
            self.cleanup_undo_available_count = len(committed_undo_items)
            if len(committed_undo_items) == len(committed_message_ids):
                self.cleanup_undo_items = list(committed_undo_items)
            self.cleanup_undo_account = account
            self.cleanup_undo_password = imap_password
            self.cleanup_undo_trash = trash_mailbox
            self._show_results(account)
            if committed_message_ids:
                suffix = "\n\n" + _(
                    "{moved} mensagens já confirmadas pelo servidor foram "
                    "mantidas como concluídas; as demais continuam disponíveis."
                ).format(
                    moved=f"{len(committed_message_ids):,}".replace(",", ".")
                )
            else:
                suffix = "\n\n" + _("Nenhuma mensagem foi alterada.")
            if reconciliation_error:
                suffix += _(
                    "\n\nA atualização local automática também falhou: "
                    "{detail}. Sincronize a conta antes de tentar novamente."
                ).format(detail=reconciliation_error)
            self._finish_cleanup_dialog(
                "failed",
                len(committed_message_ids),
                detail + suffix,
            )
            return False

        threading.Thread(target=work, daemon=True).start()

    def _choose_export(
        self,
        kind: str,
        selection_only: bool = False,
        message_ids: list[int] | None = None,
    ) -> None:
        if not self.active_account:
            return
        if self._require_account_unlocked(self.active_account) is None:
            return
        if (
            selection_only
            and not self.selected_cleanup_senders
            and not self.selected_cleanup_domains
            and not message_ids
        ):
            self._show_error(
                "Nenhuma mensagem selecionada",
                "Marque pelo menos um remetente ou domínio antes de exportar "
                "a seleção.",
            )
            return
        chooser = Gtk.FileChooserNative.new(
            _("Salvar exportação da seleção")
            if selection_only
            else _("Salvar exportação completa"),
            self,
            Gtk.FileChooserAction.SAVE,
            _("Salvar"),
            _("Cancelar"),
        )
        slug = (
            self.active_account["email"]
            .replace("@", "-")
            .replace(".", "-")
            .replace("/", "-")
        )
        selection_name = "selecao-" if selection_only else ""
        chooser.set_current_name(
            f"metadados-{selection_name}{slug}.{kind}"
        )

        def response(dialog: Gtk.FileChooserNative, response_id: int) -> None:
            if response_id != Gtk.ResponseType.ACCEPT:
                dialog.destroy()
                return
            selected_file = dialog.get_file()
            path = selected_file.get_path() if selected_file else None
            dialog.destroy()
            if path:
                self._run_export(
                    kind,
                    Path(path),
                    selection_only=selection_only,
                    message_ids=message_ids,
                )

        chooser.connect("response", response)
        chooser.show()

    def _run_export(
        self,
        kind: str,
        destination: Path,
        selection_only: bool = False,
        message_ids: list[int] | None = None,
    ) -> None:
        if not self.active_account:
            return
        account_id = int(self.active_account["id"])
        selected_senders = (
            sorted(self.selected_cleanup_senders)
            if selection_only
            else None
        )
        selected_domains = (
            sorted(self.selected_cleanup_domains)
            if selection_only
            else None
        )
        selected_message_ids = (
            sorted({int(value) for value in message_ids or []})
            if selection_only and message_ids is not None
            else None
        )
        self.csv_button.set_sensitive(False)
        self.ods_button.set_sensitive(False)
        for button in (
            *self.selected_csv_buttons,
            *self.selected_ods_buttons,
        ):
            button.set_sensitive(False)
        self.export_status.set_visible(True)
        self.export_status.set_text(
            _("Gerando o arquivo da seleção…")
            if selection_only
            else _("Gerando a exportação completa…")
        )
        self.export_operation_active = True

        def work() -> None:
            try:
                if kind == "csv":
                    output = export_csv(
                        self.database,
                        [account_id],
                        destination,
                        lambda current: GLib.idle_add(
                            self.export_status.set_text,
                            _("{rows} linhas gravadas…").format(
                                rows=f"{current:,}".replace(",", ".")
                            ),
                        ),
                        sender_emails=selected_senders,
                        domains=selected_domains,
                        message_ids=selected_message_ids,
                    )
                else:
                    output = export_ods(
                        self.database,
                        [account_id],
                        destination,
                        lambda sheet, current: GLib.idle_add(
                            self.export_status.set_text,
                            _("{sheet}: {rows} linhas gravadas…").format(
                                sheet=sheet,
                                rows=f"{current:,}".replace(",", "."),
                            ),
                        ),
                        sender_emails=selected_senders,
                        domains=selected_domains,
                        message_ids=selected_message_ids,
                    )
                GLib.idle_add(success, str(output))
            except Exception as exc:
                GLib.idle_add(failure, str(exc))

        def success(path: str) -> bool:
            self.export_operation_active = False
            self._refresh_results_summary()
            self._update_cleanup_preview()
            self.export_status.set_text(
                _("Arquivo salvo em: {path}").format(path=path)
            )
            return False

        def failure(detail: str) -> bool:
            self.export_operation_active = False
            self._refresh_results_summary()
            self._update_cleanup_preview()
            self.export_status.set_text(_("A exportação falhou."))
            self._show_error("Não foi possível exportar", detail)
            return False

        threading.Thread(target=work, daemon=True).start()

    def _confirm_remove(self, account: dict[str, Any]) -> None:
        account_id = int(account["id"])
        if account_id not in self.unlocked_accounts:
            self._confirm_recovery_remove(account)
            return
        dialog = AppDialog(
            transient_for=self,
            title=_("Remover conta"),
            default_width=500,
        )
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        set_margins(content, 20)
        heading = Gtk.Label(
            label=_("Remover “{account}”?").format(
                account=account["display_name"]
            ),
            wrap=True,
            xalign=0,
        )
        heading.add_css_class("title-3")
        content.append(heading)
        detail = Gtk.Label(
            label=_(
                "A conta, a credencial criptografada e todos os dados extraídos "
                "localmente serão removidos."
            ),
            wrap=True,
            xalign=0,
        )
        detail.add_css_class("dim-label")
        content.append(detail)
        dialog.get_content_area().append(content)
        dialog.add_button(_("Cancelar"), Gtk.ResponseType.CANCEL)
        remove = dialog.add_button(_("Remover tudo"), Gtk.ResponseType.ACCEPT)
        remove.add_css_class("destructive-action")
        dialog.set_default_response(Gtk.ResponseType.CANCEL)

        def response(current_dialog: AppDialog, response_id: int) -> None:
            if response_id == Gtk.ResponseType.ACCEPT:
                self._delete_local_account(account)
            current_dialog.destroy()

        dialog.connect("response", response)
        dialog.present()

    def _delete_local_account(self, account: dict[str, Any]) -> None:
        account_id = int(account["id"])
        session = self.unlocked_accounts.pop(account_id, None)
        if session is not None:
            session.clear()
        self.database.delete_account(account_id)
        if (
            self.active_account
            and int(self.active_account["id"]) == account_id
        ):
            self._clear_active_credentials()
        self.refresh_accounts()

    def _confirm_recovery_remove(self, account: dict[str, Any]) -> None:
        account_id = int(account["id"])
        if account_id in self.recovery_removals_pending:
            return
        dialog = AppDialog(
            transient_for=self,
            title=_("Remover conta bloqueada"),
            default_width=560,
        )
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        set_margins(content, 20)
        heading = Gtk.Label(
            label=_("Remover “{account}” sem a senha local?").format(
                account=account["display_name"]
            ),
            wrap=True,
            xalign=0,
        )
        heading.add_css_class("title-3")
        content.append(heading)
        explanation = Gtk.Label(
            label=_(
                "A senha local não pode ser recuperada. A autorização "
                "administrativa permite apenas apagar este cadastro e os "
                "dados armazenados localmente."
            ),
            wrap=True,
            xalign=0,
        )
        explanation.add_css_class("dim-label")
        content.append(explanation)
        boundary = Gtk.Label(
            label=_(
                "A senha IMAP não será descriptografada, nenhuma conexão "
                "será feita e nenhuma mensagem do servidor será alterada."
            ),
            wrap=True,
            xalign=0,
        )
        boundary.add_css_class("warning")
        content.append(boundary)
        dialog.get_content_area().append(content)
        dialog.add_button(_("Cancelar"), Gtk.ResponseType.CANCEL)
        authorize = dialog.add_button(
            _("Autorizar remoção"),
            Gtk.ResponseType.ACCEPT,
        )
        authorize.add_css_class("destructive-action")
        dialog.set_default_response(Gtk.ResponseType.CANCEL)

        def response(current_dialog: AppDialog, response_id: int) -> None:
            current_dialog.destroy()
            if response_id == Gtk.ResponseType.ACCEPT:
                self._authorize_locked_account_removal(account)

        dialog.connect("response", response)
        dialog.present()

    def _authorize_locked_account_removal(
        self,
        account: dict[str, Any],
    ) -> None:
        account_id = int(account["id"])
        if account_id in self.recovery_removals_pending:
            return
        pkexec = shutil.which("pkexec")
        if pkexec is None:
            self._show_error(
                _("Autenticação administrativa indisponível"),
                _(
                    "O componente pkexec não foi encontrado. Instale o "
                    "suporte a Polkit do sistema para remover uma conta "
                    "bloqueada."
                ),
            )
            return
        helper = (
            str(RECOVERY_AUTH_HELPER)
            if RECOVERY_AUTH_HELPER.is_file()
            else "/usr/bin/true"
        )
        self.recovery_removals_pending.add(account_id)
        self.refresh_accounts()

        def finish(returncode: int) -> bool:
            self.recovery_removals_pending.discard(account_id)
            current = self.database.get_account(account_id)
            if returncode == 0 and current is not None:
                if (
                    str(current.get("email") or "")
                    != str(account.get("email") or "")
                ):
                    self.refresh_accounts()
                    self._show_error(
                        _("Não foi possível autorizar a remoção"),
                        _(
                            "A conta mudou durante a autenticação. Nenhum "
                            "dado foi removido."
                        ),
                    )
                    return False
                self._delete_local_account(current)
                self._show_notice(
                    _("Conta removida"),
                    _(
                        "A conta bloqueada e todos os dados locais associados "
                        "foram removidos. Nenhuma mensagem no servidor foi "
                        "alterada."
                    ),
                )
                return False
            self.refresh_accounts()
            if returncode == 126:
                self._show_notice(
                    _("Autorização administrativa cancelada"),
                    _("Nenhum dado foi removido."),
                )
            elif current is not None:
                self._show_error(
                    _("Não foi possível autorizar a remoção"),
                    _(
                        "O sistema recusou ou não conseguiu concluir a "
                        "autenticação administrativa. Nenhum dado foi removido."
                    ),
                )
            return False

        def work() -> None:
            try:
                result = subprocess.run(
                    [pkexec, helper],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                returncode = int(result.returncode)
            except OSError:
                returncode = 127
            GLib.idle_add(finish, returncode)

        threading.Thread(target=work, daemon=True).start()

    def _confirm_rebuild_index(self) -> None:
        if not self.active_account:
            return
        account = self.active_account
        session = self._require_account_unlocked(account)
        if session is None:
            return
        dialog = AppDialog(
            transient_for=self,
            title=_("Reconstruir índice local"),
            default_width=560,
        )
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        set_margins(content, 20)
        heading = Gtk.Label(
            label=_("Recriar os metadados desta conta?"),
            wrap=True,
            xalign=0,
        )
        heading.add_css_class("title-3")
        content.append(heading)
        detail = Gtk.Label(
            label=_(
                "Todos os metadados e resultados locais desta conta serão "
                "removidos e baixados novamente na próxima sincronização. "
                "A conta, a senha criptografada e as mensagens no servidor "
                "não serão alteradas."
            ),
            wrap=True,
            xalign=0,
        )
        detail.add_css_class("dim-label")
        content.append(detail)
        dialog.get_content_area().append(content)
        cancel = dialog.add_button(_("Cancelar"), Gtk.ResponseType.CANCEL)
        rebuild = dialog.add_button(
            _("Reconstruir índice"),
            Gtk.ResponseType.ACCEPT,
        )
        rebuild.add_css_class("destructive-action")
        dialog.set_default_response(Gtk.ResponseType.CANCEL)

        def response(current_dialog: AppDialog, response_id: int) -> None:
            if response_id != Gtk.ResponseType.ACCEPT:
                current_dialog.destroy()
                return
            cancel.set_sensitive(False)
            rebuild.set_sensitive(False)
            heading.set_text(_("Preparando a reconstrução"))
            detail.set_text(
                _(
                    "Removendo o índice local. Nenhuma mensagem será alterada "
                    "no servidor…"
                )
            )
            spinner = Gtk.Spinner()
            spinner.set_halign(Gtk.Align.CENTER)
            spinner.start()
            content.append(spinner)

            def work() -> None:
                try:
                    self.database.rebuild_account_index(int(account["id"]))
                    GLib.idle_add(success)
                except Exception as exc:
                    GLib.idle_add(failure, str(exc))

            def success() -> bool:
                spinner.stop()
                current_dialog.destroy()
                refreshed = self.database.get_account(int(account["id"]))
                if refreshed is not None:
                    self.active_account = refreshed
                self.active_imap_password = session["imap_password"]
                self.subject_preview_cache.clear()
                self._discover_folders(is_rebuild=True)
                return False

            def failure(error_detail: str) -> bool:
                spinner.stop()
                cancel.set_sensitive(True)
                rebuild.set_sensitive(True)
                heading.set_text(_("Não foi possível reconstruir o índice"))
                detail.set_text(error_detail)
                return False

            threading.Thread(target=work, daemon=True).start()

        dialog.connect("response", response)
        dialog.present()

    def _show_error(
        self, title: str, detail: str, parent: Gtk.Window | None = None
    ) -> None:
        title = _(title)
        detail = _(detail)
        dialog = AppDialog(
            transient_for=parent or self,
            title=title,
            default_width=500,
        )
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        set_margins(content, 20)
        heading = Gtk.Label(label=title, wrap=True, xalign=0)
        heading.add_css_class("title-3")
        content.append(heading)
        message = Gtk.Label(label=detail, wrap=True, xalign=0)
        message.add_css_class("dim-label")
        content.append(message)
        dialog.get_content_area().append(content)
        ok_button = dialog.add_button(_("OK"), Gtk.ResponseType.OK)
        ok_button.add_css_class("suggested-action")
        dialog.set_default_response(Gtk.ResponseType.OK)
        dialog.connect("response", lambda item, _response: item.destroy())
        dialog.present_with_focus(ok_button)

    def _show_notice(
        self,
        title: str,
        detail: str,
        parent: Gtk.Window | None = None,
    ) -> None:
        title = _(title)
        detail = _(detail)
        dialog = AppDialog(
            transient_for=parent or self,
            title=title,
            default_width=500,
        )
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        set_margins(content, 20)
        heading = Gtk.Label(label=title, wrap=True, xalign=0)
        heading.add_css_class("title-3")
        content.append(heading)
        message = Gtk.Label(label=detail, wrap=True, xalign=0)
        message.add_css_class("dim-label")
        content.append(message)
        dialog.get_content_area().append(content)
        ok_button = dialog.add_button(_("OK"), Gtk.ResponseType.OK)
        ok_button.add_css_class("suggested-action")
        dialog.set_default_response(Gtk.ResponseType.OK)
        dialog.connect("response", lambda item, _response: item.destroy())
        dialog.present_with_focus(ok_button)

    def _show_page(self, name: str) -> None:
        self.stack.set_visible_child_name(name)
        is_accounts = name == "accounts"
        self.back_button.set_visible(not is_accounts and name != "progress")
        self.add_button.set_visible(is_accounts)
        account_id = (
            int(self.active_account["id"]) if self.active_account else None
        )
        self.rebuild_index_action.set_enabled(
            not is_accounts
            and name != "progress"
            and account_id is not None
            and account_id in self.unlocked_accounts
        )

    def _on_back(self, _button: Gtk.Button) -> None:
        if self.stack.get_visible_child_name() == "progress":
            return
        self._clear_active_credentials()
        self.refresh_accounts()
        self._show_page("accounts")

    def _clear_active_credentials(self, keep_account: bool = False) -> None:
        self.active_imap_password = None
        if not keep_account:
            self.active_account = None


class HeaderExporterApplication(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID)
        self.css_provider: Gtk.CssProvider | None = None
        self.update_service = UpdateService(current_version=APP_VERSION)
        self._main_window: MainWindow | None = None
        self._update_window: UpdateWindow | None = None
        self._startup_check_started = False
        self._mandatory_result: UpdateCheckResult | None = None
        self._shutting_down = False
        self._safe_exit_pending = False

    def do_startup(self) -> None:
        Gtk.Application.do_startup(self)
        display = Gdk.Display.get_default()
        stylesheet = Path(__file__).with_name("style.css")
        if display is None:
            return
        Gtk.IconTheme.get_for_display(display).add_search_path(
            str(ICON_THEME_PATH)
        )
        if not stylesheet.exists():
            return
        provider = Gtk.CssProvider()
        try:
            provider.load_from_path(str(stylesheet))
        except GLib.Error as exc:
            print(
                _("Não foi possível carregar o estilo: {detail}").format(
                    detail=exc
                ),
                file=sys.stderr,
            )
            return
        Gtk.StyleContext.add_provider_for_display(
            display,
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        self.css_provider = provider

    def do_activate(self) -> None:
        window = self._main_window
        if window is None:
            try:
                window = MainWindow(self)
            except SecretError as exc:
                print(str(exc), file=sys.stderr)
                self.quit()
                return
            self._main_window = window
        window.present()
        if self._update_window is not None:
            self._update_window.present()
        if self._mandatory_result is not None:
            self._present_update_window(self._mandatory_result, window)
            return
        if not self._startup_check_started:
            self._startup_check_started = True
            GLib.idle_add(self._start_startup_update_check)

    def _start_startup_update_check(self) -> bool:
        if self._shutting_down:
            return False

        def completed(result: UpdateCheckResult) -> None:
            GLib.idle_add(self._handle_startup_update_result, result)

        self.update_service.check_async(CheckSource.STARTUP, completed)
        return False

    def request_manual_update_check(
        self,
        parent: Gtk.Window,
        button: Gtk.Button,
        status_label: Gtk.Label,
    ) -> None:
        if self._shutting_down:
            return
        button.set_sensitive(False)
        button.set_label(_("Verificando…"))
        status_label.set_text(_("Consultando a versão mais recente…"))
        status_label.set_visible(True)
        parent_ref = weakref.ref(parent)
        button_ref = weakref.ref(button)
        status_ref = weakref.ref(status_label)

        def completed(result: UpdateCheckResult) -> None:
            GLib.idle_add(
                self._handle_manual_update_result,
                result,
                parent_ref,
                button_ref,
                status_ref,
            )

        started = self.update_service.check_async(CheckSource.MANUAL, completed)
        if not started:
            status_label.set_text(_("Verificação já em andamento…"))

    def _handle_startup_update_result(
        self,
        result: UpdateCheckResult,
    ) -> bool:
        if self._shutting_down:
            return False
        window = self._main_window
        if window is None:
            return False
        if result.status == UpdateStatus.MANDATORY_UPDATE_REQUIRED:
            self._enter_mandatory_update_state(result)
            return False

        if result.status == UpdateStatus.OPTIONAL_UPDATE_AVAILABLE:
            self._present_update_window(result, window)
        return False

    def _handle_manual_update_result(
        self,
        result: UpdateCheckResult,
        parent_ref: weakref.ReferenceType[Gtk.Window],
        button_ref: weakref.ReferenceType[Gtk.Button],
        status_ref: weakref.ReferenceType[Gtk.Label],
    ) -> bool:
        if self._shutting_down:
            return False
        parent = parent_ref()
        button = button_ref()
        status_label = status_ref()
        if button is not None:
            button.set_label(_("Verificar atualizações"))
            button.set_sensitive(True)

        if result.status == UpdateStatus.CHECK_FAILED:
            if status_label is not None:
                status_label.set_text(
                    _(
                        "Não foi possível verificar atualizações. Verifique "
                        "sua conexão e tente novamente."
                    )
                )
            return False
        if result.status == UpdateStatus.UP_TO_DATE:
            if status_label is not None:
                status_label.set_text(
                    _("Você está usando a versão mais recente ({version}).").format(
                        version=result.current_version
                    )
                )
            return False
        if result.status == UpdateStatus.LOCAL_VERSION_NEWER:
            if status_label is not None:
                status_label.set_text(
                    _(
                        "Esta instalação é mais recente que a versão publicada "
                        "atualmente."
                    )
                )
            return False

        if parent is not None and parent.get_visible():
            parent.destroy()
        if result.status == UpdateStatus.MANDATORY_UPDATE_REQUIRED:
            self._enter_mandatory_update_state(result)
        else:
            self._present_update_window(result, self._main_window)
        return False

    def _enter_mandatory_update_state(
        self,
        result: UpdateCheckResult,
    ) -> None:
        self._mandatory_result = result
        window = self._main_window
        operation_active = False
        if window is not None:
            operation_active = window.prepare_for_mandatory_update()
        self._present_update_window(result, window)
        if operation_active:
            GLib.timeout_add(250, self._lock_runtime_when_safe)

    def _lock_runtime_when_safe(self) -> bool:
        if self._shutting_down:
            return False
        window = self._main_window
        if window is None:
            return False
        if window.has_critical_operation():
            return True
        window.stack.set_sensitive(False)
        return False

    def _present_update_window(
        self,
        result: UpdateCheckResult,
        parent: Gtk.Window | None,
    ) -> None:
        if self._shutting_down or result.manifest is None:
            return
        mandatory = result.status == UpdateStatus.MANDATORY_UPDATE_REQUIRED
        existing = self._update_window
        if existing is not None:
            same_release = existing.remote_version == result.remote_version
            if same_release and existing._mandatory == mandatory:
                existing.present()
                logging.getLogger("imap_exporter.update").info(
                    "Existing update window presented version=%s mandatory=%s",
                    result.remote_version,
                    mandatory,
                )
                return
            existing.destroy()

        candidate_parent = parent or self._main_window
        visible_parent = (
            candidate_parent
            if candidate_parent is not None and candidate_parent.get_visible()
            else None
        )
        update_window = UpdateWindow(
            application=self,
            transient_for=visible_parent,
            result=result,
            on_close=self._update_window_closed,
            on_quit=self._request_quit_for_update,
        )
        self._update_window = update_window
        update_window.present()
        logging.getLogger("imap_exporter.update").info(
            "Update window presented version=%s mandatory=%s",
            result.remote_version,
            mandatory,
        )

    def _update_window_closed(self, window: Gtk.Window) -> None:
        if self._update_window is window:
            self._update_window = None

    def _request_quit_for_update(self) -> None:
        if self._shutting_down or self._safe_exit_pending:
            return
        window = self._main_window
        if window is not None:
            window.prepare_for_mandatory_update()
        if window is not None and window.has_critical_operation():
            self._safe_exit_pending = True
            if self._update_window is not None:
                self._update_window.set_waiting_for_safe_exit()
            GLib.timeout_add(250, self._quit_when_runtime_is_safe)
            return
        self.quit()

    def _quit_when_runtime_is_safe(self) -> bool:
        if self._shutting_down:
            return False
        window = self._main_window
        if window is not None and window.has_critical_operation():
            return True
        self.quit()
        return False

    def do_shutdown(self) -> None:
        self._shutting_down = True
        self.update_service.shutdown()
        Gtk.Application.do_shutdown(self)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    application = HeaderExporterApplication()
    return application.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
