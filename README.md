<p align="right">
  <strong>English</strong> · <a href="README.pt-BR.md">Português (Brasil)</a>
</p>

<p align="center">
  <img src="assets/io.github.ehstbr.imapexporter.svg" width="112" alt="IMAP Exporter icon">
</p>

<h1 align="center">IMAP Exporter</h1>

<p align="center">
  Synchronize, analyze, organize, and export email metadata through IMAP.
</p>

<p align="center">
  <img alt="Version 0.4.12" src="https://img.shields.io/badge/version-0.4.12-3584e4">
  <img alt="MIT License" src="https://img.shields.io/badge/license-MIT-26a269">
  <img alt="Python 3.10 or newer" src="https://img.shields.io/badge/Python-3.10%2B-3776ab">
  <img alt="GTK 4" src="https://img.shields.io/badge/interface-GTK%204-9141ac">
  <img alt="Linux" src="https://img.shields.io/badge/platform-Linux-f6d32d">
</p>

<p align="center">
  <img src="docs/screenshots/10-results-summary.png" width="920" alt="IMAP Exporter synchronization summary">
</p>

IMAP Exporter is a multi-account Linux desktop application for inspecting large
mailboxes without downloading every message body. Normal synchronization stores
headers, identifiers, folder state, message size, and MIME structure. Textual
content and attachment bytes are requested only when you explicitly open or
download them.

> [!IMPORTANT]
> IMAP Exporter is an analysis and cleanup tool, not an email client or a backup
> application. Verify a small cleanup selection in webmail before moving large
> batches.

## Highlights

- Multiple IMAP accounts, each with an independent local history and encrypted
  credential.
- Two-step account setup with provider presets and manual server configuration.
- Incremental synchronization: known UIDs stay local and only unknown headers
  are fetched in batches.
- Pause, resume, and safe cancellation with restart points.
- Rankings by sender, domain, and message size, with search, date, attachment,
  minimum-size, and multi-extension filters.
- Lightweight message reader using `BODY.PEEK`, with HTML converted to safe text
  and no external images or scripts executed.
- Attachment inspection and on-demand download, including real per-row progress,
  individual or batch cancellation, and cleanup of incomplete temporary files.
- CSV and ODS export for the complete index or the current selection.
- Batch move to Trash with confirmation, progress, technical log, and undo when
  the IMAP server provides identifiers that make restoration safe.
- Portuguese and English interface, symbolic icons, and GTK 4 styling tested for
  GNOME, LXQt/Lubuntu, and other Linux desktop environments.

## Screenshots

<details>
<summary><strong>Open the interface gallery</strong></summary>

### Account setup and folder selection

<p align="center">
  <img src="docs/screenshots/02-add-account-server.png" width="49%" alt="Add IMAP account server settings">
  <img src="docs/screenshots/08-folder-selection.png" width="49%" alt="Choose IMAP folders">
</p>

### Synchronization and analysis

<p align="center">
  <img src="docs/screenshots/09-synchronization-progress.png" width="49%" alt="Header synchronization progress">
  <img src="docs/screenshots/11-senders-ranking.png" width="49%" alt="Sender ranking and selection">
</p>

### Safe reading and attachments

<p align="center">
  <img src="docs/screenshots/14-message-reader.png" width="49%" alt="Safe on-demand message reader">
  <img src="docs/screenshots/18-attachment-downloads.png" width="49%" alt="On-demand attachment downloads">
</p>

### Cleanup and undo

<p align="center">
  <img src="docs/screenshots/15-cleanup-confirmation.png" width="49%" alt="Cleanup confirmation">
  <img src="docs/screenshots/17-cleanup-completed.png" width="49%" alt="Completed cleanup with undo">
</p>

</details>

All screenshots included in this repository were reviewed and anonymized before
publication. The full set is available in [`docs/screenshots`](docs/screenshots).

## Privacy and credential protection

1. The account's local password must contain at least eight characters and is
   never stored.
2. The IMAP password is encrypted with AES-256-CBC and PBKDF2 through OpenSSL.
3. A separate HMAC verifies integrity before decryption.
4. Decrypted credentials remain only in process memory while the account is
   unlocked.
5. Locking the account clears its in-memory session.
6. Changing the local password immediately re-encrypts the IMAP credential.

There is no local-password recovery. If it is forgotten, the locked account can
be removed through the system's native administrative authorization. This flow
deletes only the local registration, encrypted credential, and metadata; it does
not decrypt the credential, connect to IMAP, or alter server messages.

Normal synchronization does not fetch message bodies or attachment bytes. The
reader requests only the selected message and keeps its content in the window's
memory. Attachment bytes are written only to a location chosen by the user.

## Preconfigured providers

