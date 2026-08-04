from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from email.header import decode_header
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any
from urllib.parse import unquote

from .i18n import tr


def header_text(message: Message, name: str) -> str:
    value = message.get(name)
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:
        return str(value).strip()


def normalize_email(value: str) -> str:
    return value.strip().strip("<>").lower()


def email_domain(address: str) -> str:
    address = normalize_email(address)
    if "@" not in address:
        return ""
    domain = address.rsplit("@", 1)[1].rstrip(".")
    try:
        return domain.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return domain.lower()


def parse_addresses(message: Message, name: str) -> list[dict[str, str]]:
    values = message.get_all(name, [])
    decoded = [str(value) for value in values]
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for display_name, address in getaddresses(decoded):
        normalized = normalize_email(address)
        key = (display_name.strip(), normalized)
        if not normalized and not display_name.strip():
            continue
        if key in seen:
            continue
        seen.add(key)
        result.append(
            {
                "name": display_name.strip(),
                "email": normalized,
                "domain": email_domain(normalized),
            }
        )
    return result


def format_addresses(addresses: list[dict[str, str]]) -> str:
    formatted = []
    for item in addresses:
        if item["name"] and item["email"]:
            formatted.append(f'{item["name"]} <{item["email"]}>')
        else:
            formatted.append(item["email"] or item["name"])
    return "; ".join(formatted)


def parse_header_date(raw: str) -> str:
    if not raw:
        return ""
    try:
        parsed = parsedate_to_datetime(raw)
        if parsed is None:
            return ""
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError):
        return ""


def parse_internal_date(raw: str) -> str:
    if not raw:
        return ""
    try:
        parsed = datetime.strptime(raw, "%d-%b-%Y %H:%M:%S %z")
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return ""


def parse_imap_tokens(raw: str) -> list[str]:
    tokens: list[str] = []
    current: list[str] = []
    quoted = False
    escaped = False
    for char in raw.strip():
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\" and quoted:
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif char.isspace() and not quoted:
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(char)
    if current:
        tokens.append("".join(current))
    return tokens


def _metadata_text(metadata: bytes | str) -> str:
    return (
        metadata.decode("utf-8", errors="replace")
        if isinstance(metadata, bytes)
        else metadata
    )


def _regex_value(pattern: str, metadata: str) -> str:
    match = re.search(pattern, metadata, re.IGNORECASE)
    return match.group(1) if match else ""


def _parenthesized_value(metadata: str, key: str) -> str:
    match = re.search(rf"\b{re.escape(key)}\s+\(", metadata, re.IGNORECASE)
    if not match:
        return ""
    start = match.end()
    depth = 1
    quoted = False
    escaped = False
    for index in range(start, len(metadata)):
        char = metadata[index]
        if escaped:
            escaped = False
        elif char == "\\" and quoted:
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif not quoted and char == "(":
            depth += 1
        elif not quoted and char == ")":
            depth -= 1
            if depth == 0:
                return metadata[start:index]
    return ""


def _decode_mime_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "''" in text:
        charset, encoded = text.split("''", 1)
        try:
            return unquote(encoded, encoding=charset or "utf-8", errors="replace")
        except LookupError:
            return unquote(encoded, encoding="utf-8", errors="replace")
    try:
        parts = []
        for payload, charset in decode_header(text):
            if isinstance(payload, bytes):
                parts.append(payload.decode(charset or "utf-8", errors="replace"))
            else:
                parts.append(payload)
        return "".join(parts).strip()
    except Exception:
        return text


def _bodystructure_tokens(value: str) -> list[str]:
    tokens: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char.isspace():
            index += 1
            continue
        if char in "()":
            tokens.append(char)
            index += 1
            continue
        if char == '"':
            index += 1
            current: list[str] = []
            while index < len(value):
                char = value[index]
                if char == "\\" and index + 1 < len(value):
                    index += 1
                    current.append(value[index])
                elif char == '"':
                    index += 1
                    break
                else:
                    current.append(char)
                index += 1
            tokens.append("".join(current))
            continue
        start = index
        while (
            index < len(value)
            and not value[index].isspace()
            and value[index] not in "()"
        ):
            index += 1
        tokens.append(value[start:index])
    return tokens


