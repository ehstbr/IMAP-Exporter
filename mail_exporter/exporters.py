from __future__ import annotations

import csv
import json
import os
import tempfile
import zipfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from .db import Database


MESSAGE_COLUMNS = [
    ("account_name", "Conta"),
    ("account_email", "E-mail da conta"),
    ("provider_message_id", "ID da mensagem no provedor"),
    ("provider_thread_id", "ID da conversa"),
    ("source_mailbox", "Pasta de origem"),
    ("source_uid", "UID"),
    ("message_id", "Message-ID"),
    ("from_name", "Nome do remetente"),
    ("from_email", "E-mail do remetente"),
    ("from_domain", "Domínio do remetente"),
    ("sender_name", "Nome do Sender"),
    ("sender_email", "E-mail do Sender"),
    ("sender_domain", "Domínio do Sender"),
    ("reply_to", "Responder para"),
    ("to_addresses", "Destinatários"),
    ("cc_addresses", "Cc"),
    ("bcc_addresses", "Cco"),
    ("subject", "Assunto"),
    ("date_header_raw", "Data original do cabeçalho"),
    ("date_sent_utc", "Data de envio UTC"),
    ("internal_date_utc", "Data interna UTC"),
    ("delivered_to", "Delivered-To"),
    ("x_original_to", "X-Original-To"),
    ("return_path", "Return-Path"),
    ("in_reply_to", "In-Reply-To"),
    ("message_references", "References"),
    ("list_id", "List-ID"),
    ("flags_json", "Flags"),
    ("labels_json", "Marcadores"),
    ("size_bytes", "Tamanho em bytes"),
    ("attachment_indexed", "Anexos analisados"),
    ("attachment_count", "Quantidade de anexos"),
    ("attachment_size_bytes", "Tamanho dos anexos em bytes"),
    ("attachment_names", "Nomes dos anexos"),
    ("attachment_extensions", "Extensões dos anexos"),
]

RECIPIENT_COLUMNS = [
    ("account_name", "Conta"),
    ("account_email", "E-mail da conta"),
    ("provider_message_id", "ID da mensagem no provedor"),
    ("message_id", "Message-ID"),
    ("subject", "Assunto"),
    ("kind", "Tipo"),
    ("position", "Posição"),
    ("name", "Nome"),
    ("email", "E-mail"),
    ("domain", "Domínio"),
]

DOMAIN_COLUMNS = [
    ("account_name", "Conta"),
    ("domain", "Domínio"),
    ("messages", "Mensagens"),
    ("senders", "Remetentes"),
    ("total_size", "Volume em bytes"),
    ("first_date", "Primeira mensagem"),
    ("last_date", "Última mensagem"),
]

SENDER_COLUMNS = [
    ("account_name", "Conta"),
    ("name", "Nome do remetente"),
    ("email", "E-mail do remetente"),
    ("domain", "Domínio"),
    ("messages", "Mensagens"),
    ("total_size", "Volume em bytes"),
    ("first_date", "Primeira mensagem"),
    ("last_date", "Última mensagem"),
]

ERROR_COLUMNS = [
    ("account_name", "Conta"),
    ("mailbox_name", "Pasta"),
    ("uid", "UID"),
    ("error_type", "Tipo"),
    ("detail", "Detalhe"),
    ("created_at", "Data"),
]

ATTACHMENT_COLUMNS = [
    ("account_name", "Conta"),
    ("account_email", "E-mail da conta"),
    ("provider_message_id", "ID da mensagem no provedor"),
    ("subject", "Assunto"),
    ("from_name", "Nome do remetente"),
    ("from_email", "E-mail do remetente"),
    ("part_number", "Seção IMAP"),
    ("filename", "Nome do anexo"),
    ("extension", "Extensão"),
    ("content_type", "Tipo MIME"),
    ("disposition", "Disposição"),
    ("transfer_encoding", "Codificação"),
    ("size_bytes", "Tamanho codificado em bytes"),
]


def display_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def safe_spreadsheet_text(value: Any) -> str:
    text = display_value(value)
    if text.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def row_values(row: dict[str, Any], columns: list[tuple[str, str]]) -> list[str]:
    return [safe_spreadsheet_text(row.get(key)) for key, _ in columns]


def export_csv(
    database: Database,
    account_ids: list[int],
    destination: str | Path,
    progress: callable | None = None,
    *,
    sender_emails: list[str] | None = None,
    domains: list[str] | None = None,
    message_ids: list[int] | None = None,
) -> Path:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    progress = progress or (lambda current: None)
    fd, temporary_name = tempfile.mkstemp(
        prefix=destination.name + ".", suffix=".tmp", dir=destination.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream, delimiter=";", quoting=csv.QUOTE_MINIMAL)
            writer.writerow([label for _, label in MESSAGE_COLUMNS])
            rows = database.iter_messages(
                account_ids,
                sender_emails,
                domains,
                message_ids,
            )
            for index, row in enumerate(rows, start=1):
                writer.writerow(row_values(row, MESSAGE_COLUMNS))
                if index % 1000 == 0:
                    progress(index)
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def _write_ods_cell(stream, value: Any) -> None:
    text = safe_spreadsheet_text(value)
    stream.write(
        '<table:table-cell office:value-type="string"><text:p>'
        + escape(text)
        + "</text:p></table:table-cell>"
    )