| Provider | IMAP server | Port | Notes |
| --- | --- | ---: | --- |
| Gmail | `imap.gmail.com` | 993 | Usually requires an app password |
| UOL Mail | `imap.uol.com.br` | 993 | Mailbox password |
| BOL Mail | `imap.bol.com.br` | 993 | Mailbox password |
| Terra Mail | `imap.terra.com.br` | 993 | Mailbox password |
| Yahoo Mail | `imap.mail.yahoo.com` | 993 | Usually requires an app password |
| iCloud Mail | `imap.mail.me.com` | 993 | Requires an app-specific password |
| AOL Mail | `imap.aol.com` | 993 | May require an app password |
| GMX Mail | `imap.gmx.com` | 993 | IMAP access must be enabled |
| Mail.ru | `imap.mail.ru` | 993 | Requires a password for external apps |

`Other IMAP server` accepts any service compatible with password authentication
over IMAP SSL/TLS. Outlook.com, Hotmail, and other OAuth2-only services are not
included because version 0.4.12 does not implement OAuth2. Proton Mail requires
its Bridge application rather than a regular direct IMAP connection.

Provider requirements may change. Confirm IMAP access and password requirements
in the provider's current documentation.

## Installation

### Debian package — recommended

On Debian, Ubuntu, Lubuntu, Linux Mint, and derivatives:

```bash
sudo apt install ./imap-exporter_0.4.12_all.deb
```

Using `apt install` allows the system to install the declared dependencies:
Python 3, PyGObject, GTK 4, OpenSSL, CA certificates, Polkit/pkexec, and the base
icon theme. The package installs the `imap-exporter` command and a desktop-menu
entry.

### Source archive

The application has no `pip` dependencies. On a standard Ubuntu desktop, extract
the source archive and run:

```bash
./executar.sh
```

If executable permissions were not preserved:

```bash
chmod +x executar.sh
./executar.sh
```

### Clone the repository

Install the runtime dependencies on Debian-based distributions:

```bash
sudo apt install python3 python3-gi gir1.2-gtk-4.0 openssl \
  ca-certificates hicolor-icon-theme pkexec
```

Then clone and run:

```bash
git clone https://github.com/ehstbr/IMAP-Exporter.git
cd IMAP-Exporter
./executar.sh
```

Administrative removal of a locked account depends on the privileged helper and
Polkit policy installed by the Debian package. All other features can be tested
directly from the source tree.

## Local data

New installations store the SQLite database at:

```text
~/.local/share/imap-exporter/dados.sqlite3
```

If the legacy `~/.local/share/gmail-header-exporter` directory already exists
and the new directory does not, it continues to be used automatically so the
application rename does not lose accounts, history, or preferences.

## How synchronization and cleanup work

The Inbox may not represent the complete account. In Gmail, `All Mail` contains
received, sent, and archived messages, while Spam and Trash remain separate. On
other servers, prefer the folder marked with the special `\All` attribute or
select the necessary folders manually.

Each synchronization obtains the current UID list, compares it with the local
index, and downloads full headers only for unknown UIDs. Missing links are
marked inactive after a folder completes successfully; restored messages become
active again when they return to the monitored scope. The maintenance command
`Rebuild local index` removes metadata only and never modifies the server.

For safety, cleanup:

- ignores messages sent by the account itself, drafts, and items already in
  Trash;
- protects shared domains such as `gmail.com` from whole-domain selection;
- requires the account to remain unlocked;
- never empties Trash;
- refuses the operation if the server does not offer `MOVE`, `UIDPLUS`, or
  another compatible safe mechanism, avoiding a global `EXPUNGE`.

## Metadata collected

- Sender name, address, and domain.
- `Sender`, `Reply-To`, `Return-Path`, `Delivered-To`, and `X-Original-To`.
- `To`, `Cc`, and `Bcc` recipients when available.
- Subject, header date, and server internal date.
- `Message-ID`, provider-specific IDs, and conversation ID.
- Source folder, UID, labels, flags, and original size.
- `In-Reply-To`, `References`, and `List-ID`.
- Attachment MIME structure: IMAP section, filename, extension, type, encoding,
  and encoded size.

For new messages, `BODYSTRUCTURE` is collected with the header query. Older
indexes can complete attachment analysis with `Complete analysis`, which
requests only `UID + BODYSTRUCTURE` and does not repeat header downloads.

## Tests

```bash
/usr/bin/python3 -m unittest discover -s tests -v
```

GitHub Actions runs the same suite on every push and pull request.

## Documentation and contributing

- [Changelog](CHANGELOG.md)
- [Contributing guide](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Terms of use](TERMS.md)
- [Third-party notices — English](THIRD_PARTY_NOTICES.en.md)
- [Third-party notices — Portuguese](THIRD_PARTY_NOTICES.pt_BR.md)

Please report regular bugs and feature requests through
[GitHub Issues](https://github.com/ehstbr/IMAP-Exporter/issues). Do not disclose
security vulnerabilities in a public issue; follow [SECURITY.md](SECURITY.md).

## Author and license

Developed by **Eduardo Henrique Silva Teixeira**  
Website: <https://eduhcommerce.com.br>  
Contact: <contato@eduhcommerce.com.br>

Released under the [MIT License](LICENSE).
