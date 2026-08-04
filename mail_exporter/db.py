from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .i18n import tr


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name TEXT NOT NULL,
    email TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'gmail',
    host TEXT NOT NULL,
    port INTEGER NOT NULL DEFAULT 993,
    security TEXT NOT NULL DEFAULT 'ssl',
    auth_type TEXT NOT NULL DEFAULT 'password',
    save_secret INTEGER NOT NULL DEFAULT 0,
    encrypted_secret TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_sync_at TEXT,
    UNIQUE(email, host)
);

CREATE TABLE IF NOT EXISTS mailboxes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    remote_name TEXT NOT NULL,
    delimiter TEXT,
    special_use TEXT,
    flags_json TEXT NOT NULL DEFAULT '[]',
    messages_count INTEGER,
    uidnext INTEGER,
    uidvalidity INTEGER,
    last_uid INTEGER NOT NULL DEFAULT 0,
    selected INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    UNIQUE(account_id, remote_name)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    provider_message_id TEXT NOT NULL,
    provider_thread_id TEXT,
    source_mailbox TEXT,
    source_uid INTEGER,
    message_id TEXT,
    from_name TEXT,
    from_email TEXT,
    from_domain TEXT,
    sender_name TEXT,
    sender_email TEXT,
    sender_domain TEXT,
    reply_to TEXT,
    to_addresses TEXT,
    cc_addresses TEXT,
    bcc_addresses TEXT,
    subject TEXT,
    date_header_raw TEXT,
    date_sent_utc TEXT,
    internal_date_utc TEXT,
    delivered_to TEXT,
    x_original_to TEXT,
    return_path TEXT,
    in_reply_to TEXT,
    message_references TEXT,
    list_id TEXT,
    flags_json TEXT NOT NULL DEFAULT '[]',
    labels_json TEXT NOT NULL DEFAULT '[]',
    size_bytes INTEGER,
    attachment_indexed INTEGER NOT NULL DEFAULT 0,
    attachment_count INTEGER NOT NULL DEFAULT 0,
    attachment_size_bytes INTEGER NOT NULL DEFAULT 0,
    attachment_indexed_at TEXT,
    state TEXT NOT NULL DEFAULT 'active',
    last_seen_at TEXT,
    missing_since TEXT,
    trashed_at TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(account_id, provider_message_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_account_date
    ON messages(account_id, date_sent_utc);
CREATE INDEX IF NOT EXISTS idx_messages_from_domain
    ON messages(account_id, from_domain);
CREATE INDEX IF NOT EXISTS idx_messages_account_from_email
    ON messages(account_id, lower(trim(from_email)));
CREATE INDEX IF NOT EXISTS idx_messages_account_from_domain
    ON messages(account_id, lower(trim(from_domain)));

CREATE TABLE IF NOT EXISTS recipients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id_fk INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    position INTEGER NOT NULL,
    name TEXT,
    email TEXT,
    domain TEXT,
    UNIQUE(message_id_fk, kind, position)
);

CREATE INDEX IF NOT EXISTS idx_recipients_email
    ON recipients(email);

CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id_fk INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    part_number TEXT NOT NULL,
    filename TEXT NOT NULL,
    extension TEXT NOT NULL DEFAULT '',
    content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    disposition TEXT NOT NULL DEFAULT 'ATTACHMENT',
    transfer_encoding TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(message_id_fk, part_number)
);

CREATE INDEX IF NOT EXISTS idx_attachments_message
    ON attachments(message_id_fk);
CREATE INDEX IF NOT EXISTS idx_attachments_extension
    ON attachments(extension);
CREATE TABLE IF NOT EXISTS message_mailboxes (
    message_id_fk INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    mailbox_id INTEGER NOT NULL REFERENCES mailboxes(id) ON DELETE CASCADE,
    uid INTEGER NOT NULL,
    uidvalidity INTEGER,
    last_seen_at TEXT,
    missing_since TEXT,
    PRIMARY KEY(message_id_fk, mailbox_id)
);