def _parse_bodystructure_tree(value: str) -> list[Any]:
    tokens = _bodystructure_tokens(value)
    position = 0

    def parse_value() -> Any:
        nonlocal position
        if position >= len(tokens):
            raise ValueError("BODYSTRUCTURE incompleto")
        token = tokens[position]
        position += 1
        if token == "(":
            result = []
            while position < len(tokens) and tokens[position] != ")":
                result.append(parse_value())
            if position >= len(tokens):
                raise ValueError("BODYSTRUCTURE sem fechamento")
            position += 1
            return result
        if token == ")":
            raise ValueError("Fechamento inesperado em BODYSTRUCTURE")
        if token.upper() == "NIL":
            return None
        if token.isdigit():
            return int(token)
        return token

    parsed = parse_value()
    if not isinstance(parsed, list):
        raise ValueError(tr("BODYSTRUCTURE inválido"))
    return parsed


def _body_parameters(value: Any) -> dict[str, str]:
    if not isinstance(value, list):
        return {}
    result: dict[str, str] = {}
    for index in range(0, len(value) - 1, 2):
        key = str(value[index] or "").strip().upper()
        if key:
            result[key] = _decode_mime_text(value[index + 1])
    return result


def _body_disposition(values: list[Any]) -> tuple[str, dict[str, str]]:
    for value in values:
        if (
            isinstance(value, list)
            and value
            and str(value[0] or "").upper() in {"ATTACHMENT", "INLINE"}
        ):
            return (
                str(value[0]).upper(),
                _body_parameters(value[1] if len(value) > 1 else None),
            )
    return "", {}


def _attachment_extension(filename: str) -> str:
    clean = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].strip()
    if "." not in clean:
        return ""
    extension = clean.rsplit(".", 1)[1].lower()
    return extension if re.fullmatch(r"[a-z0-9][a-z0-9+_-]{0,15}", extension) else ""


def _bodystructure_attachments(
    tree: list[Any],
    prefix: str = "",
) -> list[dict[str, Any]]:
    if not tree:
        return []

    # Multipart bodies begin with one or more child body structures.
    if isinstance(tree[0], list):
        attachments: list[dict[str, Any]] = []
        child_index = 0
        for child in tree:
            if not isinstance(child, list):
                break
            child_index += 1
            part_number = f"{prefix}.{child_index}" if prefix else str(child_index)
            attachments.extend(_bodystructure_attachments(child, part_number))
        return attachments

    media_type = str(tree[0] or "").upper() if len(tree) > 0 else ""
    media_subtype = str(tree[1] or "").upper() if len(tree) > 1 else ""
    content_type = (
        f"{media_type.lower()}/{media_subtype.lower()}"
        if media_type and media_subtype
        else "application/octet-stream"
    )
    type_parameters = _body_parameters(tree[2] if len(tree) > 2 else None)
    transfer_encoding = str(tree[5] or "").lower() if len(tree) > 5 else ""
    encoded_size = int(tree[6]) if len(tree) > 6 and isinstance(tree[6], int) else 0
    disposition, disposition_parameters = _body_disposition(tree[7:])
    filename = (
        disposition_parameters.get("FILENAME")
        or disposition_parameters.get("FILENAME*")
        or type_parameters.get("NAME")
        or type_parameters.get("NAME*")
        or ""
    )
    filename = _decode_mime_text(filename)
    is_attachment = disposition == "ATTACHMENT" or bool(filename)

    attachments: list[dict[str, Any]] = []
    if is_attachment and prefix:
        attachments.append(
            {
                "part_number": prefix,
                "filename": filename or f"attachment-{prefix}",
                "extension": _attachment_extension(filename),
                "content_type": content_type,
                "disposition": disposition or "ATTACHMENT",
                "transfer_encoding": transfer_encoding,
                "size_bytes": encoded_size,
            }
        )

    # A message/rfc822 part may contain a nested body. Only recurse when the
    # outer message is not itself the downloadable attachment.
    if (
        not is_attachment
        and media_type == "MESSAGE"
        and media_subtype == "RFC822"
        and len(tree) > 8
        and isinstance(tree[8], list)
    ):
        nested_prefix = f"{prefix}.1" if prefix else "1"
        attachments.extend(
            _bodystructure_attachments(tree[8], nested_prefix)
        )
    return attachments


