from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

from mail_exporter.db import Database
from mail_exporter.exporters import export_csv, export_ods
from mail_exporter.headers import (
    parse_bodystructure_fetch_item,
    parse_fetch_item,
)
from mail_exporter.imap_service import (
    AttachmentDownloadCancelled,
    compress_uid_set,
    decode_modified_utf7,
    encode_modified_utf7,
    MailExtractor,
    normalize_capabilities,
    parse_copyuid,
    parse_list_line,
    parse_message_for_reader,
    parse_status,
    quote_mailbox,
)
from mail_exporter.i18n import get_language, set_language, tr
from mail_exporter.paths import data_dir
from mail_exporter.providers import load_provider_presets
from mail_exporter.secrets import InvalidAccountPassword, SecretCipher


SAMPLE_HEADER = b"""From: =?UTF-8?Q?Jo=C3=A3o_da_Silva?= <joao@Exemplo.COM>
Sender: Equipe <envio@mailer.exemplo.com>
Reply-To: Atendimento <resposta@exemplo.com>
To: Cliente Um <cliente1@gmail.com>, cliente2@outlook.com
Cc: Gestor <gestor@empresa.com.br>
Subject: =?UTF-8?Q?Confirma=C3=A7=C3=A3o_do_pedido?=
Date: Tue, 28 Jul 2026 18:30:00 -0300
Message-ID: <mensagem-123@exemplo.com>
Delivered-To: conta@gmail.com
Return-Path: <bounce@mailer.exemplo.com>
List-ID: Clientes <clientes.exemplo.com>

"""

SAMPLE_METADATA = (
    b'7 (X-GM-THRID 1001 X-GM-MSGID 2002 '
    b'X-GM-LABELS (\\Inbox "Cliente VIP" "Projetos (2026)") '
    b'UID 42 FLAGS (\\Seen) '
    b'INTERNALDATE "28-Jul-2026 18:31:00 -0300" RFC822.SIZE 1234 '
    b"BODY[HEADER.FIELDS (FROM TO SUBJECT)] {500}"
)