CREATE TABLE IF NOT EXISTS sync_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    total INTEGER NOT NULL DEFAULT 0,
    processed INTEGER NOT NULL DEFAULT 0,
    inserted INTEGER NOT NULL DEFAULT 0,
    updated INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS sync_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES sync_jobs(id) ON DELETE CASCADE,
    mailbox_name TEXT,
    uid INTEGER,
    error_type TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_accounts(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT a.*,
                       COUNT(DISTINCT m.id) AS message_count,
                       COUNT(DISTINCT mb.id) AS mailbox_count
                FROM accounts a
                LEFT JOIN messages m
                       ON m.account_id = a.id AND m.state='active'
                LEFT JOIN mailboxes mb ON mb.account_id = a.id
                GROUP BY a.id
                ORDER BY lower(a.display_name), lower(a.email)
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_account(self, account_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
        return dict(row) if row else None

    def save_account(self, values: dict[str, Any]) -> int:
        now = utc_now()
        fields = (
            values["display_name"].strip(),
            values["email"].strip().lower(),
            values.get("provider", "gmail"),
            values["host"].strip(),
            int(values.get("port", 993)),
            values.get("security", "ssl"),
            values.get("auth_type", "password"),
            1,
            now,
        )
        with self.connect() as conn:
            if values.get("id"):
                conn.execute(
                    """
                    UPDATE accounts
                       SET display_name=?, email=?, provider=?, host=?, port=?,
                           security=?, auth_type=?, save_secret=?, updated_at=?
                     WHERE id=?
                    """,
                    fields + (int(values["id"]),),
                )
                return int(values["id"])
            cursor = conn.execute(
                """
                INSERT INTO accounts(
                    display_name, email, provider, host, port, security,
                    auth_type, save_secret, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                fields[:-1] + (now, now),
            )
            account_id = int(cursor.lastrowid)
            if values.get("encrypted_secret"):
                conn.execute(
                    "UPDATE accounts SET encrypted_secret=? WHERE id=?",
                    (values["encrypted_secret"], account_id),
                )
            return account_id

    def update_encrypted_secret(self, account_id: int, encrypted_secret: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE accounts
                   SET encrypted_secret=?, save_secret=1, updated_at=?
                 WHERE id=?
                """,
                (encrypted_secret, utc_now(), account_id),
            )

    def delete_account(self, account_id: int, keep_data: bool = False) -> None:
        with self.connect() as conn:
            if keep_data:
                conn.execute(
                    """
                    UPDATE accounts
                       SET save_secret=0, encrypted_secret='', updated_at=?
                     WHERE id=?
                    """,
                    (utc_now(), account_id),
                )
            else:
                conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))

    def clear_account_data(self, account_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM messages WHERE account_id=?", (account_id,))
            conn.execute("DELETE FROM mailboxes WHERE account_id=?", (account_id,))
            conn.execute("DELETE FROM sync_jobs WHERE account_id=?", (account_id,))
            conn.execute(
                "UPDATE accounts SET last_sync_at=NULL, updated_at=? WHERE id=?",
                (utc_now(), account_id),
            )

    def rebuild_account_index(self, account_id: int) -> None:
        """Remove only synchronized metadata while preserving the account."""
        now = utc_now()
        with self.connect() as conn:
            conn.execute("DELETE FROM messages WHERE account_id=?", (account_id,))
            conn.execute("DELETE FROM sync_jobs WHERE account_id=?", (account_id,))
            conn.execute(
                """
                UPDATE mailboxes
                   SET last_uid=0, updated_at=?
                 WHERE account_id=?
                """,
                (now, account_id),
            )
            conn.execute(
                """
                UPDATE accounts
                   SET last_sync_at=NULL, updated_at=?
                 WHERE id=?
                """,
                (now, account_id),
            )

    def replace_mailboxes(
        self, account_id: int, mailboxes: Iterable[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        items = list(mailboxes)
        now = utc_now()
        with self.connect() as conn:
            for item in items:
                existing = conn.execute(
                    """
                    SELECT id, uidvalidity FROM mailboxes
                     WHERE account_id=? AND remote_name=?
                    """,
                    (account_id, item["remote_name"]),
                ).fetchone()
                new_validity = item.get("uidvalidity")
                if (
                    existing
                    and existing["uidvalidity"] is not None
                    and new_validity is not None
                    and int(existing["uidvalidity"]) != int(new_validity)
                ):
                    conn.execute(
                        "DELETE FROM message_mailboxes WHERE mailbox_id=?",
                        (int(existing["id"]),),
                    )
                conn.execute(
                    """
                    INSERT INTO mailboxes(
                        account_id, remote_name, delimiter, special_use,
                        flags_json, messages_count, uidnext, uidvalidity,
                        selected, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_id, remote_name) DO UPDATE SET
                        delimiter=excluded.delimiter,
                        special_use=excluded.special_use,
                        flags_json=excluded.flags_json,
                        messages_count=excluded.messages_count,
                        uidnext=excluded.uidnext,
                        last_uid=CASE
                            WHEN mailboxes.uidvalidity IS NOT NULL
                             AND excluded.uidvalidity IS NOT NULL
                             AND mailboxes.uidvalidity <> excluded.uidvalidity
                            THEN 0 ELSE mailboxes.last_uid END,
                        uidvalidity=excluded.uidvalidity,
                        updated_at=excluded.updated_at
                    """,
                    (
                        account_id,
                        item["remote_name"],
                        item.get("delimiter"),
                        item.get("special_use"),
                        json.dumps(item.get("flags", []), ensure_ascii=False),
                        item.get("messages_count"),
                        item.get("uidnext"),
                        item.get("uidvalidity"),
                        1 if item.get("selected") else 0,
                        now,
                    ),
                )
            remote_names = [str(item["remote_name"]) for item in items]
            if remote_names:
                placeholders = ",".join("?" for _ in remote_names)
                conn.execute(
                    f"""
                    DELETE FROM mailboxes
                     WHERE account_id=?
                       AND remote_name NOT IN ({placeholders})
                    """,
                    (account_id, *remote_names),
                )
            rows = conn.execute(
                """
                SELECT * FROM mailboxes
                 WHERE account_id=?
                 ORDER BY CASE lower(COALESCE(special_use, ''))
                     WHEN '\\all' THEN 0
                     WHEN '\\inbox' THEN 1
                     WHEN '\\sent' THEN 2
                     WHEN '\\drafts' THEN 3
                     WHEN '\\trash' THEN 4
                     WHEN '\\junk' THEN 5
                     ELSE 6
                 END,
                 lower(remote_name)
                """,
                (account_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_mailbox_selection(self, account_id: int, mailbox_ids: list[int]) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE mailboxes SET selected=0 WHERE account_id=?", (account_id,)
            )
            conn.executemany(
                "UPDATE mailboxes SET selected=1 WHERE account_id=? AND id=?",
                ((account_id, mailbox_id) for mailbox_id in mailbox_ids),
            )

    def get_mailboxes(
        self, account_id: int, selected_only: bool = False
    ) -> list[dict[str, Any]]:
        where = " AND selected=1" if selected_only else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM mailboxes
                 WHERE account_id=? {where}
                 ORDER BY CASE lower(COALESCE(special_use, ''))
                     WHEN '\\all' THEN 0
                     WHEN '\\inbox' THEN 1
                     WHEN '\\sent' THEN 2
                     WHEN '\\drafts' THEN 3
                     WHEN '\\trash' THEN 4
                     WHEN '\\junk' THEN 5
                     ELSE 6
                 END,
                 lower(remote_name)
                """,
                (account_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_action_reconciliation_mailboxes(
        self,
        account_id: int,
        source_names: Iterable[str],
    ) -> list[dict[str, Any]]:
        """Return only the source folders and Trash needed after an action."""
        requested = {
            str(name).strip()
            for name in source_names
            if str(name).strip()
        }
        mailboxes = self.get_mailboxes(account_id)
        chosen = [
            dict(mailbox)
            for mailbox in mailboxes
            if str(mailbox["remote_name"]) in requested
        ]
        if not chosen:
            chosen.extend(
                dict(mailbox)
                for mailbox in mailboxes
                if int(mailbox.get("selected") or 0)
                and str(mailbox.get("special_use") or "").lower() != "\\trash"
            )
        chosen.extend(
            {
                **dict(mailbox),
                "action_status_only": True,
            }
            for mailbox in mailboxes
            if str(mailbox.get("special_use") or "").lower() == "\\trash"
            and all(
                int(item["id"]) != int(mailbox["id"])
                for item in chosen
            )
        )
        return chosen

    def create_job(self, account_id: int, mode: str, total: int) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO sync_jobs(account_id, mode, status, total, started_at)
                VALUES (?, ?, 'running', ?, ?)
                """,
                (account_id, mode, total, utc_now()),
            )
            return int(cursor.lastrowid)

    def update_job(self, job_id: int, **values: Any) -> None:
        allowed = {
            "status",
            "total",
            "processed",
            "inserted",
            "updated",
            "errors",
            "detail",
            "finished_at",
        }
        items = [(key, value) for key, value in values.items() if key in allowed]
        if not items:
            return
        sql = ", ".join(f"{key}=?" for key, _ in items)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE sync_jobs SET {sql} WHERE id=?",
                tuple(value for _, value in items) + (job_id,),
            )

    def add_job_error(
        self,
        job_id: int,
        error_type: str,
        detail: str,
        mailbox_name: str | None = None,
        uid: int | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO sync_errors(
                    job_id, mailbox_name, uid, error_type, detail, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (job_id, mailbox_name, uid, error_type, detail, utc_now()),
            )

    def store_batch(
        self,
        account_id: int,
        mailbox_id: int,
        records: list[dict[str, Any]],
        last_uid: int,
        uidvalidity: int | None,
    ) -> tuple[int, int]:
        inserted = 0
        updated = 0
        now = utc_now()
        message_columns = [
            "provider_thread_id",
            "source_mailbox",
            "source_uid",
            "message_id",
            "from_name",
            "from_email",
            "from_domain",
            "sender_name",
            "sender_email",
            "sender_domain",
            "reply_to",
            "to_addresses",
            "cc_addresses",
            "bcc_addresses",
            "subject",
            "date_header_raw",
            "date_sent_utc",
            "internal_date_utc",
            "delivered_to",
            "x_original_to",
            "return_path",
            "in_reply_to",
            "message_references",
            "list_id",
            "flags_json",
            "labels_json",
            "size_bytes",
        ]
        with self.connect() as conn:
            for record in records:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO messages(
                        account_id, provider_message_id, first_seen_at, updated_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        account_id,
                        record["provider_message_id"],
                        now,
                        now,
                    ),
                )
                if cursor.rowcount:
                    inserted += 1
                else:
                    updated += 1
                assignments = ", ".join(f"{column}=?" for column in message_columns)
                conn.execute(
                    f"""
                    UPDATE messages SET {assignments}, updated_at=?
                     WHERE account_id=? AND provider_message_id=?
                    """,
                    tuple(record.get(column) for column in message_columns)
                    + (now, account_id, record["provider_message_id"]),
                )
                message_row = conn.execute(
                    """
                    SELECT id FROM messages
                     WHERE account_id=? AND provider_message_id=?
                    """,
                    (account_id, record["provider_message_id"]),
                ).fetchone()
                message_pk = int(message_row["id"])
                conn.execute(
                    """
                    INSERT INTO message_mailboxes(
                        message_id_fk, mailbox_id, uid, uidvalidity,
                        last_seen_at, missing_since
                    )
                    VALUES (?, ?, ?, ?, ?, NULL)
                    ON CONFLICT(message_id_fk, mailbox_id)
                    DO UPDATE SET
                        uid=excluded.uid,
                        uidvalidity=excluded.uidvalidity,
                        last_seen_at=excluded.last_seen_at,
                        missing_since=NULL
                    """,
                    (
                        message_pk,
                        mailbox_id,
                        int(record["uid"]),
                        uidvalidity,
                        now,
                    ),
                )
                labels = {
                    str(label).lower()
                    for label in json.loads(record.get("labels_json") or "[]")
                }
                conn.execute(
                    """
                    UPDATE messages
                       SET state=?,
                           last_seen_at=?,
                           missing_since=NULL,
                           trashed_at=?
                     WHERE id=?
                    """,
                    (
                        "trashed" if "\\trash" in labels else "active",
                        now,
                        now if "\\trash" in labels else None,
                        message_pk,
                    ),
                )
                conn.execute(
                    "DELETE FROM recipients WHERE message_id_fk=?", (message_pk,)
                )
                conn.executemany(
                    """
                    INSERT INTO recipients(
                        message_id_fk, kind, position, name, email, domain
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            message_pk,
                            recipient["kind"],
                            recipient["position"],
                            recipient.get("name"),
                            recipient.get("email"),
                            recipient.get("domain"),
                        )
                        for recipient in record.get("recipients", [])
                    ),
                )
                if record.get("attachment_indexed"):
                    attachments = list(record.get("attachments") or [])
                    conn.execute(
                        "DELETE FROM attachments WHERE message_id_fk=?",
                        (message_pk,),
                    )
                    conn.executemany(
                        """
                        INSERT INTO attachments(
                            message_id_fk, part_number, filename, extension,
                            content_type, disposition, transfer_encoding,
                            size_bytes, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            (
                                message_pk,
                                str(attachment.get("part_number") or ""),
                                str(attachment.get("filename") or ""),
                                str(attachment.get("extension") or "").lower(),
                                str(
                                    attachment.get("content_type")
                                    or "application/octet-stream"
                                ),
                                str(
                                    attachment.get("disposition")
                                    or "ATTACHMENT"
                                ),
                                str(
                                    attachment.get("transfer_encoding") or ""
                                ).lower(),
                                int(attachment.get("size_bytes") or 0),
                                now,
                            )
                            for attachment in attachments
                            if str(attachment.get("part_number") or "")
                        ),
                    )
                    conn.execute(
                        """
                        UPDATE messages
                           SET attachment_indexed=1,
                               attachment_count=?,
                               attachment_size_bytes=?,
                               attachment_indexed_at=?,
                               updated_at=?
                         WHERE id=?
                        """,
                        (
                            len(attachments),
                            sum(
                                int(item.get("size_bytes") or 0)
                                for item in attachments
                            ),
                            now,
                            now,
                            message_pk,
                        ),
                    )
            conn.execute(
                """
                UPDATE mailboxes
                   SET last_uid=?, uidvalidity=COALESCE(?, uidvalidity), updated_at=?
                 WHERE id=?
                """,
                (last_uid, uidvalidity, now, mailbox_id),
            )
        return inserted, updated

    def mailbox_known_uids(
        self,
        mailbox_id: int,
        uidvalidity: int | None,
    ) -> set[int]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT uid
                  FROM message_mailboxes
                 WHERE mailbox_id=?
                   AND (
                       uidvalidity IS NULL OR ? IS NULL OR uidvalidity=?
                   )
                """,
                (mailbox_id, uidvalidity, uidvalidity),
            ).fetchall()
        return {int(row["uid"]) for row in rows}

    def reconcile_mailbox_snapshot(
        self,
        mailbox_id: int,
        current_uids: Iterable[int],
        uidvalidity: int | None,
    ) -> dict[str, int]:
        """Apply a complete UID snapshot only after the mailbox finished."""
        current = {int(uid) for uid in current_uids}
        now = utc_now()
        missing = restored = 0
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT message_id_fk, uid, missing_since
                  FROM message_mailboxes
                 WHERE mailbox_id=?
                   AND (
                       uidvalidity IS NULL OR ? IS NULL OR uidvalidity=?
                   )
                """,
                (mailbox_id, uidvalidity, uidvalidity),
            ).fetchall()
            for row in rows:
                message_id = int(row["message_id_fk"])
                uid = int(row["uid"])
                if uid in current:
                    if row["missing_since"] is not None:
                        restored += 1
                    conn.execute(
                        """
                        UPDATE message_mailboxes
                           SET last_seen_at=?, missing_since=NULL,
                               uidvalidity=COALESCE(?, uidvalidity)
                         WHERE message_id_fk=? AND mailbox_id=?
                        """,
                        (now, uidvalidity, message_id, mailbox_id),
                    )
                else:
                    if row["missing_since"] is None:
                        missing += 1
                    conn.execute(
                        """
                        UPDATE message_mailboxes
                           SET missing_since=COALESCE(missing_since, ?)
                         WHERE message_id_fk=? AND mailbox_id=?
                        """,
                        (now, message_id, mailbox_id),
                    )
            conn.execute(
                """
                UPDATE mailboxes
                   SET last_uid=?, uidvalidity=COALESCE(?, uidvalidity),
                       messages_count=?, updated_at=?
                 WHERE id=?
                """,
                (
                    max(current, default=0),
                    uidvalidity,
                    len(current),
                    now,
                    mailbox_id,
                ),
            )
        return {
            "current": len(current),
            "missing": missing,
            "restored": restored,
        }

    def recompute_account_states(self, account_id: int) -> dict[str, int]:
        """Reconcile message state from live mappings in the monitored scope."""
        now = utc_now()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT m.id, m.state, m.trashed_at,
                       EXISTS(
                           SELECT 1
                             FROM message_mailboxes mm
                             JOIN mailboxes mb ON mb.id=mm.mailbox_id
                            WHERE mm.message_id_fk=m.id
                              AND mm.missing_since IS NULL
                              AND lower(COALESCE(mb.special_use, ''))='\\trash'
                       ) AS in_trash,
                       EXISTS(
                           SELECT 1
                             FROM message_mailboxes mm
                             JOIN mailboxes mb ON mb.id=mm.mailbox_id
                            WHERE mm.message_id_fk=m.id
                              AND mm.missing_since IS NULL
                              AND mb.selected=1
                              AND lower(COALESCE(mb.special_use, ''))
                                  NOT IN ('\\trash', '\\junk')
                       ) AS in_scope
                  FROM messages m
                 WHERE m.account_id=?
                """,
                (account_id,),
            ).fetchall()
            counts = {"active": 0, "trashed": 0, "missing": 0}
            for row in rows:
                if bool(row["in_trash"]):
                    state = "trashed"
                elif bool(row["in_scope"]):
                    state = "active"
                elif row["trashed_at"] is not None and row["state"] == "trashed":
                    state = "trashed"
                else:
                    state = "missing"
                counts[state] += 1
                conn.execute(
                    """
                    UPDATE messages
                       SET state=?,
                           last_seen_at=CASE
                               WHEN ? IN ('active', 'trashed') THEN ?
                               ELSE last_seen_at
                           END,
                           missing_since=CASE
                               WHEN ?='missing'
                               THEN COALESCE(missing_since, ?)
                               ELSE NULL
                           END,
                           trashed_at=CASE
                               WHEN ?='trashed'
                               THEN COALESCE(trashed_at, ?)
                               WHEN ?='active' THEN NULL
                               ELSE trashed_at
                           END,
                           updated_at=?
                     WHERE id=?
                    """,
                    (
                        state,
                        state,
                        now,
                        state,
                        now,
                        state,
                        now,
                        state,
                        now,
                        int(row["id"]),
                    ),
                )
        return counts

    def mark_account_synced(self, account_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE accounts SET last_sync_at=?, updated_at=? WHERE id=?",
                (utc_now(), utc_now(), account_id),
            )

    def account_summary(self, account_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS messages,
                       COUNT(DISTINCT NULLIF(from_email, '')) AS senders,
                       COUNT(DISTINCT NULLIF(from_domain, '')) AS domains,
                       MIN(date_sent_utc) AS first_date,
                       MAX(date_sent_utc) AS last_date,
                       COALESCE(SUM(size_bytes), 0) AS total_size
                  FROM messages
                 WHERE account_id=? AND state='active'
                """,
                (account_id,),
            ).fetchone()
            errors = conn.execute(
                """
                SELECT COUNT(*) AS amount
                  FROM sync_errors e
                  JOIN sync_jobs j ON j.id=e.job_id
                 WHERE j.account_id=?
                """,
                (account_id,),
            ).fetchone()["amount"]
        result = dict(row)
        result["errors"] = errors
        return result

    def attachment_analysis_summary(self, account_id: int) -> dict[str, int]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN attachment_indexed=1 THEN 1 ELSE 0 END)
                           AS indexed,
                       SUM(CASE WHEN attachment_count>0 THEN 1 ELSE 0 END)
                           AS with_attachments,
                       COALESCE(SUM(attachment_size_bytes), 0)
                           AS attachment_size
                  FROM messages
                 WHERE account_id=? AND state='active'
                """,
                (account_id,),
            ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "indexed": int(row["indexed"] or 0),
            "with_attachments": int(row["with_attachments"] or 0),
            "attachment_size": int(row["attachment_size"] or 0),
        }

    def attachment_analysis_targets(
        self,
        account_id: int,
    ) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT m.id AS message_pk, m.size_bytes,
                       mb.id AS mailbox_id, mb.remote_name AS mailbox_name,
                       mb.special_use, mb.uidvalidity AS mailbox_uidvalidity,
                       mm.uid, mm.uidvalidity AS mapping_uidvalidity
                  FROM messages m
                  JOIN message_mailboxes mm ON mm.message_id_fk=m.id
                  JOIN mailboxes mb ON mb.id=mm.mailbox_id
                 WHERE m.account_id=?
                   AND m.state='active'
                   AND m.attachment_indexed=0
                   AND mm.missing_since IS NULL
                   AND lower(COALESCE(mb.special_use, '')) <> '\\trash'
                   AND (
                       mm.uidvalidity IS NULL OR mb.uidvalidity IS NULL
                       OR mm.uidvalidity=mb.uidvalidity
                   )
                 ORDER BY m.id,
                          CASE lower(COALESCE(mb.special_use, ''))
                              WHEN '\\all' THEN 0
                              WHEN '\\inbox' THEN 1
                              WHEN '\\sent' THEN 2
                              ELSE 3
                          END,
                          mb.id
                """,
                (account_id,),
            ).fetchall()
        selected: dict[int, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            selected.setdefault(int(item["message_pk"]), item)
        return list(selected.values())

    def store_attachment_analysis(
        self,
        records: list[dict[str, Any]],
    ) -> None:
        now = utc_now()
        with self.connect() as conn:
            for record in records:
                message_id = int(record["message_pk"])
                attachments = list(record.get("attachments") or [])
                conn.execute(
                    "DELETE FROM attachments WHERE message_id_fk=?",
                    (message_id,),
                )
                conn.executemany(
                    """
                    INSERT INTO attachments(
                        message_id_fk, part_number, filename, extension,
                        content_type, disposition, transfer_encoding,
                        size_bytes, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            message_id,
                            str(item.get("part_number") or ""),
                            str(item.get("filename") or ""),
                            str(item.get("extension") or "").lower(),
                            str(
                                item.get("content_type")
                                or "application/octet-stream"
                            ),
                            str(item.get("disposition") or "ATTACHMENT"),
                            str(item.get("transfer_encoding") or "").lower(),
                            int(item.get("size_bytes") or 0),
                            now,
                        )
                        for item in attachments
                        if str(item.get("part_number") or "")
                    ),
                )
                conn.execute(
                    """
                    UPDATE messages
                       SET attachment_indexed=1,
                           attachment_count=?,
                           attachment_size_bytes=?,
                           attachment_indexed_at=?,
                           updated_at=?
                     WHERE id=?
                    """,
                    (
                        len(attachments),
                        sum(
                            int(item.get("size_bytes") or 0)
                            for item in attachments
                        ),
                        now,
                        now,
                        message_id,
                    ),
                )

    def largest_messages(
        self,
        account_id: int,
        *,
        search: str = "",
        extensions: Iterable[str] | None = None,
        minimum_size: int = 0,
        attachments_only: bool = False,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        conditions = ["m.account_id=?", "m.state='active'"]
        values: list[Any] = [account_id]
        if minimum_size > 0:
            conditions.append("COALESCE(m.size_bytes, 0)>=?")
            values.append(int(minimum_size))
        normalized_extensions = sorted(
            {
                str(extension).strip().lower().lstrip(".")
                for extension in (extensions or ())
                if str(extension).strip().lstrip(".")
            }
        )
        if normalized_extensions:
            placeholders = ",".join("?" for _ in normalized_extensions)
            conditions.append(
                "EXISTS (SELECT 1 FROM attachments ax "
                "WHERE ax.message_id_fk=m.id "
                f"AND ax.extension IN ({placeholders}))"
            )
            values.extend(normalized_extensions)
        elif attachments_only:
            conditions.append("m.attachment_count>0")
        normalized_search = search.strip().lower()
        if normalized_search:
            escaped = (
                normalized_search.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            pattern = f"%{escaped}%"
            conditions.append(
                "("
                "lower(COALESCE(m.subject, '')) LIKE ? ESCAPE '\\' OR "
                "lower(COALESCE(m.from_name, '')) LIKE ? ESCAPE '\\' OR "
                "lower(COALESCE(m.from_email, '')) LIKE ? ESCAPE '\\' OR "
                "EXISTS (SELECT 1 FROM attachments aq "
                "WHERE aq.message_id_fk=m.id "
                "AND lower(aq.filename) LIKE ? ESCAPE '\\')"
                ")"
            )
            values.extend((pattern, pattern, pattern, pattern))
        values.append(max(1, int(limit)))
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT m.id, m.subject, m.from_name, m.from_email,
                       m.from_domain, m.source_mailbox, m.size_bytes,
                       m.attachment_indexed, m.attachment_count,
                       m.attachment_size_bytes,
                       COALESCE(
                           m.date_sent_utc,
                           m.internal_date_utc,
                           m.first_seen_at
                       ) AS message_date,
                       (
                           SELECT GROUP_CONCAT(filename, ' · ')
                             FROM attachments af
                            WHERE af.message_id_fk=m.id
                            ORDER BY af.size_bytes DESC, af.id
                       ) AS attachment_names,
                       (
                           SELECT GROUP_CONCAT(DISTINCT extension)
                             FROM attachments ae
                            WHERE ae.message_id_fk=m.id
                              AND extension<>''
                       ) AS attachment_extensions
                  FROM messages m
                 WHERE {" AND ".join(conditions)}
                 ORDER BY
                       CASE
                           WHEN m.attachment_count>0
                           THEN m.attachment_size_bytes
                           ELSE COALESCE(m.size_bytes, 0)
                       END DESC,
                       COALESCE(m.size_bytes, 0) DESC,
                       m.id DESC
                 LIMIT ?
                """,
                values,
            ).fetchall()
        return [dict(row) for row in rows]

    def message_attachments(
        self,
        account_id: int,
        message_id: int,
    ) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT ax.*
                  FROM attachments ax
                  JOIN messages m ON m.id=ax.message_id_fk
                 WHERE m.account_id=? AND m.id=?
                 ORDER BY ax.size_bytes DESC, ax.id
                """,
                (account_id, message_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def _export_selection_filter(
        self,
        sender_emails: list[str] | None,
        domains: list[str] | None,
        message_ids: list[int] | None = None,
    ) -> tuple[str, list[Any]]:
        if message_ids is not None:
            normalized = sorted(
                {int(value) for value in message_ids if int(value) > 0}
            )
            if not normalized:
                return " AND 0", []
            placeholders = ",".join("?" for _ in normalized)
            return f" AND m.id IN ({placeholders})", normalized
        if sender_emails is None and domains is None:
            return "", []
        selection, values = self._selection_sql(
            sender_emails or [],
            domains or [],
        )
        if not selection:
            return " AND 0", []
        return (
            f" AND {self._eligible_cleanup_sql()} AND ({selection})",
            values,
        )

    def iter_messages(
        self,
        account_ids: list[int],
        sender_emails: list[str] | None = None,
        domains: list[str] | None = None,
        message_ids: list[int] | None = None,
    ) -> Iterator[dict[str, Any]]:
        placeholders = ",".join("?" for _ in account_ids)
        selection_filter, selection_values = self._export_selection_filter(
            sender_emails,
            domains,
            message_ids,
        )
        sql = f"""
            SELECT a.display_name AS account_name, a.email AS account_email, m.*,
                   (
                       SELECT GROUP_CONCAT(filename, '; ')
                         FROM attachments ax
                        WHERE ax.message_id_fk=m.id
                   ) AS attachment_names,
                   (
                       SELECT GROUP_CONCAT(DISTINCT extension)
                         FROM attachments ae
                        WHERE ae.message_id_fk=m.id AND ae.extension<>''
                   ) AS attachment_extensions
              FROM messages m
              JOIN accounts a ON a.id=m.account_id
             WHERE m.account_id IN ({placeholders})
               AND m.state='active'
               {selection_filter}
             ORDER BY COALESCE(m.date_sent_utc, m.internal_date_utc), m.id
        """
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            for row in conn.execute(
                sql,
                (*account_ids, *selection_values),
            ):
                yield dict(row)
        finally:
            conn.close()

    def iter_recipients(
        self,
        account_ids: list[int],
        sender_emails: list[str] | None = None,
        domains: list[str] | None = None,
        message_ids: list[int] | None = None,
    ) -> Iterator[dict[str, Any]]:
        placeholders = ",".join("?" for _ in account_ids)
        selection_filter, selection_values = self._export_selection_filter(
            sender_emails,
            domains,
            message_ids,
        )
        sql = f"""
            SELECT a.display_name AS account_name, a.email AS account_email,
                   m.provider_message_id, m.message_id, m.subject,
                   r.kind, r.position, r.name, r.email, r.domain
              FROM recipients r
              JOIN messages m ON m.id=r.message_id_fk
              JOIN accounts a ON a.id=m.account_id
             WHERE m.account_id IN ({placeholders})
               AND m.state='active'
               {selection_filter}
             ORDER BY a.id, m.id, r.kind, r.position
        """
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            for row in conn.execute(
                sql,
                (*account_ids, *selection_values),
            ):
                yield dict(row)
        finally:
            conn.close()

    def domain_summary(
        self,
        account_ids: list[int],
        sender_emails: list[str] | None = None,
        domains: list[str] | None = None,
        message_ids: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in account_ids)
        selection_filter, selection_values = self._export_selection_filter(
            sender_emails,
            domains,
            message_ids,
        )
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT a.display_name AS account_name,
                       m.from_domain AS domain,
                       COUNT(*) AS messages,
                       COUNT(DISTINCT m.from_email) AS senders,
                       COALESCE(SUM(m.size_bytes), 0) AS total_size,
                       MIN(m.date_sent_utc) AS first_date,
                       MAX(m.date_sent_utc) AS last_date
                  FROM messages m
                  JOIN accounts a ON a.id=m.account_id
                 WHERE m.account_id IN ({placeholders})
                   AND m.state='active'
                   {selection_filter}
                   AND COALESCE(m.from_domain, '') <> ''
                 GROUP BY a.id, m.from_domain
                 ORDER BY messages DESC, domain
                """,
                (*account_ids, *selection_values),
            ).fetchall()
        return [dict(row) for row in rows]

    def iter_attachments(
        self,
        account_ids: list[int],
        sender_emails: list[str] | None = None,
        domains: list[str] | None = None,
        message_ids: list[int] | None = None,
    ) -> Iterator[dict[str, Any]]:
        placeholders = ",".join("?" for _ in account_ids)
        selection_filter, selection_values = self._export_selection_filter(
            sender_emails,
            domains,
            message_ids,
        )
        sql = f"""
            SELECT a.display_name AS account_name,
                   a.email AS account_email,
                   m.provider_message_id, m.subject,
                   m.from_name, m.from_email,
                   ax.part_number, ax.filename, ax.extension,
                   ax.content_type, ax.disposition,
                   ax.transfer_encoding, ax.size_bytes
              FROM attachments ax
              JOIN messages m ON m.id=ax.message_id_fk
              JOIN accounts a ON a.id=m.account_id
             WHERE m.account_id IN ({placeholders})
               AND m.state='active'
               {selection_filter}
             ORDER BY a.id, m.id, ax.size_bytes DESC, ax.id
        """
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            for row in conn.execute(
                sql,
                (*account_ids, *selection_values),
            ):
                yield dict(row)
        finally:
            conn.close()

    @staticmethod
    def _eligible_cleanup_sql() -> str:
        return """
            m.state='active'
            AND COALESCE(TRIM(m.from_email), '') <> ''
            AND lower(TRIM(m.from_email)) <> lower(TRIM(a.email))
            AND m.labels_json NOT LIKE '%\\\\Sent%'
            AND m.labels_json NOT LIKE '%\\\\Draft%'
            AND m.labels_json NOT LIKE '%\\\\Trash%'
        """

    def sender_summary(
        self, account_id: int, limit: int = 500
    ) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT lower(TRIM(m.from_email)) AS email,
                       MAX(NULLIF(TRIM(m.from_name), '')) AS name,
                       lower(TRIM(m.from_domain)) AS domain,
                       COUNT(*) AS messages,
                       COALESCE(SUM(m.size_bytes), 0) AS total_size,
                       MIN(COALESCE(m.date_sent_utc, m.internal_date_utc)) AS first_date,
                       MAX(COALESCE(m.date_sent_utc, m.internal_date_utc)) AS last_date
                  FROM messages m
                  JOIN accounts a ON a.id=m.account_id
                 WHERE m.account_id=? AND {self._eligible_cleanup_sql()}
                 GROUP BY lower(TRIM(m.from_email)), lower(TRIM(m.from_domain))
                 ORDER BY messages DESC, email
                 LIMIT ?
                """,
                (account_id, max(1, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    def subject_messages(
        self,
        account_id: int,
        *,
        sender_email: str | None = None,
        domain: str | None = None,
        search: str = "",
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if (sender_email is None) == (domain is None):
            raise ValueError(tr("Informe exatamente um remetente ou domínio."))

        if sender_email is not None:
            item_filter = "lower(TRIM(m.from_email))=lower(TRIM(?))"
            item_value = sender_email
        else:
            item_filter = "lower(TRIM(m.from_domain))=lower(TRIM(?))"
            item_value = str(domain)

        search_filter = ""
        values: list[Any] = [account_id, item_value]
        normalized_search = search.strip().lower()
        if normalized_search:
            escaped_search = (
                normalized_search.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            pattern = f"%{escaped_search}%"
            search_filter = """
                AND (
                    lower(COALESCE(m.subject, '')) LIKE ? ESCAPE '\\'
                    OR lower(COALESCE(m.from_name, '')) LIKE ? ESCAPE '\\'
                    OR lower(COALESCE(m.from_email, '')) LIKE ? ESCAPE '\\'
                )
            """
            values.extend((pattern, pattern, pattern))
        date_expression = (
            "date(COALESCE(m.date_sent_utc, m.internal_date_utc, "
            "m.first_seen_at))"
        )
        if date_from:
            search_filter += (
                f"\n                AND {date_expression}>=date(?)"
            )
            values.append(str(date_from))
        if date_to:
            search_filter += (
                f"\n                AND {date_expression}<=date(?)"
            )
            values.append(str(date_to))
        values.extend((max(1, int(limit)), max(0, int(offset))))

        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT m.id, m.subject, m.from_name, m.from_email,
                       m.from_domain,
                       COALESCE(
                           m.date_sent_utc,
                           m.internal_date_utc,
                           m.first_seen_at
                       ) AS message_date,
                       m.source_mailbox, m.size_bytes
                  FROM messages m
                  JOIN accounts a ON a.id=m.account_id
                 WHERE m.account_id=?
                   AND {self._eligible_cleanup_sql()}
                   AND {item_filter}
                   {search_filter}
                 ORDER BY COALESCE(
                              m.date_sent_utc,
                              m.internal_date_utc,
                              m.first_seen_at
                          ) DESC,
                          m.id DESC
                 LIMIT ? OFFSET ?
                """,
                values,
            ).fetchall()
        return [dict(row) for row in rows]

    def count_subject_messages(
        self,
        account_id: int,
        *,
        sender_email: str | None = None,
        domain: str | None = None,
        search: str = "",
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> int:
        if (sender_email is None) == (domain is None):
            raise ValueError(tr("Informe exatamente um remetente ou domínio."))

        if sender_email is not None:
            item_filter = "lower(TRIM(m.from_email))=lower(TRIM(?))"
            item_value = sender_email
        else:
            item_filter = "lower(TRIM(m.from_domain))=lower(TRIM(?))"
            item_value = str(domain)

        conditions = [
            "m.account_id=?",
            self._eligible_cleanup_sql(),
            item_filter,
        ]
        values: list[Any] = [account_id, item_value]
        normalized_search = search.strip().lower()
        if normalized_search:
            escaped_search = (
                normalized_search.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            pattern = f"%{escaped_search}%"
            conditions.append(
                "(lower(COALESCE(m.subject, '')) LIKE ? ESCAPE '\\' "
                "OR lower(COALESCE(m.from_name, '')) LIKE ? ESCAPE '\\' "
                "OR lower(COALESCE(m.from_email, '')) LIKE ? ESCAPE '\\')"
            )
            values.extend((pattern, pattern, pattern))
        date_expression = (
            "date(COALESCE(m.date_sent_utc, m.internal_date_utc, "
            "m.first_seen_at))"
        )
        if date_from:
            conditions.append(f"{date_expression}>=date(?)")
            values.append(str(date_from))
        if date_to:
            conditions.append(f"{date_expression}<=date(?)")
            values.append(str(date_to))

        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM messages m JOIN accounts a "
                "ON a.id=m.account_id WHERE " + " AND ".join(conditions),
                values,
            ).fetchone()
        return int(row[0] if row else 0)

    def message_reader_target(
        self,
        account_id: int,
        message_id: int,
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT m.id, m.subject, m.from_name, m.from_email,
                       m.to_addresses, m.cc_addresses, m.date_header_raw,
                       m.size_bytes, m.attachment_indexed,
                       m.attachment_count, m.attachment_size_bytes,
                       mb.remote_name AS mailbox_name,
                       mm.uid, mb.uidvalidity AS mailbox_uidvalidity,
                       mm.uidvalidity AS mapping_uidvalidity
                  FROM messages m
                  JOIN message_mailboxes mm ON mm.message_id_fk=m.id
                  JOIN mailboxes mb ON mb.id=mm.mailbox_id
                 WHERE m.account_id=?
                   AND m.id=?
                   AND m.state='active'
                   AND mm.missing_since IS NULL
                   AND (
                       mm.uidvalidity IS NULL OR mb.uidvalidity IS NULL
                       OR mm.uidvalidity=mb.uidvalidity
                   )
                 ORDER BY
                       CASE lower(COALESCE(mb.special_use, ''))
                           WHEN '\\all' THEN 0
                           WHEN '\\inbox' THEN 1
                           ELSE 2
                       END,
                       mb.id
                 LIMIT 1
                """,
                (account_id, message_id),
            ).fetchone()
        return dict(row) if row else None

    def export_sender_summary(
        self,
        account_ids: list[int],
        sender_emails: list[str] | None = None,
        domains: list[str] | None = None,
        message_ids: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in account_ids)
        selection_filter, selection_values = self._export_selection_filter(
            sender_emails,
            domains,
            message_ids,
        )
        eligibility = (
            "m.state='active'"
            if message_ids is not None
            else self._eligible_cleanup_sql()
        )
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT a.display_name AS account_name,
                       lower(TRIM(m.from_email)) AS email,
                       MAX(NULLIF(TRIM(m.from_name), '')) AS name,
                       lower(TRIM(m.from_domain)) AS domain,
                       COUNT(*) AS messages,
                       COALESCE(SUM(m.size_bytes), 0) AS total_size,
                       MIN(COALESCE(m.date_sent_utc, m.internal_date_utc)) AS first_date,
                       MAX(COALESCE(m.date_sent_utc, m.internal_date_utc)) AS last_date
                  FROM messages m
                  JOIN accounts a ON a.id=m.account_id
                 WHERE m.account_id IN ({placeholders})
                   AND {eligibility}
                   {selection_filter}
                 GROUP BY a.id, lower(TRIM(m.from_email)),
                          lower(TRIM(m.from_domain))
                 ORDER BY messages DESC, email
                """,
                (*account_ids, *selection_values),
            ).fetchall()
        return [dict(row) for row in rows]

    def cleanup_domain_summary(
        self, account_id: int, limit: int = 500
    ) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT lower(TRIM(m.from_domain)) AS domain,
                       COUNT(*) AS messages,
                       COUNT(DISTINCT lower(TRIM(m.from_email))) AS senders,
                       COALESCE(SUM(m.size_bytes), 0) AS total_size,
                       MIN(COALESCE(m.date_sent_utc, m.internal_date_utc)) AS first_date,
                       MAX(COALESCE(m.date_sent_utc, m.internal_date_utc)) AS last_date
                  FROM messages m
                  JOIN accounts a ON a.id=m.account_id
                 WHERE m.account_id=? AND {self._eligible_cleanup_sql()}
                   AND COALESCE(TRIM(m.from_domain), '') <> ''
                 GROUP BY lower(TRIM(m.from_domain))
                 ORDER BY messages DESC, domain
                 LIMIT ?
                """,
                (account_id, max(1, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _selection_sql(
        sender_emails: list[str], domains: list[str]
    ) -> tuple[str, list[str]]:
        clauses: list[str] = []
        values: list[str] = []
        if sender_emails:
            placeholders = ",".join("?" for _ in sender_emails)
            clauses.append(f"lower(TRIM(m.from_email)) IN ({placeholders})")
            values.extend(email.strip().lower() for email in sender_emails)
        if domains:
            placeholders = ",".join("?" for _ in domains)
            clauses.append(f"lower(TRIM(m.from_domain)) IN ({placeholders})")
            values.extend(domain.strip().lower() for domain in domains)
        return (" OR ".join(clauses), values)

    def cleanup_preview(
        self,
        account_id: int,
        sender_emails: list[str],
        domains: list[str],
    ) -> dict[str, int]:
        selection, values = self._selection_sql(sender_emails, domains)
        if not selection:
            return {"messages": 0, "total_size": 0}
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS messages,
                       COALESCE(SUM(m.size_bytes), 0) AS total_size
                  FROM messages m
                  JOIN accounts a ON a.id=m.account_id
                 WHERE m.account_id=? AND {self._eligible_cleanup_sql()}
                   AND ({selection})
                """,
                (account_id, *values),
            ).fetchone()
        return {
            "messages": int(row["messages"] or 0),
            "total_size": int(row["total_size"] or 0),
        }

    def cleanup_targets(
        self,
        account_id: int,
        sender_emails: list[str],
        domains: list[str],
    ) -> list[dict[str, Any]]:
        selection, values = self._selection_sql(sender_emails, domains)
        if not selection:
            return []
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT m.id AS message_pk, m.provider_message_id, m.message_id,
                       m.labels_json,
                       m.from_email, m.from_domain, m.size_bytes,
                       mb.id AS mailbox_id, mb.remote_name AS mailbox_name,
                       mb.special_use, mb.uidvalidity AS mailbox_uidvalidity,
                       mm.uid, mm.uidvalidity AS mapping_uidvalidity
                  FROM messages m
                  JOIN accounts a ON a.id=m.account_id
                  JOIN message_mailboxes mm ON mm.message_id_fk=m.id
                  JOIN mailboxes mb ON mb.id=mm.mailbox_id
                 WHERE m.account_id=? AND {self._eligible_cleanup_sql()}
                   AND ({selection})
                   AND lower(COALESCE(mb.special_use, '')) NOT IN (
                       '\\trash', '\\sent', '\\drafts'
                   )
                   AND (
                       mm.uidvalidity IS NULL OR mb.uidvalidity IS NULL
                       OR mm.uidvalidity=mb.uidvalidity
                   )
                 ORDER BY m.id,
                          CASE lower(COALESCE(mb.special_use, ''))
                              WHEN '\\all' THEN 0
                              WHEN '\\inbox' THEN 1
                              ELSE 2
                          END,
                          mb.id
                """,
                (account_id, *values),
            ).fetchall()
        selected: dict[int, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            selected.setdefault(int(item["message_pk"]), item)
        return list(selected.values())

    def message_cleanup_preview(
        self,
        account_id: int,
        message_ids: list[int],
    ) -> dict[str, int]:
        normalized = sorted({int(value) for value in message_ids if int(value) > 0})
        if not normalized:
            return {"messages": 0, "total_size": 0}
        placeholders = ",".join("?" for _ in normalized)
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS messages,
                       COALESCE(SUM(size_bytes), 0) AS total_size
                  FROM messages
                 WHERE account_id=?
                   AND state='active'
                   AND labels_json NOT LIKE '%\\\\Draft%'
                   AND labels_json NOT LIKE '%\\\\Trash%'
                   AND id IN ({placeholders})
                """,
                (account_id, *normalized),
            ).fetchone()
        return {
            "messages": int(row["messages"] or 0),
            "total_size": int(row["total_size"] or 0),
        }

    def message_cleanup_targets(
        self,
        account_id: int,
        message_ids: list[int],
    ) -> list[dict[str, Any]]:
        normalized = sorted({int(value) for value in message_ids if int(value) > 0})
        if not normalized:
            return []
        placeholders = ",".join("?" for _ in normalized)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT m.id AS message_pk, m.provider_message_id, m.message_id,
                       m.labels_json, m.from_email, m.from_domain, m.size_bytes,
                       mb.id AS mailbox_id, mb.remote_name AS mailbox_name,
                       mb.special_use, mb.uidvalidity AS mailbox_uidvalidity,
                       mm.uid, mm.uidvalidity AS mapping_uidvalidity
                  FROM messages m
                  JOIN message_mailboxes mm ON mm.message_id_fk=m.id
                  JOIN mailboxes mb ON mb.id=mm.mailbox_id
                 WHERE m.account_id=?
                   AND m.state='active'
                   AND m.labels_json NOT LIKE '%\\\\Draft%'
                   AND m.labels_json NOT LIKE '%\\\\Trash%'
                   AND m.id IN ({placeholders})
                   AND mm.missing_since IS NULL
                   AND lower(COALESCE(mb.special_use, '')) NOT IN (
                       '\\trash', '\\drafts'
                   )
                   AND (
                       mm.uidvalidity IS NULL OR mb.uidvalidity IS NULL
                       OR mm.uidvalidity=mb.uidvalidity
                   )
                 ORDER BY m.id,
                          CASE lower(COALESCE(mb.special_use, ''))
                              WHEN '\\all' THEN 0
                              WHEN '\\inbox' THEN 1
                              WHEN '\\sent' THEN 2
                              ELSE 3
                          END,
                          mb.id
                """,
                (account_id, *normalized),
            ).fetchall()
        selected: dict[int, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            selected.setdefault(int(item["message_pk"]), item)
        return list(selected.values())

    def get_trash_mailbox(self, account_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM mailboxes
                 WHERE account_id=? AND lower(COALESCE(special_use, ''))='\\trash'
                 ORDER BY id LIMIT 1
                """,
                (account_id,),
            ).fetchone()
        return dict(row) if row else None

    def mark_messages_trashed(self, message_ids: list[int]) -> None:
        if not message_ids:
            return
        placeholders = ",".join("?" for _ in message_ids)
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                f"UPDATE messages SET state='trashed', trashed_at=?, "
                f"missing_since=NULL, updated_at=? "
                f"WHERE id IN ({placeholders})",
                (now, now, *message_ids),
            )
            conn.execute(
                f"""
                UPDATE message_mailboxes
                   SET missing_since=COALESCE(missing_since, ?)
                 WHERE message_id_fk IN ({placeholders})
                   AND mailbox_id IN (
                       SELECT id FROM mailboxes
                        WHERE lower(COALESCE(special_use, '')) <> '\\trash'
                   )
                """,
                (now, *message_ids),
            )

    def mark_messages_restored(self, message_ids: list[int]) -> None:
        if not message_ids:
            return
        placeholders = ",".join("?" for _ in message_ids)
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                f"""
                UPDATE messages
                   SET state='missing', trashed_at=NULL, missing_since=?,
                       updated_at=?
                 WHERE id IN ({placeholders})
                """,
                (now, now, *message_ids),
            )
            conn.execute(
                f"""
                DELETE FROM message_mailboxes
                 WHERE message_id_fk IN ({placeholders})
                """,
                tuple(message_ids),
            )

    def export_errors(self, account_ids: list[int]) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in account_ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT a.display_name AS account_name, e.mailbox_name, e.uid,
                       e.error_type, e.detail, e.created_at
                  FROM sync_errors e
                  JOIN sync_jobs j ON j.id=e.job_id
                  JOIN accounts a ON a.id=j.account_id
                 WHERE j.account_id IN ({placeholders})
                 ORDER BY e.id
                """,
                tuple(account_ids),
            ).fetchall()
        return [dict(row) for row in rows]