def _write_ods_table(
    stream,
    name: str,
    columns: list[tuple[str, str]],
    rows: Iterable[dict[str, Any]],
    progress: callable | None = None,
) -> int:
    progress = progress or (lambda current: None)
    stream.write(f"<table:table table:name={quoteattr(name)}>")
    stream.write("<table:table-row>")
    for _, label in columns:
        _write_ods_cell(stream, label)
    stream.write("</table:table-row>")
    amount = 0
    for amount, row in enumerate(rows, start=1):
        stream.write("<table:table-row>")
        for key, _ in columns:
            _write_ods_cell(stream, row.get(key))
        stream.write("</table:table-row>")
        if amount % 1000 == 0:
            progress(amount)
    stream.write("</table:table>")
    return amount


def _content_xml(
    database: Database,
    account_ids: list[int],
    progress: callable | None = None,
) -> Iterator[str]:
    yield """<?xml version="1.0" encoding="UTF-8"?>
<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 office:version="1.2">
 <office:body><office:spreadsheet>"""
    # The iterator form is retained for the fixed prefix/suffix; tables are
    # streamed directly by export_ods to avoid keeping the document in memory.
    yield "</office:spreadsheet></office:body></office:document-content>"


def export_ods(
    database: Database,
    account_ids: list[int],
    destination: str | Path,
    progress: callable | None = None,
    *,
    sender_emails: list[str] | None = None,
    domains: list[str] | None = None,
    message_ids: list[int] | None = None,
) -> Path:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    progress = progress or (lambda sheet, current: None)
    fd, temporary_name = tempfile.mkstemp(
        prefix=destination.name + ".", suffix=".tmp", dir=destination.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    fd, content_name = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    content_path = Path(content_name)
    selected_export = (
        sender_emails is not None
        or domains is not None
        or message_ids is not None
    )
    try:
        prefix, suffix = list(_content_xml(database, account_ids))
        with content_path.open("w", encoding="utf-8", newline="") as stream:
            stream.write(prefix)
            _write_ods_table(
                stream,
                "Mensagens",
                MESSAGE_COLUMNS,
                database.iter_messages(
                    account_ids,
                    sender_emails,
                    domains,
                    message_ids,
                ),
                lambda current: progress("Mensagens", current),
            )
            _write_ods_table(
                stream,
                "Destinatários",
                RECIPIENT_COLUMNS,
                database.iter_recipients(
                    account_ids,
                    sender_emails,
                    domains,
                    message_ids,
                ),
                lambda current: progress("Destinatários", current),
            )
            _write_ods_table(
                stream,
                "Remetentes",
                SENDER_COLUMNS,
                database.export_sender_summary(
                    account_ids,
                    sender_emails,
                    domains,
                    message_ids,
                ),
            )
            _write_ods_table(
                stream,
                "Domínios",
                DOMAIN_COLUMNS,
                database.domain_summary(
                    account_ids,
                    sender_emails,
                    domains,
                    message_ids,
                ),
            )
            _write_ods_table(
                stream,
                "Anexos",
                ATTACHMENT_COLUMNS,
                database.iter_attachments(
                    account_ids,
                    sender_emails,
                    domains,
                    message_ids,
                ),
                lambda current: progress("Anexos", current),
            )
            _write_ods_table(
                stream,
                "Erros",
                ERROR_COLUMNS,
                [] if selected_export else database.export_errors(account_ids),
            )
            stream.write(suffix)

        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
        ) as archive:
            archive.writestr(
                "mimetype",
                "application/vnd.oasis.opendocument.spreadsheet",
                compress_type=zipfile.ZIP_STORED,
            )
            archive.write(content_path, "content.xml")
            archive.writestr(
                "styles.xml",
                """<?xml version="1.0" encoding="UTF-8"?>
<office:document-styles
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 office:version="1.2"><office:styles/></office:document-styles>""",
            )
            archive.writestr(
                "meta.xml",
                """<?xml version="1.0" encoding="UTF-8"?>
<office:document-meta
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0"
 office:version="1.2"><office:meta>
<meta:generator>IMAP Exporter</meta:generator>
</office:meta></office:document-meta>""",
            )
            archive.writestr(
                "META-INF/manifest.xml",
                """<?xml version="1.0" encoding="UTF-8"?>
<manifest:manifest
 xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"
 manifest:version="1.2">
 <manifest:file-entry manifest:full-path="/"
  manifest:media-type="application/vnd.oasis.opendocument.spreadsheet"/>
 <manifest:file-entry manifest:full-path="content.xml"
  manifest:media-type="text/xml"/>
 <manifest:file-entry manifest:full-path="styles.xml"
  manifest:media-type="text/xml"/>
 <manifest:file-entry manifest:full-path="meta.xml"
  manifest:media-type="text/xml"/>
</manifest:manifest>""",
            )
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        content_path.unlink(missing_ok=True)
    return destination