class ImapHelpersTest(unittest.TestCase):
    def test_modified_utf7_round_trip(self) -> None:
        original = "[Gmail]/Todos os e-mails & revisão"
        encoded = encode_modified_utf7(original)
        self.assertEqual(decode_modified_utf7(encoded), original)

    def test_parse_list_and_status(self) -> None:
        parsed = parse_list_line(
            b'(\\HasNoChildren \\All) "/" "[Gmail]/Todos os e-mails"'
        )
        self.assertEqual(parsed["special_use"], "\\All")
        self.assertEqual(parsed["remote_name"], "[Gmail]/Todos os e-mails")
        status = parse_status(
            [b'"[Gmail]/Todos os e-mails" (MESSAGES 250 UIDNEXT 900 UIDVALIDITY 5)']
        )
        self.assertEqual(
            status, {"messages_count": 250, "uidnext": 900, "uidvalidity": 5}
        )

    def test_copyuid_response_maps_source_to_trash_uids(self) -> None:
        self.assertEqual(
            parse_copyuid(
                [b"[COPYUID 77 42:43,50 100:101,200] completed"]
            ),
            {42: 100, 43: 101, 50: 200},
        )

    def test_uid_compression(self) -> None:
        self.assertEqual(compress_uid_set([1, 2, 3, 8, 10, 11]), "1:3,8,10:11")

    def test_capabilities_are_split_and_normalized(self) -> None:
        self.assertEqual(
            normalize_capabilities(
                [b"IMAP4rev1 UIDPLUS", "move x-gm-ext-1"]
            ),
            {"IMAP4REV1", "UIDPLUS", "MOVE", "X-GM-EXT-1"},
        )

    def test_mailbox_quoting(self) -> None:
        self.assertEqual(
            quote_mailbox('Projetos "Ativos"\\2026'),
            '"Projetos \\"Ativos\\"\\\\2026"',
        )

    def test_discovery_uses_valid_list_and_quoted_mailboxes(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.status_mailboxes: list[str] = []

            def list(self):
                return (
                    "OK",
                    [
                        b'(\\HasNoChildren \\Inbox) "/" "INBOX"',
                        b'(\\HasNoChildren) "/" "Projetos"',
                        b'(\\HasNoChildren \\All) "/" "[Gmail]/Todos os e-mails"',
                    ],
                )

            def status(self, mailbox: str, _fields: str):
                self.status_mailboxes.append(mailbox)
                return "OK", [b"(MESSAGES 10 UIDNEXT 11 UIDVALIDITY 1)"]

        class FakeConnection:
            def __init__(self) -> None:
                self.client = FakeClient()

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

        connection = FakeConnection()
        with patch(
            "mail_exporter.imap_service.ImapConnection",
            return_value=connection,
        ):
            folders = MailExtractor(None).discover_mailboxes({}, "senha")

        self.assertEqual(
            connection.client.status_mailboxes,
            ['"INBOX"', '"Projetos"', '"[Gmail]/Todos os e-mails"'],
        )
        self.assertFalse(folders[0]["selected"])
        self.assertFalse(folders[1]["selected"])
        self.assertTrue(folders[2]["selected"])

    def test_reader_converts_html_without_scripts(self) -> None:
        raw_message = b"""Subject: =?UTF-8?Q?Ol=C3=A1?=
From: Equipe <equipe@example.com>
To: Pessoa <pessoa@example.net>
Content-Type: text/html; charset=utf-8

<html><head><style>.x{display:none}</style></head>
<body><h1>Boas-vindas</h1><script>alert('x')</script>
<p>Texto &amp; detalhes.</p></body></html>
"""
        parsed = parse_message_for_reader(raw_message)
        self.assertEqual(parsed["subject"], "Olá")
        self.assertIn("Boas-vindas", parsed["body"])
        self.assertIn("Texto & detalhes.", parsed["body"])
        self.assertNotIn("alert", parsed["body"])
        self.assertNotIn("display:none", parsed["body"])

    def test_reader_uses_readonly_body_peek(self) -> None:
        raw_headers = b"""Subject: Teste
From: remetente@example.com

"""
        raw_body = b"Corpo da mensagem."

        class FakeClient:
            def __init__(self) -> None:
                self.select_calls: list[tuple[str, bool]] = []
                self.uid_calls: list[tuple[str, tuple]] = []

            def select(self, mailbox: str, readonly: bool = False):
                self.select_calls.append((mailbox, readonly))
                return "OK", []

            def uid(self, command: str, *args):
                self.uid_calls.append((command, args))
                if args[1] == "(BODY.PEEK[HEADER] BODYSTRUCTURE)":
                    return (
                        "OK",
                        [
                            (
                                b'1 (UID 42 BODYSTRUCTURE ("TEXT" "PLAIN" '
                                b'("CHARSET" "UTF-8") NIL NIL "7BIT" 18 1 '
                                b'NIL NIL NIL) BODY[HEADER] {52}',
                                raw_headers,
                            ),
                            b")",
                        ],
                    )
                return (
                    "OK",
                    [(b"1 (UID 42 BODY[1] {18}", raw_body), b")"],
                )

        class FakeConnection:
            def __init__(self) -> None:
                self.client = FakeClient()

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

        connection = FakeConnection()
        with patch(
            "mail_exporter.imap_service.ImapConnection",
            return_value=connection,
        ):
            parsed = MailExtractor(None).fetch_message_for_reader(
                {},
                "senha",
                {
                    "mailbox_name": "INBOX",
                    "uid": 42,
                    "size_bytes": 15 * 1024 * 1024,
                },
            )

        self.assertEqual(connection.client.select_calls, [('"INBOX"', True)])
        self.assertEqual(
            connection.client.uid_calls,
            [
                ("FETCH", ("42", "(BODY.PEEK[HEADER] BODYSTRUCTURE)")),
                ("FETCH", ("42", "(BODY.PEEK[1])")),
            ],
        )
        self.assertEqual(parsed["body"], "Corpo da mensagem.")

    def test_bodystructure_identifies_named_attachments(self) -> None:
        parsed = parse_bodystructure_fetch_item(
            b'1 (UID 42 BODYSTRUCTURE ('
            b'("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL '
            b'"7BIT" 120 5 NIL NIL NIL)'
            b'("APPLICATION" "PDF" ("NAME" "relatorio.pdf") NIL NIL '
            b'"BASE64" 2048 NIL '
            b'("ATTACHMENT" ("FILENAME" "relatorio.pdf")) NIL NIL)'
            b' "MIXED" ("BOUNDARY" "x") NIL NIL))'
        )
        self.assertEqual(parsed["uid"], 42)
        self.assertEqual(parsed["attachment_count"], 1)
        self.assertEqual(parsed["attachment_size_bytes"], 2048)
        self.assertEqual(
            parsed["attachments"][0],
            {
                "part_number": "2",
                "filename": "relatorio.pdf",
                "extension": "pdf",
                "content_type": "application/pdf",
                "disposition": "ATTACHMENT",
                "transfer_encoding": "base64",
                "size_bytes": 2048,
            },
        )

    def test_single_part_attachment_uses_section_one(self) -> None:
        parsed = parse_bodystructure_fetch_item(
            b'2 (UID 43 BODYSTRUCTURE '
            b'("APPLICATION" "ZIP" ("NAME" "backup.zip") NIL NIL '
            b'"BASE64" 4096 NIL '
            b'("ATTACHMENT" ("FILENAME" "backup.zip")) NIL NIL))'
        )
        self.assertEqual(parsed["attachment_count"], 1)
        self.assertEqual(parsed["attachments"][0]["part_number"], "1")
        self.assertEqual(parsed["attachments"][0]["extension"], "zip")

    def test_attachment_download_uses_body_peek_and_decodes_base64(self) -> None:
        class FakeClient:
            def select(self, _mailbox: str, readonly: bool = False):
                self.readonly = readonly
                return "OK", []

            def uid(self, command: str, *args):
                self.call = (command, args)
                return "OK", [
                    (b"1 (UID 42 BODY[2] {8}", b"Y29udGV1ZG8="),
                    b")",
                ]

        class FakeConnection:
            def __init__(self) -> None:
                self.client = FakeClient()

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

        connection = FakeConnection()
        with patch(
            "mail_exporter.imap_service.ImapConnection",
            return_value=connection,
        ):
            payload = MailExtractor(None).fetch_attachment(
                {},
                "senha",
                {"mailbox_name": "INBOX", "uid": 42},
                {
                    "part_number": "2",
                    "filename": "arquivo.txt",
                    "transfer_encoding": "base64",
                    "size_bytes": 12,
                },
            )
        self.assertTrue(connection.client.readonly)
        self.assertEqual(
            connection.client.call,
            ("FETCH", ("42", "(BODY.PEEK[2])")),
        )
        self.assertEqual(payload, b"conteudo")

    def test_large_attachment_download_reports_real_partial_progress(
        self,
    ) -> None:
        encoded = b"Y29udGV1ZG8="

        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str, tuple[Any, ...]]] = []

            def select(self, _mailbox: str, readonly: bool = False):
                self.readonly = readonly
                return "OK", []

            def uid(self, command: str, *args):
                self.calls.append((command, args))
                match = re.search(
                    rb"<(\d+)\.(\d+)>",
                    str(args[1]).encode(),
                )
                assert match is not None
                offset = int(match.group(1))
                amount = int(match.group(2))
                return "OK", [
                    (
                        b"1 (UID 42 BODY[2] {4}",
                        encoded[offset : offset + amount],
                    ),
                    b")",
                ]

        class FakeConnection:
            def __init__(self) -> None:
                self.client = FakeClient()

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

        connection = FakeConnection()
        reported: list[tuple[int, int]] = []
        with patch(
            "mail_exporter.imap_service.ImapConnection",
            return_value=connection,
        ):
            payload = MailExtractor(None).fetch_attachment(
                {},
                "senha",
                {"mailbox_name": "INBOX", "uid": 42},
                {
                    "part_number": "2",
                    "filename": "arquivo.txt",
                    "transfer_encoding": "base64",
                    "size_bytes": len(encoded),
                },
                progress=lambda current, total: reported.append(
                    (current, total)
                ),
                chunk_size=4,
            )
        self.assertEqual(payload, b"conteudo")
        self.assertEqual(
            reported,
            [(0, 12), (4, 12), (8, 12), (12, 12)],
        )

    def test_attachment_download_cancellation_stops_partial_fetch(
        self,
    ) -> None:
        encoded = b"Y29udGV1ZG8="
        cancel_event = threading.Event()

        class FakeClient:
            def __init__(self) -> None:
                self.calls = 0

            def select(self, _mailbox: str, readonly: bool = False):
                return "OK", []

            def uid(self, _command: str, *args):
                self.calls += 1
                match = re.search(rb"<(\d+)\.(\d+)>", str(args[1]).encode())
                assert match is not None
                offset = int(match.group(1))
                amount = int(match.group(2))
                return "OK", [
                    (
                        b"1 (UID 42 BODY[2] {4}",
                        encoded[offset : offset + amount],
                    ),
                    b")",
                ]

        class FakeConnection:
            def __init__(self) -> None:
                self.client = FakeClient()

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

        connection = FakeConnection()

        def progress(current: int, _total: int) -> None:
            if current == 4:
                cancel_event.set()

        with patch(
            "mail_exporter.imap_service.ImapConnection",
            return_value=connection,
        ):
            with self.assertRaises(AttachmentDownloadCancelled):
                MailExtractor(None).fetch_attachment(
                    {},
                    "senha",
                    {"mailbox_name": "INBOX", "uid": 42},
                    {
                        "part_number": "2",
                        "filename": "arquivo.txt",
                        "transfer_encoding": "base64",
                        "size_bytes": len(encoded),
                    },
                    progress=progress,
                    cancel_event=cancel_event,
                    chunk_size=4,
                )
        self.assertEqual(connection.client.calls, 1)

    def test_attachment_analysis_ignores_fetch_closing_tokens(self) -> None:
        class FakeClient:
            def select(self, _mailbox: str, readonly: bool = False):
                return "OK", []

            def uid(self, command: str, *_args):
                self.command = command
                return "OK", [
                    b'1 (UID 42 BODYSTRUCTURE ("APPLICATION" "PDF" '
                    b'("NAME" "arquivo.pdf") NIL NIL "BASE64" 100 NIL '
                    b'("ATTACHMENT" ("FILENAME" "arquivo.pdf")) NIL NIL))',
                    b")",
                ]

        class FakeConnection:
            def __init__(self) -> None:
                self.client = FakeClient()

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

        class FakeDatabase:
            def __init__(self) -> None:
                self.records: list[dict] = []

            def store_attachment_analysis(self, records):
                self.records.extend(records)

        database = FakeDatabase()
        events: list[dict] = []
        with patch(
            "mail_exporter.imap_service.ImapConnection",
            return_value=FakeConnection(),
        ):
            result = MailExtractor(database).analyze_attachments(
                {},
                "senha",
                [
                    {
                        "message_pk": 10,
                        "mailbox_name": "INBOX",
                        "uid": 42,
                    }
                ],
                events.append,
                threading.Event(),
                threading.Event(),
            )
        self.assertEqual(result["indexed"], 1)
        self.assertEqual(result["attachments"], 1)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(database.records[0]["message_pk"], 10)

    def test_sync_compares_all_uids_but_fetches_only_unknown_headers(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.fetch_sets: list[str] = []
                self.fetch_items: list[str] = []

            def select(self, _mailbox: str, readonly: bool = True):
                return "OK", []

            def uid(self, command: str, *args):
                if command.lower() == "search":
                    return "OK", [b"42 43"]
                if command.lower() == "fetch":
                    self.fetch_sets.append(str(args[0]))
                    self.fetch_items.append(str(args[1]))
                    metadata = (
                        SAMPLE_METADATA.replace(b"X-GM-MSGID 2002", b"X-GM-MSGID 2003")
                        .replace(b"UID 42", b"UID 43")
                    )
                    return "OK", [(metadata, SAMPLE_HEADER), b")"]
                raise AssertionError(command)

        class FakeConnection:
            def __init__(self) -> None:
                self.client = FakeClient()
                self.capabilities = {"X-GM-EXT-1"}
                self.gmail_extensions = True

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "sync.sqlite3")
            account_id = database.save_account(
                {
                    "display_name": "Conta",
                    "email": "conta@gmail.com",
                    "provider": "gmail",
                    "host": "imap.gmail.com",
                    "port": 993,
                    "security": "ssl",
                    "auth_type": "password",
                }
            )
            account = database.get_account(account_id)
            assert account is not None
            mailboxes = database.replace_mailboxes(
                account_id,
                [
                    {
                        "remote_name": "[Gmail]/Todos os e-mails",
                        "special_use": "\\All",
                        "flags": ["\\All"],
                        "messages_count": 1,
                        "uidnext": 43,
                        "uidvalidity": 1,
                        "selected": True,
                    }
                ],
            )
            existing = parse_fetch_item(
                SAMPLE_METADATA,
                SAMPLE_HEADER,
                mailboxes[0]["remote_name"],
                True,
            )
            database.store_batch(
                account_id,
                mailboxes[0]["id"],
                [existing],
                last_uid=42,
                uidvalidity=1,
            )
            connection = FakeConnection()
            events: list[dict] = []
            with patch(
                "mail_exporter.imap_service.ImapConnection",
                return_value=connection,
            ):
                result = MailExtractor(database).sync(
                    account,
                    "senha",
                    mailboxes,
                    events.append,
                    threading.Event(),
                    threading.Event(),
                )

        self.assertEqual(connection.client.fetch_sets, ["43"])
        self.assertEqual(len(connection.client.fetch_items), 1)
        self.assertIn("BODYSTRUCTURE", connection.client.fetch_items[0])
        self.assertIn("BODY.PEEK[HEADER.FIELDS", connection.client.fetch_items[0])
        self.assertEqual(result["checked"], 2)
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["missing"], 0)
        self.assertTrue(any(event["type"] == "reconciled" for event in events))

    def test_action_status_only_trash_does_not_download_old_headers(
        self,
    ) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.fetch_calls = 0

            def select(self, _mailbox: str, readonly: bool = True):
                return "OK", []

            def uid(self, command: str, *_args):
                if command.lower() == "search":
                    return "OK", [b"700 701 702"]
                if command.lower() == "fetch":
                    self.fetch_calls += 1
                    raise AssertionError("Trash headers must not be fetched")
                raise AssertionError(command)

        class FakeConnection:
            def __init__(self) -> None:
                self.client = FakeClient()
                self.capabilities: set[str] = set()
                self.gmail_extensions = False

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "trash-status.sqlite3")
            account_id = database.save_account(
                {
                    "display_name": "Conta",
                    "email": "conta@example.com",
                    "provider": "custom",
                    "host": "imap.example.com",
                    "port": 993,
                    "security": "ssl",
                    "auth_type": "password",
                }
            )
            account = database.get_account(account_id)
            assert account is not None
            mailboxes = database.replace_mailboxes(
                account_id,
                [
                    {
                        "remote_name": "Trash",
                        "special_use": "\\Trash",
                        "flags": ["\\Trash"],
                        "messages_count": 3,
                        "uidnext": 703,
                        "uidvalidity": 1,
                        "selected": False,
                    }
                ],
            )
            mailboxes[0]["action_status_only"] = True
            connection = FakeConnection()
            with patch(
                "mail_exporter.imap_service.ImapConnection",
                return_value=connection,
            ):
                result = MailExtractor(database).sync(
                    account,
                    "senha",
                    mailboxes,
                    lambda _event: None,
                    threading.Event(),
                    threading.Event(),
                )

        self.assertEqual(connection.client.fetch_calls, 0)
        self.assertEqual(result["checked"], 3)
        self.assertEqual(result["processed"], 0)

    def test_cleanup_uses_uid_move_to_the_discovered_trash(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[tuple] = []

            def select(self, mailbox: str, readonly: bool = False):
                self.calls.append(("select", mailbox, readonly))
                return "OK", []

            def uid(self, command: str, *args):
                self.calls.append(("uid", command, *args))
                return "OK", []

        class FakeConnection:
            def __init__(self) -> None:
                self.client = FakeClient()
                self.capabilities = {"MOVE", "UIDPLUS"}

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

        connection = FakeConnection()
        targets = [
            {
                "message_pk": 10,
                "mailbox_name": "[Gmail]/Todos os e-mails",
                "uid": 42,
            },
            {
                "message_pk": 11,
                "mailbox_name": "[Gmail]/Todos os e-mails",
                "uid": 43,
            },
        ]
        trash = {
            "remote_name": "[Gmail]/Lixeira",
            "special_use": "\\Trash",
        }
        with patch(
            "mail_exporter.imap_service.ImapConnection",
            return_value=connection,
        ):
            result = MailExtractor(None).move_to_trash(
                {}, "senha", targets, trash
            )

        self.assertEqual(result["message_ids"], [10, 11])
        self.assertEqual(result["status"], "completed")
        self.assertIn(
            (
                "uid",
                "MOVE",
                "42:43",
                '"[Gmail]/Lixeira"',
            ),
            connection.client.calls,
        )

    def test_cleanup_cancellation_stops_before_the_next_batch(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[tuple] = []

            def select(self, mailbox: str, readonly: bool = False):
                self.calls.append(("select", mailbox, readonly))
                return "OK", []

            def uid(self, command: str, *args):
                self.calls.append(("uid", command, *args))
                return "OK", []

        class FakeConnection:
            def __init__(self) -> None:
                self.client = FakeClient()
                self.capabilities = {"MOVE", "UIDPLUS"}

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

        connection = FakeConnection()
        cancel_event = threading.Event()

        def progress(event: dict) -> None:
            if event.get("message_ids"):
                cancel_event.set()

        with patch(
            "mail_exporter.imap_service.ImapConnection",
            return_value=connection,
        ):
            result = MailExtractor(None).move_to_trash(
                {},
                "senha",
                [
                    {
                        "message_pk": 10,
                        "mailbox_name": "INBOX",
                        "uid": 42,
                    },
                    {
                        "message_pk": 11,
                        "mailbox_name": "INBOX",
                        "uid": 43,
                    },
                ],
                {"remote_name": "Trash", "special_use": "\\Trash"},
                progress,
                batch_size=1,
                cancel_event=cancel_event,
            )

        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["message_ids"], [10])
        move_calls = [
            call
            for call in connection.client.calls
            if call[:2] == ("uid", "MOVE")
        ]
        self.assertEqual(len(move_calls), 1)

    def test_gmail_cleanup_uses_the_native_trash_label(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[tuple] = []

            def select(self, mailbox: str, readonly: bool = False):
                self.calls.append(("select", mailbox, readonly))
                return "OK", []

            def uid(self, command: str, *args):
                self.calls.append(("uid", command, *args))
                return "OK", []

        class FakeConnection:
            def __init__(self) -> None:
                self.client = FakeClient()
                self.capabilities = {"MOVE", "UIDPLUS"}
                self.gmail_extensions = True

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

        connection = FakeConnection()
        with patch(
            "mail_exporter.imap_service.ImapConnection",
            return_value=connection,
        ):
            result = MailExtractor(None).move_to_trash(
                {"provider": "gmail"},
                "senha",
                [
                    {
                        "message_pk": 10,
                        "mailbox_name": "[Gmail]/Todos os e-mails",
                        "uid": 42,
                        "provider_message_id": "2002",
                        "labels_json": json.dumps(
                            ["\\Inbox", "Cliente VIP"]
                        ),
                    }
                ],
                {
                    "remote_name": "[Gmail]/Lixeira",
                    "special_use": "\\Trash",
                },
            )

        self.assertEqual(result["message_ids"], [10])
        self.assertTrue(result["undo_supported"])
        self.assertEqual(
            result["undo_items"][0]["provider_message_id"],
            "2002",
        )
        self.assertIn(
            (
                "uid",
                "STORE",
                "42",
                "+X-GM-LABELS",
                r"(\Trash)",
            ),
            connection.client.calls,
        )
        self.assertFalse(
            any(
                call[:2] in {
                    ("uid", "MOVE"),
                    ("uid", "EXPUNGE"),
                }
                for call in connection.client.calls
            )
        )

    def test_gmail_cleanup_uses_message_id_as_safe_undo_fallback(
        self,
    ) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[tuple] = []

            def select(self, mailbox: str, readonly: bool = False):
                self.calls.append(("select", mailbox, readonly))
                return "OK", []

            def uid(self, command: str, *args):
                self.calls.append(("uid", command, *args))
                return "OK", []

        class FakeConnection:
            def __init__(self) -> None:
                self.client = FakeClient()
                self.capabilities = {"MOVE", "UIDPLUS"}
                self.gmail_extensions = True

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

        connection = FakeConnection()
        with patch(
            "mail_exporter.imap_service.ImapConnection",
            return_value=connection,
        ):
            result = MailExtractor(None).move_to_trash(
                {"provider": "gmail"},
                "senha",
                [
                    {
                        "message_pk": 10,
                        "mailbox_name": "[Gmail]/Todos os e-mails",
                        "uid": 42,
                        "message_id": "<unique-message@example.com>",
                        "labels_json": json.dumps(["\\Inbox"]),
                    }
                ],
                {
                    "remote_name": "[Gmail]/Lixeira",
                    "special_use": "\\Trash",
                },
            )

        self.assertTrue(result["undo_supported"])
        self.assertEqual(result["undo_available"], 1)
        self.assertEqual(
            result["undo_items"][0],
            {
                "strategy": "gmail_header",
                "message_pk": 10,
                "source_mailbox": "[Gmail]/Todos os e-mails",
                "labels_json": json.dumps(["\\Inbox"]),
                "message_id": "<unique-message@example.com>",
            },
        )

    def test_gmail_undo_finds_the_message_and_restores_labels(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[tuple] = []

            def select(self, mailbox: str, readonly: bool = False):
                self.calls.append(("select", mailbox, readonly))
                return "OK", []

            def uid(self, command: str, *args):
                self.calls.append(("uid", command, *args))
                if command == "SEARCH":
                    if "X-GM-MSGID" in args:
                        return "OK", [b"777"]
                    return "OK", [b"778"]
                return "OK", []

        class FakeConnection:
            def __init__(self) -> None:
                self.client = FakeClient()
                self.capabilities = {"MOVE", "UIDPLUS"}
                self.gmail_extensions = True

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

        connection = FakeConnection()
        with patch(
            "mail_exporter.imap_service.ImapConnection",
            return_value=connection,
        ):
            result = MailExtractor(None).restore_from_trash(
                {"provider": "gmail"},
                "senha",
                [
                    {
                        "strategy": "gmail_msgid",
                        "message_pk": 10,
                        "provider_message_id": "2002",
                        "labels_json": json.dumps(
                            ["\\Inbox", "Cliente VIP"]
                        ),
                    },
                    {
                        "strategy": "gmail_header",
                        "message_pk": 11,
                        "message_id": "<unique-message@example.com>",
                        "labels_json": json.dumps(["\\Inbox"]),
                    },
                ],
                {
                    "remote_name": "[Gmail]/Lixeira",
                    "special_use": "\\Trash",
                },
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["message_ids"], [10, 11])
        self.assertIn(
            ("uid", "SEARCH", None, "X-GM-MSGID", "2002"),
            connection.client.calls,
        )
        self.assertIn(
            (
                "uid",
                "SEARCH",
                None,
                "HEADER",
                "Message-ID",
                '"<unique-message@example.com>"',
            ),
            connection.client.calls,
        )
        add_labels = next(
            call
            for call in connection.client.calls
            if call[:4] == ("uid", "STORE", "777", "+X-GM-LABELS")
        )
        remove_trash = next(
            call
            for call in connection.client.calls
            if call[:4] == ("uid", "STORE", "777", "-X-GM-LABELS")
        )
        self.assertIn("Cliente VIP", add_labels[4])
        self.assertEqual(remove_trash[4], r"(\Trash)")

    def test_gmail_cleanup_falls_back_to_copy_without_expunge(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[tuple] = []

            def select(self, mailbox: str, readonly: bool = False):
                self.calls.append(("select", mailbox, readonly))
                return "OK", []

            def uid(self, command: str, *args):
                self.calls.append(("uid", command, *args))
                if command == "STORE":
                    return "NO", [b"System label rejected"]
                return "OK", []

        class FakeConnection:
            def __init__(self) -> None:
                self.client = FakeClient()
                self.capabilities = {"MOVE"}
                self.gmail_extensions = True

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

        connection = FakeConnection()
        with patch(
            "mail_exporter.imap_service.ImapConnection",
            return_value=connection,
        ):
            result = MailExtractor(None).move_to_trash(
                {"provider": "gmail"},
                "senha",
                [
                    {
                        "message_pk": 10,
                        "mailbox_name": "[Gmail]/Todos os e-mails",
                        "uid": 42,
                    }
                ],
                {
                    "remote_name": "[Gmail]/Lixeira",
                    "special_use": "\\Trash",
                },
            )

        self.assertEqual(result["message_ids"], [10])
        self.assertIn(
            (
                "uid",
                "COPY",
                "42",
                '"[Gmail]/Lixeira"',
            ),
            connection.client.calls,
        )
        self.assertFalse(
            any(
                call[:2] in {
                    ("uid", "MOVE"),
                    ("uid", "EXPUNGE"),
                }
                for call in connection.client.calls
            )
        )

    def test_gmail_cleanup_error_does_not_dump_the_uid_set(self) -> None:
        class FakeClient:
            def select(self, _mailbox: str, readonly: bool = False):
                return "OK", []

            def uid(self, _command: str, *_args):
                return "NO", [b"Operation rejected"]

        class FakeConnection:
            def __init__(self) -> None:
                self.client = FakeClient()
                self.capabilities = {"MOVE"}
                self.gmail_extensions = True

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

        with patch(
            "mail_exporter.imap_service.ImapConnection",
            return_value=FakeConnection(),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "2 mensagens",
            ) as raised:
                MailExtractor(None).move_to_trash(
                    {"provider": "gmail"},
                    "senha",
                    [
                        {
                            "message_pk": 10,
                            "mailbox_name": "[Gmail]/Todos os e-mails",
                            "uid": 235873,
                        },
                        {
                            "message_pk": 11,
                            "mailbox_name": "[Gmail]/Todos os e-mails",
                            "uid": 369466,
                        },
                    ],
                    {
                        "remote_name": "[Gmail]/Lixeira",
                        "special_use": "\\Trash",
                    },
                )
        self.assertNotIn("235873", str(raised.exception))
        self.assertNotIn("369466", str(raised.exception))

    def test_cleanup_refuses_unsafe_global_expunge_fallback(self) -> None:
        class FakeConnection:
            def __init__(self) -> None:
                self.client = object()
                self.capabilities: set[str] = set()

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

        with patch(
            "mail_exporter.imap_service.ImapConnection",
            return_value=FakeConnection(),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "mecanismo de Lixeira",
            ):
                MailExtractor(None).move_to_trash(
                    {},
                    "senha",
                    [
                        {
                            "message_pk": 10,
                            "mailbox_name": "INBOX",
                            "uid": 42,
                        }
                    ],
                    {"remote_name": "Trash", "special_use": "\\Trash"},
                )


class HeaderParsingTest(unittest.TestCase):
    def test_parse_selected_headers(self) -> None:
        record = parse_fetch_item(
            SAMPLE_METADATA, SAMPLE_HEADER, "[Gmail]/Todos os e-mails", True
        )
        self.assertEqual(record["uid"], 42)
        self.assertEqual(record["provider_message_id"], "2002")
        self.assertEqual(record["provider_thread_id"], "1001")
        self.assertEqual(record["from_name"], "João da Silva")
        self.assertEqual(record["from_email"], "joao@exemplo.com")
        self.assertEqual(record["from_domain"], "exemplo.com")
        self.assertEqual(record["subject"], "Confirmação do pedido")
        self.assertEqual(record["date_sent_utc"], "2026-07-28T21:30:00+00:00")
        self.assertEqual(len(record["recipients"]), 3)
        self.assertEqual(
            json.loads(record["labels_json"]),
            ["\\Inbox", "Cliente VIP", "Projetos (2026)"],
        )


class SecretCipherTest(unittest.TestCase):
    def test_round_trip_and_wrong_password(self) -> None:
        cipher = SecretCipher()
        encrypted = cipher.encrypt("senha-imap-de-teste", "senha-local-segura")
        self.assertNotIn("senha-imap-de-teste", encrypted)
        self.assertEqual(
            cipher.decrypt(encrypted, "senha-local-segura"),
            "senha-imap-de-teste",
        )
        with self.assertRaises(InvalidAccountPassword):
            cipher.decrypt(encrypted, "senha-local-incorreta")

    def test_credentials_created_by_version_one_remain_compatible(self) -> None:
        cipher = SecretCipher()
        with patch.object(SecretCipher, "VERSION", 1):
            legacy = cipher.encrypt("senha-imap-antiga", "senha-local-segura")
        self.assertEqual(
            cipher.decrypt(legacy, "senha-local-segura"),
            "senha-imap-antiga",
        )


class IdentityAndPresetTest(unittest.TestCase):
    def test_provider_presets_are_secure_and_exclude_oauth_only_services(
        self,
    ) -> None:
        providers = load_provider_presets()
        by_id = {item["id"]: item for item in providers}
        self.assertTrue(
            {
                "gmail",
                "uol",
                "bol",
                "terra",
                "yahoo",
                "icloud",
                "aol",
                "gmx",
                "mailru",
                "generic",
            }.issubset(by_id)
        )
        self.assertEqual(by_id["uol"]["host"], "imap.uol.com.br")
        self.assertEqual(by_id["terra"]["host"], "imap.terra.com.br")
        self.assertTrue(
            all(
                item["port"] == 993 and item["security"] == "ssl"
                for item in providers
            )
        )
        provider_text = json.dumps(providers).lower()
        self.assertNotIn("hotmail", provider_text)
        self.assertNotIn("outlook", provider_text)

    def test_locale_files_cover_every_static_app_translation(self) -> None:
        root = Path(__file__).parents[1]
        source = (root / "app.py").read_text(encoding="utf-8")
        import ast

        tree = ast.parse(source)
        required: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_"
                and node.args
            ):
                try:
                    value = ast.literal_eval(node.args[0])
                except (ValueError, TypeError):
                    continue
                if isinstance(value, str):
                    required.add(value)
        for language in ("pt_BR", "en"):
            messages = json.loads(
                (root / "locales" / f"{language}.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertFalse(required - messages.keys())

    def test_language_can_be_changed_without_persisting(self) -> None:
        previous = get_language()
        try:
            set_language("en", persist=False)
            self.assertEqual(tr("Adicionar conta"), "Add account")
            set_language("pt_BR", persist=False)
            self.assertEqual(tr("Adicionar conta"), "Adicionar conta")
        finally:
            set_language(previous, persist=False)

    def test_legacy_data_directory_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            legacy = Path(temporary) / "gmail-header-exporter"
            legacy.mkdir()
            with patch.dict(
                os.environ,
                {"XDG_DATA_HOME": temporary},
                clear=False,
            ), patch.dict(
                os.environ,
                {"IMAP_EXPORTER_DATA_DIR": ""},
                clear=False,
            ):
                self.assertEqual(data_dir(), legacy)


class DatabaseAndExportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = Database(self.root / "test.sqlite3")
        self.account_id = self.database.save_account(
            {
                "display_name": "Conta teste",
                "email": "conta@gmail.com",
                "provider": "gmail",
                "host": "imap.gmail.com",
                "port": 993,
                "security": "ssl",
                "auth_type": "password",
                "encrypted_secret": '{"test":true}',
            }
        )
        mailboxes = self.database.replace_mailboxes(
            self.account_id,
            [
                {
                    "remote_name": "[Gmail]/Todos os e-mails",
                    "delimiter": "/",
                    "special_use": "\\All",
                    "flags": ["\\All"],
                    "messages_count": 1,
                    "uidnext": 43,
                    "uidvalidity": 1,
                    "selected": True,
                }
            ],
        )
        self.mailbox_id = mailboxes[0]["id"]
        record = parse_fetch_item(
            SAMPLE_METADATA, SAMPLE_HEADER, "[Gmail]/Todos os e-mails", True
        )
        self.database.store_batch(
            self.account_id,
            self.mailbox_id,
            [record],
            last_uid=42,
            uidvalidity=1,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_database_summary(self) -> None:
        summary = self.database.account_summary(self.account_id)
        self.assertEqual(summary["messages"], 1)
        self.assertEqual(summary["domains"], 1)

    def test_attachment_index_supports_largest_filter_and_direct_export(
        self,
    ) -> None:
        message = next(self.database.iter_messages([self.account_id]))
        self.database.store_attachment_analysis(
            [
                {
                    "message_pk": message["id"],
                    "attachments": [
                        {
                            "part_number": "2",
                            "filename": "manual.pdf",
                            "extension": "pdf",
                            "content_type": "application/pdf",
                            "disposition": "ATTACHMENT",
                            "transfer_encoding": "base64",
                            "size_bytes": 900,
                        }
                    ],
                }
            ]
        )
        rows = self.database.largest_messages(
            self.account_id,
            extensions=["pdf", "zip"],
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["attachment_count"], 1)
        self.assertEqual(rows[0]["attachment_names"], "manual.pdf")
        self.assertEqual(
            self.database.largest_messages(
                self.account_id,
                extensions=["zip", "rar"],
            ),
            [],
        )
        destination = self.root / "largest.csv"
        export_csv(
            self.database,
            [self.account_id],
            destination,
            message_ids=[message["id"]],
        )
        with destination.open(encoding="utf-8-sig", newline="") as stream:
            exported = list(csv.reader(stream, delimiter=";"))
        self.assertEqual(len(exported), 2)
        self.assertIn("Nomes dos anexos", exported[0])
        self.assertIn("manual.pdf", exported[1])

    def test_existing_033_database_is_migrated_before_state_index(self) -> None:
        legacy_path = self.root / "legacy-033.sqlite3"
        connection = sqlite3.connect(legacy_path)
        connection.executescript(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                account_id INTEGER NOT NULL,
                provider_message_id TEXT NOT NULL,
                from_email TEXT,
                from_domain TEXT,
                date_sent_utc TEXT,
                labels_json TEXT NOT NULL DEFAULT '[]',
                trashed_at TEXT,
                first_seen_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE message_mailboxes (
                message_id_fk INTEGER NOT NULL,
                mailbox_id INTEGER NOT NULL,
                uid INTEGER NOT NULL,
                uidvalidity INTEGER,
                PRIMARY KEY(message_id_fk, mailbox_id)
            );
            """
        )
        connection.close()

        migrated = Database(legacy_path)
        with migrated.connect() as current:
            message_columns = {
                row["name"]
                for row in current.execute("PRAGMA table_info(messages)")
            }
            mapping_columns = {
                row["name"]
                for row in current.execute(
                    "PRAGMA table_info(message_mailboxes)"
                )
            }
        self.assertTrue(
            {"state", "last_seen_at", "missing_since"} <= message_columns
        )
        self.assertTrue(
            {"last_seen_at", "missing_since"} <= mapping_columns
        )

    def test_subject_messages_filter_search_and_paginate_metadata(self) -> None:
        sender_rows = self.database.subject_messages(
            self.account_id,
            sender_email="JOAO@EXEMPLO.COM",
            limit=10,
        )
        self.assertEqual(len(sender_rows), 1)
        self.assertEqual(sender_rows[0]["subject"], "Confirmação do pedido")
        self.assertEqual(sender_rows[0]["source_mailbox"], "[Gmail]/Todos os e-mails")
        self.assertNotIn("body", sender_rows[0])

        domain_rows = self.database.subject_messages(
            self.account_id,
            domain="exemplo.com",
            search="pedido",
            limit=1,
        )
        self.assertEqual(len(domain_rows), 1)
        self.assertEqual(
            self.database.subject_messages(
                self.account_id,
                domain="exemplo.com",
                search="não existe",
            ),
            [],
        )
        self.assertEqual(
            self.database.count_subject_messages(
                self.account_id,
                sender_email="joao@exemplo.com",
            ),
            1,
        )
        self.assertEqual(
            len(
                self.database.subject_messages(
                    self.account_id,
                    sender_email="joao@exemplo.com",
                    date_from="2026-07-28",
                    date_to="2026-07-28",
                )
            ),
            1,
        )
        self.assertEqual(
            self.database.subject_messages(
                self.account_id,
                sender_email="joao@exemplo.com",
                date_from="2026-07-29",
            ),
            [],
        )
        self.assertEqual(
            self.database.count_subject_messages(
                self.account_id,
                domain="exemplo.com",
                search="não existe",
                date_to="2026-07-28",
            ),
            0,
        )
        self.assertEqual(
            self.database.subject_messages(
                self.account_id,
                sender_email="joao@exemplo.com",
                limit=1,
                offset=1,
            ),
            [],
        )

    def test_message_reader_target_resolves_current_uid(self) -> None:
        message = self.database.subject_messages(
            self.account_id,
            sender_email="joao@exemplo.com",
            limit=1,
        )[0]
        target = self.database.message_reader_target(
            self.account_id,
            int(message["id"]),
        )
        assert target is not None
        self.assertEqual(
            target["mailbox_name"],
            "[Gmail]/Todos os e-mails",
        )
        self.assertEqual(target["uid"], 42)
        self.assertEqual(target["mailbox_uidvalidity"], 1)
        self.assertEqual(target["mapping_uidvalidity"], 1)

    def test_special_use_mailboxes_are_ordered_with_all_first(self) -> None:
        folders = self.database.replace_mailboxes(
            self.account_id,
            [
                {
                    "remote_name": "Projetos",
                    "special_use": None,
                    "flags": [],
                    "messages_count": 2,
                },
                {
                    "remote_name": "INBOX",
                    "special_use": "\\Inbox",
                    "flags": ["\\Inbox"],
                    "messages_count": 3,
                },
                {
                    "remote_name": "[Gmail]/Todos os e-mails",
                    "special_use": "\\All",
                    "flags": ["\\All"],
                    "messages_count": 4,
                    "uidvalidity": 1,
                },
            ],
        )
        self.assertEqual(
            [folder["special_use"] for folder in folders],
            ["\\All", "\\Inbox", None],
        )

    def test_cleanup_summary_deduplicates_sender_and_domain_selection(self) -> None:
        senders = self.database.sender_summary(self.account_id)
        self.assertEqual(senders[0]["email"], "joao@exemplo.com")
        self.assertEqual(senders[0]["messages"], 1)

        preview = self.database.cleanup_preview(
            self.account_id,
            ["joao@exemplo.com"],
            ["exemplo.com"],
        )
        self.assertEqual(preview["messages"], 1)
        targets = self.database.cleanup_targets(
            self.account_id,
            ["joao@exemplo.com"],
            ["exemplo.com"],
        )
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["uid"], 42)

        self.database.mark_messages_trashed([targets[0]["message_pk"]])
        self.assertEqual(
            self.database.cleanup_preview(
                self.account_id, ["joao@exemplo.com"], ["exemplo.com"]
            )["messages"],
            0,
        )
        self.assertEqual(
            self.database.account_summary(self.account_id)["messages"], 0
        )
        with self.database.connect() as connection:
            message = connection.execute(
                "SELECT state FROM messages WHERE account_id=?",
                (self.account_id,),
            ).fetchone()
            mapping = connection.execute(
                """
                SELECT missing_since FROM message_mailboxes
                 WHERE message_id_fk=(
                     SELECT id FROM messages WHERE account_id=?
                 )
                """,
                (self.account_id,),
            ).fetchone()
        self.assertEqual(message["state"], "trashed")
        self.assertIsNotNone(mapping["missing_since"])

    def test_mailbox_snapshot_marks_missing_and_restored_messages(self) -> None:
        missing = self.database.reconcile_mailbox_snapshot(
            self.mailbox_id,
            [],
            1,
        )
        self.assertEqual(missing["missing"], 1)
        counts = self.database.recompute_account_states(self.account_id)
        self.assertEqual(counts["missing"], 1)
        self.assertEqual(
            self.database.account_summary(self.account_id)["messages"],
            0,
        )

        restored = self.database.reconcile_mailbox_snapshot(
            self.mailbox_id,
            [42],
            1,
        )
        self.assertEqual(restored["restored"], 1)
        counts = self.database.recompute_account_states(self.account_id)
        self.assertEqual(counts["active"], 1)
        self.assertEqual(
            self.database.account_summary(self.account_id)["messages"],
            1,
        )

    def test_local_undo_waits_for_sync_before_reusing_messages(self) -> None:
        targets = self.database.cleanup_targets(
            self.account_id,
            ["joao@exemplo.com"],
            [],
        )
        message_id = int(targets[0]["message_pk"])
        self.database.mark_messages_trashed([message_id])
        self.database.mark_messages_restored([message_id])
        with self.database.connect() as connection:
            message = connection.execute(
                "SELECT state, trashed_at, missing_since "
                "FROM messages WHERE id=?",
                (message_id,),
            ).fetchone()
            mappings = connection.execute(
                "SELECT COUNT(*) AS amount FROM message_mailboxes "
                "WHERE message_id_fk=?",
                (message_id,),
            ).fetchone()["amount"]
        self.assertEqual(message["state"], "missing")
        self.assertIsNone(message["trashed_at"])
        self.assertIsNotNone(message["missing_since"])
        self.assertEqual(mappings, 0)
        self.assertEqual(self.database.sender_summary(self.account_id), [])
        self.assertEqual(
            self.database.cleanup_targets(
                self.account_id,
                ["joao@exemplo.com"],
                [],
            ),
            [],
        )

        refreshed = parse_fetch_item(
            SAMPLE_METADATA.replace(b"UID 42", b"UID 84"),
            SAMPLE_HEADER,
            "[Gmail]/Todos os e-mails",
            True,
        )
        self.database.store_batch(
            self.account_id,
            self.mailbox_id,
            [refreshed],
            last_uid=84,
            uidvalidity=1,
        )
        resynced_targets = self.database.cleanup_targets(
            self.account_id,
            ["joao@exemplo.com"],
            [],
        )
        self.assertEqual(len(resynced_targets), 1)
        self.assertEqual(resynced_targets[0]["uid"], 84)

    def test_mini_sync_reconciles_move_then_undo_with_a_new_uid(
        self,
    ) -> None:
        source_name = "[Gmail]/Todos os e-mails"
        trash_name = "[Gmail]/Lixeira"
        mailboxes = self.database.replace_mailboxes(
            self.account_id,
            [
                {
                    "remote_name": source_name,
                    "delimiter": "/",
                    "special_use": "\\All",
                    "flags": ["\\All"],
                    "messages_count": 1,
                    "uidnext": 43,
                    "uidvalidity": 1,
                    "selected": True,
                },
                {
                    "remote_name": trash_name,
                    "delimiter": "/",
                    "special_use": "\\Trash",
                    "flags": ["\\Trash"],
                    "messages_count": 0,
                    "uidnext": 1,
                    "uidvalidity": 1,
                    "selected": False,
                },
            ],
        )
        source_mailbox = next(
            item for item in mailboxes if item["remote_name"] == source_name
        )
        targets = self.database.cleanup_targets(
            self.account_id,
            ["joao@exemplo.com"],
            [],
        )
        message_id = int(targets[0]["message_pk"])
        account = self.database.get_account(self.account_id)
        assert account is not None

        class FakeClient:
            def __init__(
                self,
                snapshots: dict[str, list[int]],
                fetch_uid: int | None = None,
            ) -> None:
                self.snapshots = snapshots
                self.fetch_uid = fetch_uid
                self.selected = ""
                self.fetch_sets: list[str] = []

            def select(self, mailbox: str, readonly: bool = True):
                self.selected = mailbox.strip('"')
                return "OK", []

            def uid(self, command: str, *args):
                if command.lower() == "search":
                    values = self.snapshots.get(self.selected, [])
                    return "OK", [
                        " ".join(str(value) for value in values).encode()
                    ]
                if command.lower() == "fetch":
                    uid_set = str(args[0])
                    self.fetch_sets.append(uid_set)
                    if self.fetch_uid is None:
                        raise AssertionError("Unexpected header fetch")
                    metadata = SAMPLE_METADATA.replace(
                        b"UID 42",
                        f"UID {self.fetch_uid}".encode(),
                    )
                    return "OK", [(metadata, SAMPLE_HEADER), b")"]
                raise AssertionError(command)

        class FakeConnection:
            def __init__(self, client: FakeClient) -> None:
                self.client = client
                self.capabilities = {"X-GM-EXT-1"}
                self.gmail_extensions = True

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

        self.database.mark_messages_trashed([message_id])
        moved_client = FakeClient(
            {
                source_name: [],
                trash_name: [777],
            }
        )
        action_mailboxes = (
            self.database.get_action_reconciliation_mailboxes(
                self.account_id,
                {source_name},
            )
        )
        with patch(
            "mail_exporter.imap_service.ImapConnection",
            return_value=FakeConnection(moved_client),
        ):
            MailExtractor(self.database).sync(
                account,
                "senha",
                action_mailboxes,
                lambda _event: None,
                threading.Event(),
                threading.Event(),
            )
        self.assertEqual(moved_client.fetch_sets, [])
        self.assertEqual(
            self.database.cleanup_targets(
                self.account_id,
                ["joao@exemplo.com"],
                [],
            ),
            [],
        )

        self.database.mark_messages_restored([message_id])
        restored_client = FakeClient(
            {
                source_name: [84],
                trash_name: [],
            },
            fetch_uid=84,
        )
        action_mailboxes = (
            self.database.get_action_reconciliation_mailboxes(
                self.account_id,
                {source_name},
            )
        )
        with patch(
            "mail_exporter.imap_service.ImapConnection",
            return_value=FakeConnection(restored_client),
        ):
            MailExtractor(self.database).sync(
                account,
                "senha",
                action_mailboxes,
                lambda _event: None,
                threading.Event(),
                threading.Event(),
            )
        self.assertEqual(restored_client.fetch_sets, ["84"])
        refreshed = self.database.cleanup_targets(
            self.account_id,
            ["joao@exemplo.com"],
            [],
        )
        self.assertEqual(len(refreshed), 1)
        self.assertEqual(refreshed[0]["uid"], 84)
        self.assertEqual(
            int(refreshed[0]["mailbox_id"]),
            int(source_mailbox["id"]),
        )

    def test_startup_repairs_legacy_active_message_without_uid(self) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "DELETE FROM message_mailboxes WHERE message_id_fk IN "
                "(SELECT id FROM messages WHERE account_id=?)",
                (self.account_id,),
            )
            connection.execute(
                "UPDATE messages SET state='active', missing_since=NULL "
                "WHERE account_id=?",
                (self.account_id,),
            )

        repaired = Database(self.database.path)
        with repaired.connect() as connection:
            message = connection.execute(
                "SELECT state, missing_since FROM messages "
                "WHERE account_id=?",
                (self.account_id,),
            ).fetchone()
        self.assertEqual(message["state"], "missing")
        self.assertIsNotNone(message["missing_since"])
        self.assertEqual(repaired.sender_summary(self.account_id), [])

    def test_action_reconciliation_uses_only_sources_and_trash(self) -> None:
        mailboxes = self.database.replace_mailboxes(
            self.account_id,
            [
                {
                    "remote_name": "INBOX",
                    "delimiter": "/",
                    "special_use": "\\Inbox",
                    "flags": ["\\Inbox"],
                    "messages_count": 1,
                    "uidnext": 2,
                    "uidvalidity": 1,
                    "selected": True,
                },
                {
                    "remote_name": "Archive",
                    "delimiter": "/",
                    "special_use": None,
                    "flags": [],
                    "messages_count": 1,
                    "uidnext": 2,
                    "uidvalidity": 1,
                    "selected": True,
                },
                {
                    "remote_name": "Sent",
                    "delimiter": "/",
                    "special_use": "\\Sent",
                    "flags": ["\\Sent"],
                    "messages_count": 1,
                    "uidnext": 2,
                    "uidvalidity": 1,
                    "selected": False,
                },
                {
                    "remote_name": "Trash",
                    "delimiter": "/",
                    "special_use": "\\Trash",
                    "flags": ["\\Trash"],
                    "messages_count": 0,
                    "uidnext": 1,
                    "uidvalidity": 1,
                    "selected": False,
                },
            ],
        )
        self.assertEqual(len(mailboxes), 4)
        selected = self.database.get_action_reconciliation_mailboxes(
            self.account_id,
            {"Archive"},
        )
        self.assertEqual(
            {item["remote_name"] for item in selected},
            {"Archive", "Trash"},
        )
        trash = next(
            item for item in selected if item["remote_name"] == "Trash"
        )
        self.assertTrue(trash["action_status_only"])

        fallback = self.database.get_action_reconciliation_mailboxes(
            self.account_id,
            set(),
        )
        self.assertEqual(
            {item["remote_name"] for item in fallback},
            {"INBOX", "Archive", "Trash"},
        )

    def test_rebuild_index_preserves_account_and_folder_selection(self) -> None:
        self.database.rebuild_account_index(self.account_id)
        self.assertIsNotNone(self.database.get_account(self.account_id))
        folders = self.database.get_mailboxes(self.account_id)
        self.assertEqual(len(folders), 1)
        self.assertEqual(folders[0]["selected"], 1)
        self.assertEqual(folders[0]["last_uid"], 0)
        self.assertEqual(
            self.database.account_summary(self.account_id)["messages"],
            0,
        )

    def test_cleanup_summary_ignores_sent_messages(self) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE messages SET labels_json=? WHERE account_id=?",
                (json.dumps(["\\Sent"]), self.account_id),
            )
        self.assertEqual(self.database.sender_summary(self.account_id), [])

    def test_delete_account_cascades_to_local_data(self) -> None:
        self.database.delete_account(self.account_id)
        self.assertIsNone(self.database.get_account(self.account_id))
        with self.database.connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM messages WHERE account_id=?",
                    (self.account_id,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM mailboxes WHERE account_id=?",
                    (self.account_id,),
                ).fetchone()[0],
                0,
            )

        replacement_id = self.database.save_account(
            {
                "display_name": "Conta cadastrada novamente",
                "email": "conta@gmail.com",
                "provider": "gmail",
                "host": "imap.gmail.com",
                "port": 993,
                "security": "ssl",
                "auth_type": "password",
                "encrypted_secret": '{"replacement":true}',
            }
        )
        self.assertNotEqual(replacement_id, self.account_id)
        self.assertIsNotNone(self.database.get_account(replacement_id))

    def test_csv_export(self) -> None:
        destination = self.root / "saida.csv"
        export_csv(self.database, [self.account_id], destination)
        with destination.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.reader(stream, delimiter=";"))
        self.assertEqual(len(rows), 2)
        self.assertIn("Domínio do remetente", rows[0])
        self.assertIn("exemplo.com", rows[1])

    def test_csv_export_can_use_the_cleanup_selection(self) -> None:
        destination = self.root / "selecao.csv"
        export_csv(
            self.database,
            [self.account_id],
            destination,
            sender_emails=["joao@exemplo.com"],
            domains=["exemplo.com"],
        )
        with destination.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.reader(stream, delimiter=";"))
        self.assertEqual(len(rows), 2)
        self.assertIn("joao@exemplo.com", rows[1])

        empty_destination = self.root / "selecao-vazia.csv"
        export_csv(
            self.database,
            [self.account_id],
            empty_destination,
            sender_emails=["outro@exemplo.com"],
            domains=[],
        )
        with empty_destination.open(
            encoding="utf-8-sig",
            newline="",
        ) as stream:
            self.assertEqual(len(list(csv.reader(stream, delimiter=";"))), 1)

    def test_ods_export_is_valid_zip_and_xml(self) -> None:
        destination = self.root / "saida.ods"
        export_ods(self.database, [self.account_id], destination)
        with zipfile.ZipFile(destination) as archive:
            self.assertEqual(
                archive.read("mimetype"),
                b"application/vnd.oasis.opendocument.spreadsheet",
            )
            content = archive.read("content.xml")
            ElementTree.fromstring(content)
            self.assertIn(b'table:name="Remetentes"', content)
            self.assertIn("META-INF/manifest.xml", archive.namelist())

    def test_ods_export_can_use_a_domain_selection(self) -> None:
        destination = self.root / "selecao.ods"
        export_ods(
            self.database,
            [self.account_id],
            destination,
            sender_emails=[],
            domains=["exemplo.com"],
        )
        with zipfile.ZipFile(destination) as archive:
            content = archive.read("content.xml").decode("utf-8")
        self.assertIn("joao@exemplo.com", content)
        self.assertIn('table:name="Mensagens"', content)
        self.assertIn('table:name="Destinatários"', content)


class WindowLayoutTest(unittest.TestCase):
    def _app_source(self) -> str:
        return (Path(__file__).parents[1] / "app.py").read_text(
            encoding="utf-8"
        )

    def test_header_bar_is_the_native_titlebar(self) -> None:
        source = self._app_source()
        self.assertIn("self.set_titlebar(header)", source)
        self.assertNotIn("root.append(header)", source)

    def test_header_bar_follows_gnome_control_positions(self) -> None:
        source = self._app_source()
        self.assertIn("header.pack_start(self.add_button)", source)
        self.assertIn("header.pack_end(menu_button)", source)
        self.assertIn(
            'menu_icon = Gtk.Image.new_from_icon_name("imap-menu-symbolic")',
            source,
        )
        self.assertNotIn(
            'menu_button.set_child(icon_label("imap-menu-symbolic", _("Menu")))',
            source,
        )
        self.assertNotIn(
            'self.add_button.add_css_class("suggested-action")',
            source,
        )

    def test_dialog_buttons_receive_outer_margins(self) -> None:
        source = self._app_source()
        self.assertIn("class AppDialog(Gtk.Window):", source)
        self.assertIn("set_margins(self.footer, 16)", source)
        self.assertIn("def set_footer_visible(self, visible: bool)", source)
        self.assertIn("self.footer_separator.set_visible(visible)", source)
        self.assertIn("self.footer.set_visible(visible)", source)

    def test_scrolled_content_keeps_a_fixed_rounded_viewport(self) -> None:
        root = Path(__file__).parents[1]
        source = self._app_source()
        stylesheet = (root / "style.css").read_text(encoding="utf-8")
        self.assertIn("def rounded_scroll_frame(", source)
        self.assertIn(
            "frame.set_overflow(Gtk.Overflow.HIDDEN)",
            source,
        )
        self.assertGreaterEqual(
            source.count("rounded_scroll_frame("),
            9,
        )
        self.assertEqual(
            source.count('add_css_class("scroll-viewport-list")'),
            6,
        )
        self.assertIn(
            "body_frame.set_overflow(Gtk.Overflow.HIDDEN)",
            source,
        )
        self.assertIn(".rounded-scroll-frame {", stylesheet)
        self.assertIn(
            ".rounded-scroll-frame .scroll-viewport-list row {",
            stylesheet,
        )

    def test_deprecated_gtk_dialogs_are_not_used(self) -> None:
        source = self._app_source()
        self.assertNotIn("Gtk.Dialog(", source)
        self.assertNotIn("Gtk.MessageDialog(", source)
        self.assertNotIn("format_secondary_text", source)

    def test_folder_page_has_a_reload_action(self) -> None:
        source = self._app_source()
        self.assertIn(
            'icon_button(\n            _("Recarregar"), "imap-refresh-symbolic"',
            source,
        )
        self.assertIn("def _reload_folders(", source)

    def test_folder_loading_is_inside_the_fixed_list_viewport(self) -> None:
        source = self._app_source()
        folder_page = source.split(
            "def _build_folders_page(",
            maxsplit=1,
        )[1].split(
            "def _build_progress_page(",
            maxsplit=1,
        )[0]
        self.assertIn("self.folder_content_stack = Gtk.Stack()", folder_page)
        self.assertIn(
            'self.folder_content_stack.add_named(loading_page, "loading")',
            folder_page,
        )
        self.assertIn(
            'self.folder_content_stack.add_named(scroller, "folders")',
            folder_page,
        )
        self.assertIn("self.folder_loading_status = Gtk.Label(", folder_page)
        self.assertNotIn("page.append(self.folder_spinner)", folder_page)

    def test_critical_actions_use_bundled_symbolic_icons(self) -> None:
        source = self._app_source()
        self.assertNotIn("set_icon_from_icon_name", source)
        self.assertIn("Gtk.Image.new_from_icon_name(icon_name)", source)
        self.assertIn('edit = icon_only_button("imap-edit-symbolic")', source)
        self.assertIn(
            'export = icon_only_button("imap-results-symbolic")',
            source,
        )
        self.assertIn(
            '"imap-delete-symbolic"\n'
            "                if unlocked\n"
            '                else "imap-admin-delete-symbolic"',
            source,
        )
        self.assertIn("add_search_path(", source)
        self.assertIn('gi.require_version("Gdk", "4.0")', source)
        self.assertTrue(
            (Path(__file__).parents[1] / "style.css").exists()
        )
        icon_root = (
            Path(__file__).parents[1]
            / "assets"
            / "icons"
            / "hicolor"
            / "scalable"
            / "actions"
        )
        for icon in (
            "imap-lock-symbolic.svg",
            "imap-unlock-symbolic.svg",
            "imap-view-symbolic.svg",
            "imap-export-symbolic.svg",
            "imap-admin-delete-symbolic.svg",
        ):
            self.assertTrue((icon_root / icon).exists(), icon)

    def test_account_actions_are_protected_by_the_lock_session(self) -> None:
        source = self._app_source()
        self.assertIn("self.unlocked_accounts:", source)
        self.assertIn("def _toggle_account_lock(", source)
        self.assertIn("def _require_account_unlocked(", source)
        self.assertIn("extract.set_sensitive(unlocked)", source)
        self.assertIn("edit.set_sensitive(unlocked)", source)
        self.assertIn("remove.set_sensitive(not recovery_pending)", source)
        self.assertIn('remove = icon_only_button(remove_icon)', source)
        self.assertIn("def _confirm_recovery_remove(", source)
        self.assertIn("def _authorize_locked_account_removal(", source)
        self.assertIn("def _delete_local_account(", source)
        self.assertNotIn("def _request_cleanup_password(", source)

    def test_locked_account_removal_uses_polkit_without_imap_access(
        self,
    ) -> None:
        root = Path(__file__).parents[1]
        source = self._app_source()
        recovery = source.split(
            "def _confirm_recovery_remove(",
            maxsplit=1,
        )[1].split(
            "def _confirm_rebuild_index(",
            maxsplit=1,
        )[0]
        self.assertIn("shutil.which(\"pkexec\")", recovery)
        self.assertIn("subprocess.run(", recovery)
        self.assertIn("[pkexec, helper]", recovery)
        self.assertIn("self._delete_local_account(current)", recovery)
        self.assertNotIn("self.extractor.", recovery)
        self.assertNotIn("imap_password", recovery)

        helper = (
            root / "packaging" / "imap-exporter-authorize-delete"
        ).read_text(encoding="utf-8")
        self.assertIn('[ "$(id -u)" -eq 0 ]', helper)
        self.assertIn('case "${PKEXEC_UID:-}"', helper)
        self.assertNotIn("sqlite", helper.lower())

        policy = (
            root / "packaging" / "io.github.ehstbr.imapexporter.policy"
        ).read_text(encoding="utf-8")
        self.assertIn("<allow_active>auth_admin</allow_active>", policy)
        self.assertNotIn("auth_admin_keep", policy)

    def test_account_rows_are_compact_and_do_not_repeat_lock_state(self) -> None:
        source = self._app_source()
        account_rows = source.split(
            "def refresh_accounts",
            maxsplit=1,
        )[1].split(
            "def _show_add_account_dialog",
            maxsplit=1,
        )[0]
        self.assertIn(
            "orientation=Gtk.Orientation.HORIZONTAL,\n"
            "                spacing=16,",
            account_rows,
        )
        self.assertIn("actions.set_valign(Gtk.Align.CENTER)", account_rows)
        self.assertIn('edit = icon_only_button("imap-edit-symbolic")', account_rows)
        self.assertIn(
            'export = icon_only_button("imap-results-symbolic")',
            account_rows,
        )
        self.assertIn(
            "remove = icon_only_button(remove_icon)",
            account_rows,
        )
        self.assertNotIn('{"desbloqueada" if unlocked else "bloqueada"}', account_rows)
        self.assertNotIn(" · bloqueada", account_rows)

    def test_add_account_is_a_two_step_form_without_scrolling(self) -> None:
        source = self._app_source()
        add_dialog = source.split(
            "def _show_add_account_dialog",
            maxsplit=1,
        )[1].split(
            "def _show_edit_account_dialog",
            maxsplit=1,
        )[0]
        self.assertIn('"Etapa 1 de 2 · Conta e servidor"', add_dialog)
        self.assertIn('"Etapa 2 de 2 · Senhas"', add_dialog)
        self.assertIn('wizard.add_named(account_form, "account")', add_dialog)
        self.assertIn('wizard.add_named(security_form, "security")', add_dialog)
        self.assertNotIn("make_form_scroller(", add_dialog)

    def test_edit_account_is_compact_and_password_change_is_separate(
        self,
    ) -> None:
        source = self._app_source()
        self.assertNotIn("def make_form_scroller(", source)
        edit_dialog = source.split(
            "def _show_edit_account_dialog",
            maxsplit=1,
        )[1].split(
            "def _show_change_local_password_dialog",
            maxsplit=1,
        )[0]
        self.assertNotIn("make_form_scroller(", edit_dialog)
        self.assertIn('dialog.add_start_button(_("Alterar senha local"))', edit_dialog)
        self.assertNotIn('"new_local"', edit_dialog)
        self.assertIn("def _show_change_local_password_dialog(", source)

    def test_results_offer_full_and_selected_exports(self) -> None:
        source = self._app_source()
        self.assertIn('_("CSV da seleção"), "imap-export-symbolic"', source)
        self.assertIn('_("ODS da seleção"), "imap-export-symbolic"', source)
        self.assertIn(
            "senders_page.append(self._build_cleanup_action_bar())",
            source,
        )
        self.assertIn(
            "domains_page.append(self._build_cleanup_action_bar())",
            source,
        )
        self.assertNotIn("page.append(cleanup_bar)", source)
        self.assertIn("self.csv_button.set_sensitive(has_messages)", source)
        self.assertIn("self.ods_button.set_sensitive(has_messages)", source)
        self.assertIn("def _select_visible_senders(", source)
        self.assertIn("def _select_visible_domains(", source)

    def test_results_footer_places_back_left_and_exports_right(self) -> None:
        source = self._app_source()
        self.assertLess(
            source.index("actions.append(accounts_button)"),
            source.index("actions.append(action_spacer)"),
        )
        self.assertLess(
            source.index("actions.append(action_spacer)"),
            source.index("actions.append(self.csv_button)"),
        )
        self.assertLess(
            source.index("actions.append(self.csv_button)"),
            source.index("actions.append(self.ods_button)"),
        )

    def test_folder_page_uses_one_synchronization_action(self) -> None:
        source = self._app_source()
        self.assertNotIn("self.mode_dropdown", source)
        self.assertIn('_("Sincronizar agora")', source)
        self.assertIn("def _confirm_rebuild_index(", source)

    def test_subject_preview_is_lazy_searchable_and_paginated(self) -> None:
        source = self._app_source()
        self.assertEqual(source.count('"query-tooltip"'), 2)
        self.assertEqual(
            source.count('icon_button(_("Ver"), "imap-view-symbolic")'),
            3,
        )
        self.assertIn("def _query_subject_tooltip(", source)
        self.assertIn("def _show_subjects_dialog(", source)
        self.assertIn("self.database.subject_messages(", source)
        self.assertIn("self.database.count_subject_messages(", source)
        self.assertIn('_("Pesquisar por assunto ou remetente")', source)
        self.assertIn('_("Data inicial no formato DD/MM/AAAA")', source)
        self.assertIn('_("Data final no formato DD/MM/AAAA")', source)
        self.assertIn('_("Duplo clique para ler")', source)
        self.assertIn(
            '"{shown} de {total} mensagens exibidas"',
            source,
        )
        self.assertIn('dialog.add_start_button(_("Carregar mais"))', source)
        self.assertIn("page_size = 200", source)
        self.assertIn("message_list.set_activate_on_single_click(False)", source)
        self.assertIn("def _open_message_reader(", source)
        self.assertIn("self.database.message_reader_target(", source)
        self.assertIn("self.extractor.fetch_message_for_reader(", source)
        self.assertIn(
            "reader_stack.set_transition_type("
            "Gtk.StackTransitionType.CROSSFADE)",
            source,
        )
        self.assertIn(
            'reader_stack.add_named(loading_page, "loading")',
            source,
        )
        self.assertIn(
            'reader_stack.add_named(message_page, "message")',
            source,
        )
        self.assertIn(
            'reader_stack.add_named(error_page, "error")',
            source,
        )

    def test_reader_refreshes_and_limits_the_attachment_panel(self) -> None:
        source = self._app_source()
        reader = source.split(
            "def _open_message_reader(",
            maxsplit=1,
        )[1].split(
            "def _domain_is_protected(",
            maxsplit=1,
        )[0]
        self.assertIn("def render_attachments(indexed: bool)", reader)
        self.assertGreaterEqual(
            reader.count("self.database.message_attachments("),
            3,
        )
        self.assertEqual(reader.count("render_attachments(True)"), 2)
        self.assertIn("attachment_scroller.set_max_content_height(180)", reader)
        self.assertIn(
            "attachment_scroller.set_propagate_natural_height(True)",
            reader,
        )
        self.assertIn("default_height=620", reader)
        self.assertIn("dialog.set_modal(False)", reader)
        self.assertIn("dialog.set_resizable(True)", reader)
        self.assertIn("dialog.set_footer_visible(False)", reader)
        self.assertNotIn("maximize_button", reader)
        self.assertNotIn("imap-maximize-symbolic", reader)
        self.assertNotIn('dialog.add_button(_("Fechar")', reader)
        self.assertIn("dialog.present()", reader)
        self.assertIn('Gtk.Expander(label=_("Detalhes da mensagem"))', reader)
        self.assertIn('Gtk.Expander(label=_("Conteúdo da mensagem"))', reader)
        self.assertIn("def section_expanded(", reader)
        self.assertNotIn(
            "Use Analisar anexos na aba Maiores.",
            reader,
        )

    def test_largest_extension_filter_allows_multiple_choices(self) -> None:
        source = self._app_source()
        database = (
            Path(__file__).parents[1] / "mail_exporter" / "db.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "self.largest_extension_button = Gtk.MenuButton()",
            source,
        )
        self.assertIn(
            "self.largest_extension_checks: dict[str, Gtk.CheckButton]",
            source,
        )
        self.assertIn(
            "def _selected_largest_extensions(self) -> list[str]:",
            source,
        )
        self.assertIn("extensions=extensions", source)
        self.assertNotIn(
            "self.largest_extension = Gtk.DropDown.new_from_strings(",
            source,
        )
        self.assertIn(
            "extensions: Iterable[str] | None = None",
            database,
        )
        self.assertIn("ax.extension IN ({placeholders})", database)

    def test_rebuild_index_runs_visibly_and_keeps_the_unlocked_session(
        self,
    ) -> None:
        source = self._app_source()
        rebuild = source.split(
            "def _confirm_rebuild_index(",
            maxsplit=1,
        )[1].split(
            "def _show_error(",
            maxsplit=1,
        )[0]
        self.assertIn("session = self._require_account_unlocked(account)", rebuild)
        self.assertIn("threading.Thread(target=work, daemon=True).start()", rebuild)
        self.assertIn("self.active_imap_password = session[\"imap_password\"]", rebuild)
        self.assertIn("self._discover_folders(is_rebuild=True)", rebuild)
        self.assertIn('_("Preparando a reconstrução")', rebuild)

    def test_cleanup_rankings_support_mouse_and_keyboard(self) -> None:
        source = self._app_source()
        results_builder = source.split(
            "def _build_results_page(",
            maxsplit=1,
        )[1].split(
            "def _build_cleanup_action_bar(",
            maxsplit=1,
        )[0]
        self.assertEqual(
            results_builder.count(
                "set_selection_mode(Gtk.SelectionMode.SINGLE)"
            ),
            3,
        )
        self.assertEqual(
            results_builder.count("set_activate_on_single_click(True)"),
            3,
        )
        self.assertIn('"row-activated"', source)
        self.assertIn("Gdk.KEY_space", source)
        self.assertIn("def _cleanup_row_key_pressed(", source)
        self.assertIn("check.set_focusable(False)", source)

    def test_message_dialogs_focus_the_button_without_selecting_text(
        self,
    ) -> None:
        source = self._app_source()
        self.assertIn("def present_with_focus(", source)
        self.assertNotIn("select_region(", source)
        self.assertNotIn("selectable=True", source)
        self.assertEqual(
            source.count("dialog.present_with_focus(ok_button)"),
            2,
        )

    def test_summary_uses_visual_cards_instead_of_a_text_block(self) -> None:
        source = self._app_source()
        stylesheet = (
            Path(__file__).parents[1] / "style.css"
        ).read_text(encoding="utf-8")
        for field in (
            "summary_messages_value",
            "summary_senders_value",
            "summary_domains_value",
            "summary_volume_value",
            "summary_period_label",
            "summary_errors_label",
        ):
            self.assertIn(f"self.{field}", source)
        self.assertNotIn("self.results_summary", source)
        self.assertIn(".summary-card", stylesheet)
        self.assertIn(".summary-value", stylesheet)

    def test_export_status_stays_hidden_until_it_is_needed(self) -> None:
        source = self._app_source()
        self.assertNotIn("CSV e ODS incluem", source)
        self.assertNotIn(
            "CSV e ODS exportam todas as mensagens extraídas, sem ",
            source,
        )
        self.assertIn("self.export_status.set_visible(False)", source)
        self.assertIn(
            "self.export_status.set_visible(True)\n"
            "        self.export_status.set_text(",
            source,
        )

    def test_largest_page_prioritizes_the_message_list(self) -> None:
        source = self._app_source()
        results_builder = source.split(
            "def _build_results_page(",
            maxsplit=1,
        )[1].split(
            "def _build_cleanup_action_bar(",
            maxsplit=1,
        )[0]
        largest_renderer = source.split(
            "def _render_largest_rows(",
            maxsplit=1,
        )[1].split(
            "def _largest_message_toggled(",
            maxsplit=1,
        )[0]
        self.assertIn(
            "largest_toolbar.append(self.largest_search)",
            results_builder,
        )
        self.assertIn(
            "largest_toolbar.append(self.largest_mode)",
            results_builder,
        )
        self.assertIn(
            "largest_toolbar.append(self.largest_extension_button)",
            results_builder,
        )
        self.assertIn(
            "largest_toolbar.append(self.largest_size)",
            results_builder,
        )
        self.assertEqual(
            results_builder.count("largest_page.append(largest_info_row)"),
            1,
        )
        self.assertNotIn("largest_selection_actions", results_builder)
        self.assertIn("set_margins(content, 7)", largest_renderer)
        self.assertIn("secondary_text = ", largest_renderer)

    def test_all_result_tabs_use_the_compact_layout(self) -> None:
        source = self._app_source()
        results_builder = source.split(
            "def _build_results_page(",
            maxsplit=1,
        )[1].split(
            "def _build_cleanup_action_bar(",
            maxsplit=1,
        )[0]
        sender_renderer = source.split(
            "def _render_sender_rows(",
            maxsplit=1,
        )[1].split(
            "def _visible_sender_rows(",
            maxsplit=1,
        )[0]
        domain_renderer = source.split(
            "def _render_domain_rows(",
            maxsplit=1,
        )[1].split(
            "def _visible_domain_rows(",
            maxsplit=1,
        )[0]
        for toolbar, search in (
            ("sender_toolbar", "self.sender_search"),
            ("domain_toolbar", "self.domain_search"),
        ):
            self.assertIn(f"{toolbar}.append({search})", results_builder)
        self.assertNotIn("sender_note = Gtk.Label(", results_builder)
        self.assertNotIn("domain_note = Gtk.Label(", results_builder)
        self.assertIn("set_margins(summary_page, 6)", results_builder)
        self.assertIn("set_margins(content, 7)", sender_renderer)
        self.assertIn("set_margins(content, 7)", domain_renderer)

    def test_cleanup_runs_in_a_collapsible_interruptible_dialog(self) -> None:
        source = self._app_source()
        service = (
            Path(__file__).parents[1]
            / "mail_exporter"
            / "imap_service.py"
        ).read_text(encoding="utf-8")
        self.assertIn("def _build_cleanup_progress_dialog(", source)
        self.assertIn(
            "self.cleanup_log_revealer.set_reveal_child(False)",
            source,
        )
        self.assertIn("def _toggle_cleanup_pause(", source)
        self.assertIn("def _cancel_cleanup(", source)
        self.assertIn("def _start_cleanup_undo(", source)
        self.assertIn("def _run_cleanup_undo(", source)
        self.assertIn('_("Reverter movimentação")', source)
        self.assertIn(
            "self.cleanup_undo_button = dialog.add_start_button(",
            source,
        )
        self.assertIn(
            "self.cleanup_undo_button.set_visible(True)",
            source,
        )
        self.assertIn(
            "self.cleanup_undo_button.set_sensitive(True)",
            source,
        )
        self.assertIn('title=_("Confirmar reversão")', source)
        self.assertIn(
            'self._show_notice(\n'
            '                _("Reversão automática indisponível")',
            source,
        )
        self.assertIn(
            'self.cleanup_phase.set_text(_("Limpeza concluída"))\n'
            "            self._show_cleanup_recovery_options()",
            source,
        )
        self.assertIn("mensagens nela por cerca de 30 dias", source)
        self.assertIn("cancel_event: threading.Event | None = None", service)
        self.assertIn("pause_event: threading.Event | None = None", service)
        self.assertNotIn("self.cleanup_progress = Gtk.ProgressBar()", source)

    def test_cleanup_automatically_reconciles_only_related_folders(self) -> None:
        source = self._app_source()
        database = (
            Path(__file__).parents[1] / "mail_exporter" / "db.py"
        ).read_text(encoding="utf-8")
        service = (
            Path(__file__).parents[1]
            / "mail_exporter"
            / "imap_service.py"
        ).read_text(encoding="utf-8")
        self.assertIn("def _reconcile_after_cleanup_action(", source)
        self.assertIn("def _begin_cleanup_reconciliation(", source)
        self.assertIn(
            "self.database.get_action_reconciliation_mailboxes(",
            source,
        )
        self.assertIn(
            "result[\"reconciliation\"] = (",
            source,
        )
        self.assertIn(
            "def get_action_reconciliation_mailboxes(",
            database,
        )
        self.assertIn(
            '"source_mailbox": mailbox_name',
            service,
        )
        self.assertIn(
            "As pastas envolvidas e os UIDs locais foram atualizados",
            source,
        )

    def test_progress_log_is_detailed_collapsible_and_hidden_initially(
        self,
    ) -> None:
        source = self._app_source()
        service = (
            Path(__file__).parents[1]
            / "mail_exporter"
            / "imap_service.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "self.progress_details_button = Gtk.ToggleButton(",
            source,
        )
        self.assertIn('label=_("Mostrar detalhes")', source)
        self.assertIn(
            "self.progress_log_revealer.set_reveal_child(False)",
            source,
        )
        self.assertIn("def _toggle_progress_details(", source)
        self.assertIn("def _copy_progress_log(", source)
        self.assertIn('"type": "mailbox_plan"', service)
        self.assertIn('"batch_number": batch_number', service)
        self.assertIn('"batch_inserted": batch_inserted', service)

    def test_attachment_analysis_log_matches_other_progress_dialogs(
        self,
    ) -> None:
        source = self._app_source()
        analysis = source.split(
            "def _start_attachment_analysis(",
            maxsplit=1,
        )[1].split(
            "def _short_message_date(",
            maxsplit=1,
        )[0]
        self.assertIn(
            'details_button = Gtk.ToggleButton(label=_("Mostrar detalhes"))',
            analysis,
        )
        self.assertIn("log_revealer = Gtk.Revealer()", analysis)
        self.assertIn("log_revealer.set_reveal_child(False)", analysis)
        self.assertIn(
            'copy_log_button = Gtk.Button(label=_("Copiar log"))',
            analysis,
        )
        self.assertNotIn(
            'Gtk.Expander(label=_("Mostrar detalhes"))',
            analysis,
        )

    def test_attachment_rows_show_real_download_progress(self) -> None:
        source = self._app_source()
        service = (
            Path(__file__).parents[1]
            / "mail_exporter"
            / "imap_service.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'download_progress.add_css_class(\n'
            '                    "attachment-row-progress"',
            source,
        )
        self.assertIn("attachment_overlay = Gtk.Overlay()", source)
        self.assertIn(
            "attachment_overlay.set_measure_overlay(\n"
            "                    attachment_content,\n"
            "                    True,\n"
            "                )",
            source,
        )
        self.assertIn(
            "attachment_overlay.set_clip_overlay(\n"
            "                    attachment_content,\n"
            "                    True,\n"
            "                )",
            source,
        )
        self.assertIn(
            "progress=lambda received, total: GLib.idle_add(",
            source,
        )
        self.assertIn(
            "GLib.idle_add(start_row, attachment)",
            source,
        )
        self.assertIn(
            "download_rows: dict[",
            source,
        )
        self.assertIn(
            "ATTACHMENT_FETCH_CHUNK_SIZE = 1024 * 1024",
            service,
        )
        self.assertIn(
            'f"<{received}.{requested}>)"',
            service,
        )
        self.assertIn(
            "cancel_event: threading.Event | None = None",
            service,
        )
        self.assertIn("raise AttachmentDownloadCancelled()", service)
        self.assertIn('_("Cancelar este download")', source)
        self.assertIn('_("Cancelar todos")', source)
        self.assertIn("cancel_event=cancel_event", source)
        self.assertIn(
            "except AttachmentDownloadCancelled:",
            source,
        )
        self.assertTrue(
            (
                Path(__file__).parents[1]
                / "assets"
                / "icons"
                / "hicolor"
                / "scalable"
                / "actions"
                / "imap-cancel-symbolic.svg"
            ).exists()
        )
        stylesheet = (
            Path(__file__).parents[1] / "style.css"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "background-color: rgba(53, 132, 228, 0.40);",
            stylesheet,
        )
        self.assertIn(
            "background-color: alpha(@theme_selected_bg_color, 0.46);",
            stylesheet,
        )

    def test_about_window_has_expected_tabs_and_project_identity(self) -> None:
        source = self._app_source()
        self.assertIn('APP_NAME = "IMAP Exporter"', source)
        self.assertIn('APP_VERSION = "0.4.12"', source)
        self.assertIn("def _show_about_dialog(", source)
        about = source.split(
            "def _show_about_dialog(",
            maxsplit=1,
        )[1].split(
            "def _build_accounts_page(",
            maxsplit=1,
        )[0]
        self.assertIn(
            "about_icon = Gtk.Image.new_from_icon_name(APP_ID)",
            about,
        )
        self.assertIn("about_icon.set_pixel_size(96)", about)
        self.assertLess(
            about.index("information.append(about_icon)"),
            about.index("information.append(name)"),
        )
        self.assertIn("dialog.set_footer_visible(False)", about)
        self.assertNotIn('dialog.add_button(\n            _("Fechar")', about)
        self.assertIn("dialog.present()", about)
        self.assertNotIn("close_button", about)
        for page in (
            "information",
            "terms",
            "privacy",
            "license",
            "components",
        ):
            self.assertIn(f'"{page}"', source)
        self.assertIn("https://github.com/ehstbr/IMAP-Exporter", source)
        self.assertIn("contato@eduhcommerce.com.br", source)
        self.assertIn("MIT_LICENSE_FALLBACK", source)
        self.assertIn('self._read_packaged_document(', source)
        root = Path(__file__).parents[1]
        for filename in (
            "LICENSE",
            "THIRD_PARTY_NOTICES.pt_BR.md",
            "THIRD_PARTY_NOTICES.en.md",
        ):
            self.assertTrue((root / filename).exists())
            self.assertIn(f'"$PROJECT_DIR/{filename}"', (
                root / "packaging" / "build-deb.sh"
            ).read_text(encoding="utf-8"))

    def test_debian_package_declares_runtime_dependencies_and_desktop_files(
        self,
    ) -> None:
        root = Path(__file__).parents[1]
        control = (
            root / "packaging" / "debian" / "control.in"
        ).read_text(encoding="utf-8")
        for dependency in (
            "python3 (>= 3.10)",
            "python3-gi",
            "gir1.2-gtk-4.0",
            "openssl",
            "ca-certificates",
            "hicolor-icon-theme",
            "pkexec",
        ):
            self.assertIn(dependency, control)
        desktop = (
            root / "packaging" / "io.github.ehstbr.imapexporter.desktop"
        ).read_text(encoding="utf-8")
        self.assertIn("Exec=imap-exporter", desktop)
        self.assertIn("Icon=io.github.ehstbr.imapexporter", desktop)
        self.assertTrue((root / "packaging" / "build-deb.sh").exists())
        policy = (
            root / "packaging" / "io.github.ehstbr.imapexporter.policy"
        )
        helper = root / "packaging" / "imap-exporter-authorize-delete"
        self.assertTrue(policy.exists())
        self.assertTrue(helper.exists())
        self.assertIn(
            "io.github.ehstbr.imapexporter.remove-locked-account",
            policy.read_text(encoding="utf-8"),
        )
        self.assertIn(
            'install -m 0755 \\\n'
            '    "$PROJECT_DIR/packaging/imap-exporter-authorize-delete"',
            (root / "packaging" / "build-deb.sh").read_text(
                encoding="utf-8"
            ),
        )
        self.assertTrue(
            (root / "assets" / "io.github.ehstbr.imapexporter.svg").exists()
        )
        for size in (16, 32, 48, 64, 128, 256):
            self.assertTrue(
                (
                    root
                    / "assets"
                    / "icons"
                    / "hicolor"
                    / f"{size}x{size}"
                    / "apps"
                    / "io.github.ehstbr.imapexporter.png"
                ).exists()
            )


if __name__ == "__main__":
    unittest.main()