def _bodystructure_reader_parts(
    tree: list[Any],
    prefix: str = "",
) -> list[dict[str, Any]]:
    if not tree:
        return []
    if isinstance(tree[0], list):
        parts: list[dict[str, Any]] = []
        child_index = 0
        for child in tree:
            if not isinstance(child, list):
                break
            child_index += 1
            part_number = f"{prefix}.{child_index}" if prefix else str(child_index)
            parts.extend(_bodystructure_reader_parts(child, part_number))
        return parts

    media_type = str(tree[0] or "").upper() if len(tree) > 0 else ""
    media_subtype = str(tree[1] or "").upper() if len(tree) > 1 else ""
    if media_type == "TEXT" and media_subtype in {"PLAIN", "HTML"} and prefix:
        parameters = _body_parameters(tree[2] if len(tree) > 2 else None)
        return [
            {
                "part_number": prefix,
                "content_type": f"text/{media_subtype.lower()}",
                "charset": parameters.get("CHARSET", ""),
                "transfer_encoding": (
                    str(tree[5] or "").lower() if len(tree) > 5 else ""
                ),
                "size_bytes": (
                    int(tree[6])
                    if len(tree) > 6 and isinstance(tree[6], int)
                    else 0
                ),
            }
        ]
    if (
        media_type == "MESSAGE"
        and media_subtype == "RFC822"
        and len(tree) > 8
        and isinstance(tree[8], list)
    ):
        nested_prefix = f"{prefix}.1" if prefix else "1"
        return _bodystructure_reader_parts(tree[8], nested_prefix)
    return []


def parse_bodystructure_fetch_item(
    metadata: bytes | str,
) -> dict[str, Any]:
    metadata_text = _metadata_text(metadata)
    uid_raw = _regex_value(r"\bUID\s+(\d+)", metadata_text)
    if not uid_raw:
            raise ValueError(tr("Resposta BODYSTRUCTURE sem UID"))
    raw_structure = _parenthesized_value(metadata_text, "BODYSTRUCTURE")
    if not raw_structure:
            raise ValueError(tr("Resposta FETCH sem BODYSTRUCTURE"))
    tree = _parse_bodystructure_tree(f"({raw_structure})")
    root_prefix = "" if tree and isinstance(tree[0], list) else "1"
    attachments = _bodystructure_attachments(
        tree,
        root_prefix,
    )
    reader_parts = _bodystructure_reader_parts(tree, root_prefix)
    return {
        "uid": int(uid_raw),
        "attachments": attachments,
        "attachment_indexed": True,
        "attachment_count": len(attachments),
        "attachment_size_bytes": sum(
            int(item.get("size_bytes") or 0) for item in attachments
        ),
        "reader_parts": reader_parts,
    }


