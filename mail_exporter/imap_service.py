from __future__ import annotations

import base64
import binascii
import imaplib
import json
import quopri
import re
import ssl
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from typing import Any

from .db import Database, utc_now
from .headers import parse_bodystructure_fetch_item, parse_fetch_item


HEADER_FIELDS = (
    "FROM SENDER REPLY-TO TO CC BCC SUBJECT DATE MESSAGE-ID "
    "IN-REPLY-TO REFERENCES DELIVERED-TO X-ORIGINAL-TO RETURN-PATH LIST-ID"
)

ProgressCallback = Callable[[dict[str, Any]], None]
ByteProgressCallback = Callable[[int, int], None]
MESSAGE_READER_LIMIT = 5 * 1024 * 1024
ATTACHMENT_DOWNLOAD_LIMIT = 256 * 1024 * 1024
ATTACHMENT_FETCH_CHUNK_SIZE = 1024 * 1024


class AttachmentDownloadCancelled(RuntimeError):
    """Raised when an attachment transfer is cancelled by the user."""


class _HTMLTextExtractor(HTMLParser):
    BLOCK_TAGS = {
        "address",
        "article",
        "blockquote",
        "br",
        "div",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
    }
    HIDDEN_TAGS = {"head", "script", "style", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(
        self,
        tag: str,
        _attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized = tag.lower()
        if normalized in self.HIDDEN_TAGS:
            self.hidden_depth += 1
        elif not self.hidden_depth and normalized in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in self.HIDDEN_TAGS:
            self.hidden_depth = max(0, self.hidden_depth - 1)
        elif not self.hidden_depth and normalized in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.parts.append(data)


def html_to_plain_text(value: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(value)
    parser.close()
    lines = [
        re.sub(r"[ \t\f\v]+", " ", line).strip()
        for line in "".join(parser.parts).replace("\xa0", " ").splitlines()
    ]
    output: list[str] = []
    for line in lines:
        if line:
            output.append(line)
        elif output and output[-1]:
            output.append("")
    return "\n".join(output).strip()


def _text_part_content(part: Any) -> str:
    try:
        content = part.get_content()
        if isinstance(content, str):
            return content
    except (LookupError, UnicodeDecodeError, ValueError):
        pass
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def parse_message_for_reader(raw_message: bytes) -> dict[str, str]:
    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    plain_text = ""
    html_text = ""
    for part in message.walk():
        if part.is_multipart():
            continue
        if str(part.get_content_disposition() or "").lower() == "attachment":
            continue
        content_type = part.get_content_type().lower()
        if content_type == "text/plain" and not plain_text:
            plain_text = _text_part_content(part).strip()
        elif content_type == "text/html" and not html_text:
            html_text = html_to_plain_text(_text_part_content(part))

    body = plain_text or html_text
    return {
        "subject": str(message.get("Subject") or ""),
        "from": str(message.get("From") or ""),
        "to": str(message.get("To") or ""),
        "cc": str(message.get("Cc") or ""),
        "date": str(message.get("Date") or ""),
        "body": body,
    }


def _decode_transfer_payload(
    payload: bytes,
    transfer_encoding: str,
) -> bytes:
    normalized = str(transfer_encoding or "").strip().lower()
    try:
        if normalized == "base64":
            return base64.b64decode(payload, validate=False)
        if normalized == "quoted-printable":
            return quopri.decodestring(payload)
        return payload
    except (binascii.Error, ValueError) as exc:
        raise RuntimeError(
            f"Não foi possível decodificar os dados MIME: {exc}"
        ) from exc


def parse_reader_part(
    raw_headers: bytes,
    raw_body: bytes,
    part: dict[str, Any],
) -> dict[str, str]:
    message = BytesParser(policy=policy.default).parsebytes(
        raw_headers.rstrip(b"\r\n") + b"\r\n\r\n"
    )
    decoded = _decode_transfer_payload(
        raw_body,
        str(part.get("transfer_encoding") or ""),
    )
    charset = str(part.get("charset") or "").strip() or "utf-8"
    try:
        body = decoded.decode(charset, errors="replace")
    except LookupError:
        body = decoded.decode("utf-8", errors="replace")
    if str(part.get("content_type") or "").lower() == "text/html":
        body = html_to_plain_text(body)
    else:
        body = body.strip()
    return {
        "subject": str(message.get("Subject") or ""),
        "from": str(message.get("From") or ""),
        "to": str(message.get("To") or ""),
        "cc": str(message.get("Cc") or ""),
        "date": str(message.get("Date") or ""),
        "body": body,
    }


def decode_modified_utf7(value: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(value):
        if value[index] != "&":
            result.append(value[index])
            index += 1
            continue
        end = value.find("-", index)
        if end == -1:
            result.append(value[index:])
            break
        token = value[index + 1 : end]
        if not token:
            result.append("&")
        else:
            padded = token.replace(",", "/") + "=" * (-len(token) % 4)
            try:
                result.append(base64.b64decode(padded).decode("utf-16-be"))
            except (binascii.Error, UnicodeDecodeError):
                result.append(value[index : end + 1])
        index = end + 1
    return "".join(result)


def encode_modified_utf7(value: str) -> str:
    output: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if not buffer:
            return
        encoded = base64.b64encode("".join(buffer).encode("utf-16-be"))
        output.append("&" + encoded.decode("ascii").rstrip("=").replace("/", ",") + "-")
        buffer.clear()

    for char in value:
        codepoint = ord(char)
        if 0x20 <= codepoint <= 0x7E:
            flush()
            output.append("&-" if char == "&" else char)
        else:
            buffer.append(char)
    flush()
    return "".join(output)


def quote_mailbox(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unquote_imap(value: str) -> str:
    value = value.strip()
    if value.upper() == "NIL":
        return ""
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return re.sub(r"\\(.)", r"\1", value[1:-1])
    return value


def parse_list_line(raw: bytes | str) -> dict[str, Any]:
    text = raw.decode("ascii", errors="replace") if isinstance(raw, bytes) else raw
    match = re.match(
        r"^\((?P<flags>[^)]*)\)\s+"
        r"(?P<delimiter>NIL|\"(?:\\.|[^\"])*\")\s+"
        r"(?P<name>.+)$",
        text.strip(),
    )
    if not match:
        raise ValueError(f"Resposta LIST desconhecida: {text[:160]}")
    flags = [flag for flag in match.group("flags").split() if flag]
    delimiter = _unquote_imap(match.group("delimiter"))
    wire_name = _unquote_imap(match.group("name"))
    remote_name = decode_modified_utf7(wire_name)
    special_use = next(
        (
            flag
            for flag in flags
            if flag.lower()
            in {
                "\\all",
                "\\inbox",
                "\\sent",
                "\\drafts",
                "\\junk",
                "\\trash",
                "\\important",
                "\\flagged",
            }
        ),
        None,
    )
    if remote_name.upper() == "INBOX":
        special_use = "\\Inbox"
    return {
        "remote_name": remote_name,
        "delimiter": delimiter or None,
        "flags": flags,
        "special_use": special_use,
        "selectable": not any(flag.lower() == "\\noselect" for flag in flags),
    }


def parse_status(data: list[bytes] | None) -> dict[str, int | None]:
    text = b" ".join(data or []).decode("ascii", errors="ignore")

    def number(name: str) -> int | None:
        match = re.search(rf"\b{name}\s+(\d+)", text, re.IGNORECASE)
        return int(match.group(1)) if match else None

    return {
        "messages_count": number("MESSAGES"),
        "uidnext": number("UIDNEXT"),
        "uidvalidity": number("UIDVALIDITY"),
    }


def compress_uid_set(uids: list[int]) -> str:
    if not uids:
        return ""
    ordered = sorted(set(uids))
    ranges: list[str] = []
    start = previous = ordered[0]
    for uid in ordered[1:]:
        if uid == previous + 1:
            previous = uid
            continue
        ranges.append(str(start) if start == previous else f"{start}:{previous}")
        start = previous = uid
    ranges.append(str(start) if start == previous else f"{start}:{previous}")
    return ",".join(ranges)


def expand_uid_set(value: str) -> list[int]:
    result: list[int] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            result.append(int(part))
            continue
        first_text, last_text = part.split(":", 1)
        first = int(first_text)
        last = int(last_text)
        step = 1 if last >= first else -1
        result.extend(range(first, last + step, step))
    return result


def parse_copyuid(values: Any) -> dict[int, int]:
    text = imap_response_text(values, limit=4000)
    match = re.search(
        r"\bCOPYUID\s+\d+\s+([0-9:,]+)\s+([0-9:,]+)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return {}
    source_uids = expand_uid_set(match.group(1))
    destination_uids = expand_uid_set(match.group(2))
    if len(source_uids) != len(destination_uids):
        return {}
    return dict(zip(source_uids, destination_uids))


def copyuid_mapping(client: Any, values: Any) -> dict[int, int]:
    candidates: list[Any] = [values]
    response = getattr(client, "response", None)
    if callable(response):
        try:
            candidates.append(response("COPYUID"))
        except (imaplib.IMAP4.error, TypeError):
            pass
    return parse_copyuid(candidates)


def gmail_label_set(labels_json: str | None) -> str:
    try:
        payload = json.loads(labels_json or "[]")
    except (TypeError, json.JSONDecodeError):
        payload = []
    labels = [
        str(label)
        for label in payload
        if str(label).lower() not in {"\\trash", "\\all"}
    ]
    return "(" + " ".join(
        quote_mailbox(encode_modified_utf7(label)) for label in labels
    ) + ")"


def normalize_capabilities(values: Any) -> set[str]:
    capabilities: set[str] = set()
    for value in values or []:
        text = (
            value.decode("ascii", errors="ignore")
            if isinstance(value, bytes)
            else str(value)
        )
        capabilities.update(
            token.upper()
            for token in text.split()
            if token
        )
    return capabilities


def imap_response_text(values: Any, limit: int = 300) -> str:
    if values is None:
        return ""
    if not isinstance(values, (list, tuple)):
        values = [values]
    parts: list[str] = []
    for value in values:
        if value is None:
            continue
        text = (
            value.decode("utf-8", errors="replace")
            if isinstance(value, bytes)
            else str(value)
        )
        text = " ".join(text.split())
        if text:
            parts.append(text)
    return " · ".join(parts)[: max(1, int(limit))]


class ImapConnection:
    def __init__(self, account: dict[str, Any], password: str):
        self.account = account
        self.password = password
        self.client: imaplib.IMAP4 | None = None
        self.gmail_extensions = False
        self.capabilities: set[str] = set()

    def __enter__(self) -> "ImapConnection":
        context = ssl.create_default_context()
        if self.account.get("security", "ssl") == "ssl":
            self.client = imaplib.IMAP4_SSL(
                self.account["host"],
                int(self.account["port"]),
                ssl_context=context,
                timeout=45,
            )
        else:
            self.client = imaplib.IMAP4(
                self.account["host"], int(self.account["port"]), timeout=45
            )
            self.client.starttls(ssl_context=context)
        password = (
            self.password.replace(" ", "")
            if self.account["provider"] == "gmail"
            else self.password
        )
        self.client.login(self.account["email"], password)
        self.capabilities = normalize_capabilities(
            getattr(self.client, "capabilities", ())
        )
        try:
            status, values = self.client.capability()
            if status == "OK":
                self.capabilities.update(normalize_capabilities(values))
        except (imaplib.IMAP4.error, OSError):
            pass
        self.gmail_extensions = "X-GM-EXT-1" in self.capabilities
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self.client is None:
            return
        try:
            self.client.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


class MailExtractor:
    def __init__(self, database: Database):
        self.database = database

    def test_connection(self, account: dict[str, Any], password: str) -> dict[str, Any]:
        with ImapConnection(account, password) as connection:
            return {"gmail_extensions": connection.gmail_extensions}

    def fetch_message_for_reader(
        self,
        account: dict[str, Any],
        password: str,
        target: dict[str, Any],
        max_bytes: int = MESSAGE_READER_LIMIT,
    ) -> dict[str, str]:
        mailbox_name = str(target.get("mailbox_name") or "").strip()
        uid = int(target.get("uid") or 0)
        if not mailbox_name or uid <= 0:
            raise RuntimeError(
                "A mensagem não possui uma localização IMAP válida. "
                "Sincronize a conta e tente novamente."
            )

        with ImapConnection(account, password) as connection:
            client = connection.client
            assert client is not None
            mailbox_wire = encode_modified_utf7(mailbox_name)
            status, _ = client.select(
                quote_mailbox(mailbox_wire),
                readonly=True,
            )
            if status != "OK":
                raise RuntimeError(
                    f'Não foi possível abrir “{mailbox_name}” para leitura.'
                )

            status, values = client.uid(
                "FETCH",
                str(uid),
                "(BODY.PEEK[HEADER] BODYSTRUCTURE)",
            )
            if status != "OK":
                detail = imap_response_text(values)
                raise RuntimeError(
                    "O servidor não conseguiu localizar ou ler esta mensagem."
                    + (f" Detalhes: {detail}" if detail else "")
                )

            metadata_chunks: list[bytes] = []
            raw_headers: bytes | None = None
            for item in values or []:
                if isinstance(item, tuple):
                    if item and isinstance(item[0], bytes):
                        metadata_chunks.append(item[0])
                    if (
                        len(item) >= 2
                        and isinstance(item[1], bytes)
                        and raw_headers is None
                    ):
                        raw_headers = item[1]
                elif isinstance(item, bytes):
                    metadata_chunks.append(item)
            if raw_headers is None:
                raise RuntimeError(
                    "O servidor não retornou o cabeçalho desta mensagem."
                )
            try:
                structure = parse_bodystructure_fetch_item(
                    b" ".join(metadata_chunks)
                )
            except ValueError as exc:
                raise RuntimeError(
                    "O servidor não retornou uma estrutura MIME utilizável "
                    "para esta mensagem."
                ) from exc

            message_pk = int(target.get("id") or 0)
            if self.database is not None and message_pk > 0:
                self.database.store_attachment_analysis(
                    [
                        {
                            "message_pk": message_pk,
                            "attachments": structure["attachments"],
                        }
                    ]
                )

            reader_parts = [
                item
                for item in structure.get("reader_parts") or []
                if int(item.get("size_bytes") or 0) <= max_bytes
            ]
            reader_parts.sort(
                key=lambda item: (
                    str(item.get("content_type") or "").lower() != "text/plain",
                    int(item.get("size_bytes") or 0),
                )
            )
            if not reader_parts:
                raise RuntimeError(
                    "Esta mensagem não possui uma parte de texto de até 5 MB "
                    "que o leitor leve possa exibir. Os anexos continuam "
                    "disponíveis para download."
                )
            reader_part = reader_parts[0]
            part_number = str(reader_part.get("part_number") or "")
            status, part_values = client.uid(
                "FETCH",
                str(uid),
                f"(BODY.PEEK[{part_number}])",
            )
            if status != "OK":
                detail = imap_response_text(part_values)
                raise RuntimeError(
                    "O servidor não conseguiu ler a parte textual da mensagem."
                    + (f" Detalhes: {detail}" if detail else "")
                )
            raw_body = next(
                (
                    item[1]
                    for item in part_values or []
                    if isinstance(item, tuple)
                    and len(item) >= 2
                    and isinstance(item[1], bytes)
                ),
                None,
            )
            if raw_body is None:
                raise RuntimeError(
                    "O servidor não retornou a parte textual da mensagem."
                )
            if len(raw_body) > max_bytes:
                raise RuntimeError(
                    "A parte textual desta mensagem ultrapassa o limite de "
                    "5 MB do leitor leve."
                )
            return parse_reader_part(raw_headers, raw_body, reader_part)

    def fetch_attachment(
        self,
        account: dict[str, Any],
        password: str,
        target: dict[str, Any],
        attachment: dict[str, Any],
        max_bytes: int = ATTACHMENT_DOWNLOAD_LIMIT,
        progress: ByteProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
        chunk_size: int = ATTACHMENT_FETCH_CHUNK_SIZE,
    ) -> bytes:
        mailbox_name = str(target.get("mailbox_name") or "").strip()
        uid = int(target.get("uid") or 0)
        part_number = str(attachment.get("part_number") or "").strip()
        encoded_size = int(attachment.get("size_bytes") or 0)
        if not mailbox_name or uid <= 0:
            raise RuntimeError(
                "A mensagem não possui uma localização IMAP válida. "
                "Sincronize a conta e tente novamente."
            )
        if not re.fullmatch(r"\d+(?:\.\d+)*", part_number):
            raise RuntimeError("A seção IMAP deste anexo é inválida.")
        if encoded_size > max_bytes:
            raise RuntimeError(
                "Este anexo ultrapassa o limite de segurança de 256 MB "
                "para download pelo aplicativo."
            )
        chunk_size = max(4, min(int(chunk_size), 4 * 1024 * 1024))
        chunk_size -= chunk_size % 4

        def check_cancelled() -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise AttachmentDownloadCancelled()

        check_cancelled()
        if progress is not None:
            progress(0, encoded_size)

        with ImapConnection(account, password) as connection:
            check_cancelled()
            client = connection.client
            assert client is not None
            mailbox_wire = encode_modified_utf7(mailbox_name)
            status, _ = client.select(
                quote_mailbox(mailbox_wire),
                readonly=True,
            )
            if status != "OK":
                raise RuntimeError(
                    f'Não foi possível abrir “{mailbox_name}” para leitura.'
                )
            check_cancelled()

            def fetch_section(fetch_item: str) -> bytes:
                check_cancelled()
                fetch_status, values = client.uid(
                    "FETCH",
                    str(uid),
                    fetch_item,
                )
                check_cancelled()
                if fetch_status != "OK":
                    detail = imap_response_text(values)
                    raise RuntimeError(
                        "O servidor não conseguiu baixar este anexo."
                        + (f" Detalhes: {detail}" if detail else "")
                    )
                payload = next(
                    (
                        item[1]
                        for item in values or []
                        if isinstance(item, tuple)
                        and len(item) >= 2
                        and isinstance(item[1], bytes)
                    ),
                    None,
                )
                if payload is None:
                    raise RuntimeError(
                        "O servidor não retornou os dados do anexo."
                    )
                return payload

            if encoded_size <= chunk_size or encoded_size <= 0:
                encoded = fetch_section(
                    f"(BODY.PEEK[{part_number}])"
                )
                if progress is not None:
                    progress(len(encoded), max(encoded_size, len(encoded)))
            else:
                chunks: list[bytes] = []
                received = 0
                while received < encoded_size:
                    check_cancelled()
                    requested = min(chunk_size, encoded_size - received)
                    chunk = fetch_section(
                        f"(BODY.PEEK[{part_number}]"
                        f"<{received}.{requested}>)"
                    )
                    if not chunk:
                        break
                    chunks.append(chunk)
                    received += len(chunk)
                    if received > max_bytes:
                        raise RuntimeError(
                            "Este anexo ultrapassa o limite de segurança de "
                            "256 MB para download pelo aplicativo."
                        )
                    if progress is not None:
                        progress(
                            min(received, encoded_size),
                            encoded_size,
                        )
                    if len(chunk) != requested:
                        break
                encoded = b"".join(chunks)
                if not encoded:
                    raise RuntimeError(
                        "O servidor não retornou os dados do anexo."
                    )
                if progress is not None and received != encoded_size:
                    progress(len(encoded), len(encoded))
            if len(encoded) > max_bytes:
                raise RuntimeError(
                    "Este anexo ultrapassa o limite de segurança de 256 MB "
                    "para download pelo aplicativo."
                )
            check_cancelled()

        payload = _decode_transfer_payload(
            encoded,
            str(attachment.get("transfer_encoding") or ""),
        )
        check_cancelled()
        return payload

    def analyze_attachments(
        self,
        account: dict[str, Any],
        password: str,
        targets: list[dict[str, Any]],
        progress: ProgressCallback,
        cancel_event: threading.Event,
        pause_event: threading.Event,
        batch_size: int = 250,
    ) -> dict[str, Any]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for target in targets:
            mailbox_name = str(target.get("mailbox_name") or "").strip()
            uid = int(target.get("uid") or 0)
            if mailbox_name and uid > 0:
                grouped[mailbox_name].append(target)

        total = sum(len(items) for items in grouped.values())
        processed = indexed = attachments_found = errors = 0
        progress({"type": "planned", "total": total})
        with ImapConnection(account, password) as connection:
            client = connection.client
            assert client is not None
            for mailbox_name, mailbox_targets in grouped.items():
                if cancel_event.is_set():
                    break
                wire_name = encode_modified_utf7(mailbox_name)
                status, _ = client.select(
                    quote_mailbox(wire_name),
                    readonly=True,
                )
                if status != "OK":
                    raise RuntimeError(
                        f'Não foi possível abrir “{mailbox_name}”.'
                    )
                mailbox_targets.sort(key=lambda item: int(item["uid"]))
                for start in range(0, len(mailbox_targets), batch_size):
                    while pause_event.is_set() and not cancel_event.is_set():
                        time.sleep(0.15)
                    if cancel_event.is_set():
                        break
                    batch = mailbox_targets[start : start + batch_size]
                    targets_by_uid = {
                        int(item["uid"]): item for item in batch
                    }
                    uid_set = compress_uid_set(list(targets_by_uid))
                    progress(
                        {
                            "type": "batch",
                            "mailbox": mailbox_name,
                            "amount": len(batch),
                            "processed": processed,
                            "total": total,
                            "uid_first": min(targets_by_uid),
                            "uid_last": max(targets_by_uid),
                        }
                    )
                    status, values = client.uid(
                        "FETCH",
                        uid_set,
                        "(UID BODYSTRUCTURE)",
                    )
                    if status != "OK":
                        detail = imap_response_text(values)
                        raise RuntimeError(
                            f"Falha ao analisar o lote {uid_set} de "
                            f'“{mailbox_name}”.'
                            + (f" Detalhes: {detail}" if detail else "")
                        )
                    records: list[dict[str, Any]] = []
                    seen_uids: set[int] = set()
                    for item in values or []:
                        metadata: bytes | str | None = None
                        if isinstance(item, tuple) and item:
                            metadata = item[0]
                        elif isinstance(item, (bytes, str)):
                            metadata = item
                        if not metadata:
                            continue
                        metadata_text = (
                            metadata.decode("utf-8", errors="replace")
                            if isinstance(metadata, bytes)
                            else metadata
                        )
                        if (
                            re.search(
                                r"\bUID\s+\d+",
                                metadata_text,
                                re.IGNORECASE,
                            )
                            is None
                            or "BODYSTRUCTURE" not in metadata_text.upper()
                        ):
                            continue
                        try:
                            parsed = parse_bodystructure_fetch_item(
                                metadata_text
                            )
                            uid = int(parsed["uid"])
                            target = targets_by_uid.get(uid)
                            if target is None:
                                continue
                            seen_uids.add(uid)
                            records.append(
                                {
                                    "message_pk": int(target["message_pk"]),
                                    "attachments": parsed["attachments"],
                                }
                            )
                            indexed += 1
                            attachments_found += len(parsed["attachments"])
                        except Exception:
                            errors += 1
                    errors += len(set(targets_by_uid) - seen_uids)
                    self.database.store_attachment_analysis(records)
                    processed += len(batch)
                    progress(
                        {
                            "type": "progress",
                            "mailbox": mailbox_name,
                            "processed": processed,
                            "total": total,
                            "indexed": indexed,
                            "attachments": attachments_found,
                            "errors": errors,
                        }
                    )
        return {
            "status": "cancelled" if cancel_event.is_set() else "completed",
            "processed": processed,
            "total": total,
            "indexed": indexed,
            "attachments": attachments_found,
            "errors": errors,
        }

    def discover_mailboxes(
        self,
        account: dict[str, Any],
        password: str,
        progress: ProgressCallback | None = None,
    ) -> list[dict[str, Any]]:
        progress = progress or (lambda event: None)
        progress({"type": "phase", "text": "Conectando ao servidor IMAP…"})
        folders: list[dict[str, Any]] = []
        with ImapConnection(account, password) as connection:
            client = connection.client
            assert client is not None
            progress({"type": "phase", "text": "Descobrindo pastas…"})
            status, data = client.list()
            if status != "OK":
                raise RuntimeError("O servidor não retornou a lista de pastas.")
            parsed = []
            for line in data or []:
                if not line:
                    continue
                try:
                    item = parse_list_line(line)
                except ValueError:
                    continue
                if item["selectable"]:
                    parsed.append(item)
            if not parsed:
                raise RuntimeError(
                    "O servidor não retornou nenhuma pasta IMAP selecionável."
                )
            for index, item in enumerate(parsed, start=1):
                progress(
                    {
                        "type": "folder",
                        "text": (
                            f'Verificando pasta {index} de {len(parsed)}: '
                            f'{item["remote_name"]}'
                        ),
                        "current": index,
                        "total": len(parsed),
                    }
                )
                wire_name = encode_modified_utf7(item["remote_name"])
                status, values = client.status(
                    quote_mailbox(wire_name), "(MESSAGES UIDNEXT UIDVALIDITY)"
                )
                if status == "OK":
                    item.update(parse_status(values))
                else:
                    item.update(
                        {"messages_count": None, "uidnext": None, "uidvalidity": None}
                    )
                folders.append(item)
        has_all = any(
            (item.get("special_use") or "").lower() == "\\all"
            for item in folders
        )
        for item in folders:
            special = (item.get("special_use") or "").lower()
            item["selected"] = special == "\\all" if has_all else special == "\\inbox"
        return folders

    def move_to_trash(
        self,
        account: dict[str, Any],
        password: str,
        targets: list[dict[str, Any]],
        trash_mailbox: dict[str, Any],
        progress: ProgressCallback | None = None,
        batch_size: int = 250,
        cancel_event: threading.Event | None = None,
        pause_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        progress = progress or (lambda event: None)
        if not targets:
            return {
                "status": "completed",
                "moved": 0,
                "message_ids": [],
            }
        if (trash_mailbox.get("special_use") or "").lower() != "\\trash":
            raise RuntimeError(
                "A pasta de destino não foi identificada pelo servidor como Lixeira."
            )

        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for target in targets:
            grouped[str(target["mailbox_name"])].append(target)

        total = len(targets)
        moved_ids: list[int] = []
        undo_items: list[dict[str, Any]] = []
        cancelled = False
        trash_wire = encode_modified_utf7(str(trash_mailbox["remote_name"]))
        progress(
            {
                "type": "cleanup",
                "text": f"Preparando {total:,} mensagens…".replace(",", "."),
                "moved": 0,
                "total": total,
            }
        )

        with ImapConnection(account, password) as connection:
            client = connection.client
            assert client is not None
            supports_move = "MOVE" in connection.capabilities
            supports_uidplus = "UIDPLUS" in connection.capabilities
            supports_gmail_labels = bool(
                getattr(connection, "gmail_extensions", False)
            )
            if (
                not supports_move
                and not supports_uidplus
                and not supports_gmail_labels
            ):
                raise RuntimeError(
                    "Este servidor não oferece MOVE, UIDPLUS nem o mecanismo "
                    "de Lixeira do Gmail. A operação foi cancelada para não "
                    "expurgar outras mensagens por engano."
                )
            if supports_gmail_labels:
                cleanup_method = "X-GM-LABELS"
            elif supports_move:
                cleanup_method = "MOVE"
            else:
                cleanup_method = "UIDPLUS"
            progress(
                {
                    "type": "cleanup_capabilities",
                    "text": (
                        f"Método seguro selecionado pelo servidor: "
                        f"{cleanup_method}."
                    ),
                    "method": cleanup_method,
                    "moved": 0,
                    "total": total,
                }
            )

            for mailbox_name, mailbox_targets in grouped.items():
                while pause_event is not None and pause_event.is_set():
                    if cancel_event is not None and cancel_event.is_set():
                        cancelled = True
                        break
                    time.sleep(0.1)
                if cancelled or (
                    cancel_event is not None and cancel_event.is_set()
                ):
                    cancelled = True
                    break
                if mailbox_name == trash_mailbox["remote_name"]:
                    continue
                source_wire = encode_modified_utf7(mailbox_name)
                status, _ = client.select(
                    quote_mailbox(source_wire), readonly=False
                )
                if status != "OK":
                    raise RuntimeError(
                        f'Não foi possível abrir “{mailbox_name}” para gravação.'
                    )

                mailbox_batches = (
                    len(mailbox_targets) + batch_size - 1
                ) // batch_size
                for start in range(0, len(mailbox_targets), batch_size):
                    while pause_event is not None and pause_event.is_set():
                        if cancel_event is not None and cancel_event.is_set():
                            cancelled = True
                            break
                        time.sleep(0.1)
                    if cancelled or (
                        cancel_event is not None and cancel_event.is_set()
                    ):
                        cancelled = True
                        break
                    batch = mailbox_targets[start : start + batch_size]
                    batch_number = (start // batch_size) + 1
                    uid_set = compress_uid_set([int(item["uid"]) for item in batch])
                    batch_undo_items: list[dict[str, Any]] = []
                    progress(
                        {
                            "type": "cleanup",
                            "text": (
                                f'Lote {batch_number}/{mailbox_batches} de '
                                f'“{mailbox_name}”: movendo {len(batch):,} '
                                "mensagens…"
                            ).replace(
                                ",", "."
                            ),
                            "moved": len(moved_ids),
                            "total": total,
                            "mailbox": mailbox_name,
                            "batch_number": batch_number,
                            "mailbox_batches": mailbox_batches,
                            "batch_amount": len(batch),
                            "method": cleanup_method,
                        }
                    )
                    if supports_gmail_labels:
                        status, gmail_values = client.uid(
                            "STORE",
                            uid_set,
                            "+X-GM-LABELS",
                            r"(\Trash)",
                        )
                        if status != "OK":
                            copy_status, copy_values = client.uid(
                                "COPY",
                                uid_set,
                                quote_mailbox(trash_wire),
                            )
                            if copy_status != "OK":
                                store_detail = imap_response_text(
                                    gmail_values
                                )
                                copy_detail = imap_response_text(
                                    copy_values
                                )
                                details = " · ".join(
                                    item
                                    for item in (
                                        f"STORE: {store_detail}"
                                        if store_detail
                                        else "",
                                        f"COPY: {copy_detail}"
                                        if copy_detail
                                        else "",
                                    )
                                    if item
                                )
                                raise RuntimeError(
                                    (
                                        "O Gmail recusou mover um lote de "
                                        f"{len(batch):,} mensagens de "
                                        f'“{mailbox_name}” para a Lixeira.'
                                    ).replace(",", ".")
                                    + (
                                        f" Detalhes do servidor: {details}"
                                        if details
                                        else ""
                                    )
                                )
                        for item in batch:
                            provider_id = str(
                                item.get("provider_message_id") or ""
                            )
                            internet_message_id = str(
                                item.get("message_id") or ""
                            ).strip()
                            if provider_id.isdigit():
                                strategy = "gmail_msgid"
                                locator = {
                                    "provider_message_id": provider_id,
                                }
                            elif internet_message_id:
                                strategy = "gmail_header"
                                locator = {
                                    "message_id": internet_message_id,
                                }
                            else:
                                continue
                            batch_undo_items.append(
                                {
                                    "strategy": strategy,
                                    "message_pk": int(item["message_pk"]),
                                    "source_mailbox": mailbox_name,
                                    "labels_json": str(
                                        item.get("labels_json") or "[]"
                                    ),
                                    **locator,
                                }
                            )
                    elif supports_move:
                        status, move_values = client.uid(
                            "MOVE", uid_set, quote_mailbox(trash_wire)
                        )
                        if status != "OK":
                            raise RuntimeError(
                                (
                                    "O servidor recusou mover um lote de "
                                    f"{len(batch):,} mensagens de "
                                    f'“{mailbox_name}”.'
                                ).replace(",", ".")
                            )
                        uid_mapping = copyuid_mapping(client, move_values)
                        batch_undo_items = [
                            {
                                "strategy": "trash_uid",
                                "message_pk": int(item["message_pk"]),
                                "trash_uid": int(uid_mapping[int(item["uid"])]),
                                "source_mailbox": mailbox_name,
                            }
                            for item in batch
                            if int(item["uid"]) in uid_mapping
                            and str(item.get("special_use") or "").lower()
                            != "\\all"
                        ]
                    else:
                        status, copy_values = client.uid(
                            "COPY", uid_set, quote_mailbox(trash_wire)
                        )
                        if status != "OK":
                            raise RuntimeError(
                                (
                                    "O servidor recusou copiar um lote de "
                                    f"{len(batch):,} mensagens para a Lixeira."
                                ).replace(",", ".")
                            )
                        uid_mapping = copyuid_mapping(client, copy_values)
                        batch_undo_items = [
                            {
                                "strategy": "trash_uid",
                                "message_pk": int(item["message_pk"]),
                                "trash_uid": int(uid_mapping[int(item["uid"])]),
                                "source_mailbox": mailbox_name,
                            }
                            for item in batch
                            if int(item["uid"]) in uid_mapping
                            and str(item.get("special_use") or "").lower()
                            != "\\all"
                        ]
                        status, _ = client.uid(
                            "STORE", uid_set, "+FLAGS.SILENT", r"(\Deleted)"
                        )
                        if status != "OK":
                            raise RuntimeError(
                                (
                                    "O servidor recusou marcar um lote de "
                                    f"{len(batch):,} mensagens para remoção."
                                ).replace(",", ".")
                            )
                        status, _ = client.uid("EXPUNGE", uid_set)
                        if status != "OK":
                            raise RuntimeError(
                                (
                                    "O servidor recusou concluir um lote de "
                                    f"{len(batch):,} mensagens com UIDPLUS."
                                ).replace(",", ".")
                            )

                    moved_ids.extend(int(item["message_pk"]) for item in batch)
                    undo_items.extend(batch_undo_items)
                    progress(
                        {
                            "type": "cleanup",
                            "text": (
                                f'{len(moved_ids):,} de {total:,} mensagens '
                                "movidas para a Lixeira"
                            ).replace(",", "."),
                            "moved": len(moved_ids),
                            "total": total,
                            "message_ids": [
                                int(item["message_pk"]) for item in batch
                            ],
                            "mailbox": mailbox_name,
                            "batch_number": batch_number,
                            "mailbox_batches": mailbox_batches,
                            "batch_amount": len(batch),
                            "method": cleanup_method,
                            "undo_items": batch_undo_items,
                        }
                    )
                if cancelled:
                    break

        return {
            "status": "cancelled" if cancelled else "completed",
            "moved": len(moved_ids),
            "message_ids": moved_ids,
            "undo_items": undo_items,
            "undo_supported": bool(moved_ids)
            and len(undo_items) == len(moved_ids),
            "undo_available": len(undo_items),
        }

    def restore_from_trash(
        self,
        account: dict[str, Any],
        password: str,
        undo_items: list[dict[str, Any]],
        trash_mailbox: dict[str, Any],
        progress: ProgressCallback | None = None,
        batch_size: int = 100,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        progress = progress or (lambda event: None)
        if not undo_items:
            return {
                "status": "completed",
                "restored": 0,
                "message_ids": [],
            }
        if (trash_mailbox.get("special_use") or "").lower() != "\\trash":
            raise RuntimeError(
                "A pasta de origem não foi identificada pelo servidor como Lixeira."
            )
        strategies = {
            str(item.get("strategy") or "") for item in undo_items
        }
        gmail_strategies = {"gmail_msgid", "gmail_header"}
        valid_strategy_set = (
            strategies == {"trash_uid"}
            or bool(strategies)
            and strategies <= gmail_strategies
        )
        if not valid_strategy_set:
            raise RuntimeError(
                "Os identificadores de reversão não usam estratégias "
                "compatíveis e seguras."
            )

        total = len(undo_items)
        restored_ids: list[int] = []
        cancelled = False
        trash_wire = encode_modified_utf7(str(trash_mailbox["remote_name"]))
        with ImapConnection(account, password) as connection:
            client = connection.client
            assert client is not None
            status, _ = client.select(
                quote_mailbox(trash_wire),
                readonly=False,
            )
            if status != "OK":
                raise RuntimeError(
                    "Não foi possível abrir a Lixeira para reverter a operação."
                )

            if strategies <= gmail_strategies:
                if not getattr(connection, "gmail_extensions", False):
                    raise RuntimeError(
                        "O servidor deixou de oferecer os identificadores "
                        "estáveis do Gmail necessários para a reversão."
                    )
                for index, item in enumerate(undo_items, start=1):
                    if cancel_event is not None and cancel_event.is_set():
                        cancelled = True
                        break
                    if item["strategy"] == "gmail_msgid":
                        status, values = client.uid(
                            "SEARCH",
                            None,
                            "X-GM-MSGID",
                            str(item["provider_message_id"]),
                        )
                    else:
                        status, values = client.uid(
                            "SEARCH",
                            None,
                            "HEADER",
                            "Message-ID",
                            quote_mailbox(str(item["message_id"])),
                        )
                    found = [
                        int(value)
                        for value in re.findall(
                            r"\d+",
                            imap_response_text(values, limit=4000),
                        )
                    ]
                    if status != "OK" or len(found) != 1:
                        raise RuntimeError(
                            "Não foi possível localizar de forma única na "
                            "Lixeira uma mensagem já movida."
                        )
                    trash_uid = str(found[0])
                    labels = gmail_label_set(
                        str(item.get("labels_json") or "[]")
                    )
                    if labels != "()":
                        status, _ = client.uid(
                            "STORE",
                            trash_uid,
                            "+X-GM-LABELS",
                            labels,
                        )
                        if status != "OK":
                            raise RuntimeError(
                                "O Gmail recusou restaurar os marcadores "
                                "originais de uma mensagem."
                            )
                    status, _ = client.uid(
                        "STORE",
                        trash_uid,
                        "-X-GM-LABELS",
                        r"(\Trash)",
                    )
                    if status != "OK":
                        raise RuntimeError(
                            "O Gmail recusou retirar uma mensagem da Lixeira."
                        )
                    restored_ids.append(int(item["message_pk"]))
                    progress(
                        {
                            "type": "undo",
                            "text": (
                                f"{index:,} de {total:,} mensagens restauradas"
                            ).replace(",", "."),
                            "restored": len(restored_ids),
                            "total": total,
                            "message_ids": [int(item["message_pk"])],
                        }
                    )
            elif strategies == {"trash_uid"}:
                grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
                for item in undo_items:
                    grouped[str(item["source_mailbox"])].append(item)
                supports_move = "MOVE" in connection.capabilities
                supports_uidplus = "UIDPLUS" in connection.capabilities
                if not supports_move and not supports_uidplus:
                    raise RuntimeError(
                        "O servidor deixou de oferecer MOVE ou UIDPLUS, "
                        "necessário para uma reversão segura."
                    )
                for source_mailbox, mailbox_items in grouped.items():
                    source_wire = encode_modified_utf7(source_mailbox)
                    for start in range(0, len(mailbox_items), batch_size):
                        if cancel_event is not None and cancel_event.is_set():
                            cancelled = True
                            break
                        batch = mailbox_items[start : start + batch_size]
                        uid_set = compress_uid_set(
                            [int(item["trash_uid"]) for item in batch]
                        )
                        if supports_move:
                            status, _ = client.uid(
                                "MOVE",
                                uid_set,
                                quote_mailbox(source_wire),
                            )
                            if status != "OK":
                                raise RuntimeError(
                                    f'O servidor recusou restaurar mensagens '
                                    f'para “{source_mailbox}”.'
                                )
                        else:
                            status, _ = client.uid(
                                "COPY",
                                uid_set,
                                quote_mailbox(source_wire),
                            )
                            if status != "OK":
                                raise RuntimeError(
                                    f'O servidor recusou copiar mensagens de '
                                    f'volta para “{source_mailbox}”.'
                                )
                            status, _ = client.uid(
                                "STORE",
                                uid_set,
                                "+FLAGS.SILENT",
                                r"(\Deleted)",
                            )
                            if status != "OK":
                                raise RuntimeError(
                                    "O servidor recusou concluir a reversão."
                                )
                            status, _ = client.uid("EXPUNGE", uid_set)
                            if status != "OK":
                                raise RuntimeError(
                                    "O servidor recusou expurgar somente as "
                                    "cópias já restauradas da Lixeira."
                                )
                        message_ids = [
                            int(item["message_pk"]) for item in batch
                        ]
                        restored_ids.extend(message_ids)
                        progress(
                            {
                                "type": "undo",
                                "text": (
                                    f"{len(restored_ids):,} de {total:,} "
                                    "mensagens restauradas"
                                ).replace(",", "."),
                                "restored": len(restored_ids),
                                "total": total,
                                "message_ids": message_ids,
                            }
                        )
                    if cancelled:
                        break
            else:
                raise RuntimeError(
                    "A estratégia de reversão informada não é reconhecida."
                )

        return {
            "status": "cancelled" if cancelled else "completed",
            "restored": len(restored_ids),
            "message_ids": restored_ids,
        }

    def sync(
        self,
        account: dict[str, Any],
        password: str,
        mailboxes: list[dict[str, Any]],
        progress: ProgressCallback,
        cancel_event: threading.Event,
        pause_event: threading.Event,
        batch_size: int = 1000,
    ) -> dict[str, Any]:
        progress({"type": "phase", "text": "Comparando com o servidor…"})
        planned: list[tuple[dict[str, Any], list[int], list[int]]] = []
        job_id: int | None = None
        processed = inserted = updated = errors = 0
        checked = missing = restored = 0
        started = time.monotonic()

        try:
            with ImapConnection(account, password) as connection:
                client = connection.client
                assert client is not None
                progress(
                    {
                        "type": "capabilities",
                        "qresync": "QRESYNC" in connection.capabilities,
                        "condstore": "CONDSTORE" in connection.capabilities,
                        "gmail": connection.gmail_extensions,
                    }
                )
                for mailbox in mailboxes:
                    if cancel_event.is_set():
                        break
                    wire_name = encode_modified_utf7(mailbox["remote_name"])
                    status, _ = client.select(quote_mailbox(wire_name), readonly=True)
                    if status != "OK":
                        raise RuntimeError(
                            f'Não foi possível abrir a pasta “{mailbox["remote_name"]}”.'
                        )
                    status, data = client.uid("search", None, "ALL")
                    if status != "OK":
                        raise RuntimeError(
                            "Não foi possível listar as mensagens de "
                            f'“{mailbox["remote_name"]}”.'
                        )
                    uids = [
                        int(value)
                        for value in b" ".join(data or []).split()
                        if value.isdigit()
                    ]
                    current_uids = sorted(set(uids))
                    known_uids = self.database.mailbox_known_uids(
                        int(mailbox["id"]),
                        mailbox.get("uidvalidity"),
                    )
                    new_uids = (
                        []
                        if mailbox.get("action_status_only")
                        else [
                            uid
                            for uid in current_uids
                            if uid not in known_uids
                        ]
                    )
                    missing_candidates = len(known_uids - set(current_uids))
                    checked += len(current_uids)
                    progress(
                        {
                            "type": "mailbox_plan",
                            "mailbox": mailbox["remote_name"],
                            "messages": len(current_uids),
                            "new_messages": len(new_uids),
                            "missing_candidates": missing_candidates,
                            "uidvalidity": mailbox.get("uidvalidity"),
                        }
                    )
                    planned.append((mailbox, current_uids, new_uids))

                total = sum(len(new_uids) for _, _, new_uids in planned)
                job_id = self.database.create_job(account["id"], "sync", total)
                progress(
                    {
                        "type": "planned",
                        "total": total,
                        "checked": checked,
                        "job_id": job_id,
                        "text": (
                            f"{checked:,} mensagens comparadas · "
                            f"{total:,} cabeçalhos novos para baixar"
                        ).replace(",", "."),
                    }
                )

                gmail_items = (
                    "X-GM-MSGID X-GM-THRID X-GM-LABELS "
                    if connection.gmail_extensions
                    else ""
                )
                fetch_items = (
                    f"(UID FLAGS INTERNALDATE RFC822.SIZE BODYSTRUCTURE {gmail_items}"
                    f"BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])"
                )

                def fetch_with_retry(
                    uid_set: str, mailbox_name: str, wire_name: str
                ) -> list[Any]:
                    nonlocal client
                    last_detail = ""
                    for attempt in range(1, 4):
                        try:
                            status, response = client.uid(
                                "fetch", uid_set, fetch_items
                            )
                            if status == "OK":
                                return response or []
                            last_detail = f"resposta {status}"
                        except (imaplib.IMAP4.abort, OSError) as exc:
                            last_detail = str(exc)
                        if attempt == 3:
                            break
                        delay = 2 ** (attempt - 1)
                        progress(
                            {
                                "type": "phase",
                                "text": (
                                    f'Conexão interrompida em “{mailbox_name}”. '
                                    f"Nova tentativa em {delay}s…"
                                ),
                            }
                        )
                        time.sleep(delay)
                        connection.__exit__(None, None, None)
                        connection.__enter__()
                        client = connection.client
                        assert client is not None
                        selected_status, _ = client.select(
                            quote_mailbox(wire_name), readonly=True
                        )
                        if selected_status != "OK":
                            last_detail = "não foi possível reabrir a pasta"
                    raise RuntimeError(
                        f"Falha ao obter o lote {uid_set} de “{mailbox_name}” "
                        f"após três tentativas: {last_detail}"
                    )

                for mailbox, current_uids, new_uids in planned:
                    if cancel_event.is_set():
                        break
                    wire_name = encode_modified_utf7(mailbox["remote_name"])
                    status, _ = client.select(quote_mailbox(wire_name), readonly=True)
                    if status != "OK":
                        raise RuntimeError(
                            f'Não foi possível abrir a pasta “{mailbox["remote_name"]}”.'
                        )
                    mailbox_completed = True
                    for start in range(0, len(new_uids), batch_size):
                        while pause_event.is_set() and not cancel_event.is_set():
                            time.sleep(0.15)
                        if cancel_event.is_set():
                            mailbox_completed = False
                            break
                        batch = new_uids[start : start + batch_size]
                        uid_set = compress_uid_set(batch)
                        batch_number = start // batch_size + 1
                        mailbox_batches = (
                            len(new_uids) + batch_size - 1
                        ) // batch_size
                        progress(
                            {
                                "type": "batch",
                                "mailbox": mailbox["remote_name"],
                                "processed": processed,
                                "total": total,
                                "batch_amount": len(batch),
                                "batch_number": batch_number,
                                "mailbox_batches": mailbox_batches,
                                "uid_first": min(batch),
                                "uid_last": max(batch),
                            }
                        )
                        data = fetch_with_retry(
                            uid_set, mailbox["remote_name"], wire_name
                        )
                        records: list[dict[str, Any]] = []
                        errors_before_batch = errors
                        for item in data:
                            if not isinstance(item, tuple) or len(item) < 2:
                                continue
                            try:
                                records.append(
                                    parse_fetch_item(
                                        item[0],
                                        item[1],
                                        mailbox["remote_name"],
                                        connection.gmail_extensions,
                                    )
                                )
                            except Exception as exc:
                                errors += 1
                                self.database.add_job_error(
                                    job_id,
                                    "parse",
                                    str(exc),
                                    mailbox["remote_name"],
                                    None,
                                )
                        batch_inserted, batch_updated = self.database.store_batch(
                            account["id"],
                            int(mailbox["id"]),
                            records,
                            max(batch),
                            mailbox.get("uidvalidity"),
                        )
                        inserted += batch_inserted
                        updated += batch_updated
                        processed += len(batch)
                        elapsed = max(time.monotonic() - started, 0.001)
                        self.database.update_job(
                            job_id,
                            processed=processed,
                            inserted=inserted,
                            updated=updated,
                            errors=errors,
                            detail=mailbox["remote_name"],
                        )
                        progress(
                            {
                                "type": "progress",
                                "mailbox": mailbox["remote_name"],
                                "processed": processed,
                                "total": total,
                                "inserted": inserted,
                                "updated": updated,
                                "errors": errors,
                                "rate": processed / elapsed,
                                "batch_amount": len(batch),
                                "batch_number": batch_number,
                                "mailbox_batches": mailbox_batches,
                                "batch_inserted": batch_inserted,
                                "batch_updated": batch_updated,
                                "batch_errors": errors - errors_before_batch,
                                "records_parsed": len(records),
                                "uid_first": min(batch),
                                "uid_last": max(batch),
                            }
                        )

                    if mailbox_completed and not cancel_event.is_set():
                        reconciliation = (
                            self.database.reconcile_mailbox_snapshot(
                                int(mailbox["id"]),
                                current_uids,
                                mailbox.get("uidvalidity"),
                            )
                        )
                        missing += int(reconciliation["missing"])
                        restored += int(reconciliation["restored"])
                        progress(
                            {
                                "type": "reconciled",
                                "mailbox": mailbox["remote_name"],
                                "current": reconciliation["current"],
                                "missing": reconciliation["missing"],
                                "restored": reconciliation["restored"],
                                "processed": processed,
                                "total": total,
                                "inserted": inserted,
                                "updated": updated,
                                "errors": errors,
                            }
                        )

            status_name = "cancelled" if cancel_event.is_set() else "completed"
            state_counts = self.database.recompute_account_states(account["id"])
            if job_id is not None:
                self.database.update_job(
                    job_id,
                    status=status_name,
                    processed=processed,
                    inserted=inserted,
                    updated=updated,
                    errors=errors,
                    finished_at=utc_now(),
                )
            if status_name == "completed":
                self.database.mark_account_synced(account["id"])
            return {
                "status": status_name,
                "job_id": job_id,
                "processed": processed,
                "inserted": inserted,
                "updated": updated,
                "errors": errors,
                "checked": checked,
                "missing": missing,
                "restored": restored,
                "active": state_counts["active"],
                "trashed": state_counts["trashed"],
            }
        except Exception as exc:
            if job_id is not None:
                self.database.update_job(
                    job_id,
                    status="failed",
                    processed=processed,
                    inserted=inserted,
                    updated=updated,
                    errors=errors + 1,
                    detail=str(exc),
                    finished_at=utc_now(),
                )
                self.database.add_job_error(job_id, "sync", str(exc))
            raise