def parse_fetch_item(
    metadata: bytes | str,
    header_bytes: bytes,
    mailbox_name: str,
    gmail_extensions: bool,
) -> dict[str, Any]:
    metadata_text = _metadata_text(metadata)
    uid_raw = _regex_value(r"\bUID\s+(\d+)", metadata_text)
    if not uid_raw:
        raise ValueError(tr("Resposta FETCH sem UID"))
    uid = int(uid_raw)
    message = BytesParser(policy=policy.default).parsebytes(header_bytes or b"")

    from_addresses = parse_addresses(message, "From")
    sender_addresses = parse_addresses(message, "Sender")
    reply_to = parse_addresses(message, "Reply-To")
    to_addresses = parse_addresses(message, "To")
    cc_addresses = parse_addresses(message, "Cc")
    bcc_addresses = parse_addresses(message, "Bcc")
    primary_from = from_addresses[0] if from_addresses else {}
    primary_sender = sender_addresses[0] if sender_addresses else {}

    recipients: list[dict[str, Any]] = []
    for kind, addresses in (
        ("TO", to_addresses),
        ("CC", cc_addresses),
        ("BCC", bcc_addresses),
    ):
        for position, address in enumerate(addresses):
            recipients.append(
                {
                    "kind": kind,
                    "position": position,
                    "name": address["name"],
                    "email": address["email"],
                    "domain": address["domain"],
                }
            )

    flags_raw = _parenthesized_value(metadata_text, "FLAGS")
    labels_raw = _parenthesized_value(metadata_text, "X-GM-LABELS")
    internal_raw = _regex_value(r'\bINTERNALDATE\s+"([^"]+)"', metadata_text)
    size_raw = _regex_value(r"\bRFC822\.SIZE\s+(\d+)", metadata_text)
    gmail_message_id = _regex_value(r"\bX-GM-MSGID\s+(\d+)", metadata_text)
    gmail_thread_id = _regex_value(r"\bX-GM-THRID\s+(\d+)", metadata_text)
    internet_message_id = header_text(message, "Message-ID")
    provider_message_id = gmail_message_id if gmail_extensions else internet_message_id
    if not provider_message_id:
        digest = hashlib.sha256()
        digest.update(mailbox_name.encode("utf-8", errors="replace"))
        digest.update(str(uid).encode("ascii"))
        digest.update(header_bytes)
        provider_message_id = f"fallback:{digest.hexdigest()}"

    bodystructure: dict[str, Any] = {
        "attachments": [],
        "attachment_indexed": False,
        "attachment_count": 0,
        "attachment_size_bytes": 0,
    }
    if re.search(r"\bBODYSTRUCTURE\s+\(", metadata_text, re.IGNORECASE):
        bodystructure = parse_bodystructure_fetch_item(metadata_text)

    date_raw = header_text(message, "Date")
    return {
        "uid": uid,
        "provider_message_id": provider_message_id,
        "provider_thread_id": gmail_thread_id or "",
        "source_mailbox": mailbox_name,
        "source_uid": uid,
        "message_id": internet_message_id,
        "from_name": primary_from.get("name", ""),
        "from_email": primary_from.get("email", ""),
        "from_domain": primary_from.get("domain", ""),
        "sender_name": primary_sender.get("name", ""),
        "sender_email": primary_sender.get("email", ""),
        "sender_domain": primary_sender.get("domain", ""),
        "reply_to": format_addresses(reply_to),
        "to_addresses": format_addresses(to_addresses),
        "cc_addresses": format_addresses(cc_addresses),
        "bcc_addresses": format_addresses(bcc_addresses),
        "subject": header_text(message, "Subject"),
        "date_header_raw": date_raw,
        "date_sent_utc": parse_header_date(date_raw),
        "internal_date_utc": parse_internal_date(internal_raw),
        "delivered_to": header_text(message, "Delivered-To"),
        "x_original_to": header_text(message, "X-Original-To"),
        "return_path": header_text(message, "Return-Path"),
        "in_reply_to": header_text(message, "In-Reply-To"),
        "message_references": header_text(message, "References"),
        "list_id": header_text(message, "List-ID"),
        "flags_json": json.dumps(parse_imap_tokens(flags_raw), ensure_ascii=False),
        "labels_json": json.dumps(parse_imap_tokens(labels_raw), ensure_ascii=False),
        "size_bytes": int(size_raw) if size_raw else None,
        "recipients": recipients,
        **bodystructure,
    }
