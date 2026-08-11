# Update manifest

IMAP Exporter checks the following HTTPS document once per application start:

```text
https://raw.githubusercontent.com/ehstbr/IMAP-Exporter/main/version.json
```

The automatic request is silent and runs in the background without a startup
panel, spinner, or temporary interface lock. The same update service is used by
the **Check for updates** action in the About window, where progress remains
visible because the user explicitly requested the check. It does not use the
GitHub API, scrape release pages, download packages, or run installation
commands.

## Schema 1

```json
{
  "schema_version": 1,
  "version": "0.5.5",
  "mandatory": false,
  "released_at": "2026-08-11T11:45:38Z",
  "summary": "Short plain-text release summary.",
  "changelog": [
    "First plain-text change.",
    "Second plain-text change."
  ]
}
```

- `schema_version` must be the integer `1`.
- `version` must be a valid semantic version without a `v` prefix.
- `mandatory` must be a JSON boolean, never a string or number.
- `released_at` must be an ISO 8601 timestamp with timezone information.
- `summary` is the short text displayed immediately.
- `changelog` is the complete ordered list displayed on demand.

Remote text is rendered as plain text. Invalid JSON, missing fields, unknown
schemas, invalid types, HTTP errors, redirects outside HTTPS, timeouts, and
oversized responses are treated as check failures. Startup remains fail-open:
the application is immediately usable and continues normally when the current
policy cannot be verified.

## Optional and mandatory releases

When the remote version is newer and `mandatory` is `false`, normal use is
enabled and a non-modal update notice is shown. Closing it means continuing
with the installed version for the current session.

When the remote version is newer and `mandatory` is `true`, new operations are
blocked. A critical operation that is already running is allowed to reach a
safe boundary; it is not force-killed. The user can open the official release
or quit. Closing the required-update window never unlocks the application.

`mandatory: true` effectively makes the published version the minimum allowed
version. Use it only for a genuinely critical incompatibility or security fix.

## Safe publication order

1. Update the canonical version in `mail_exporter/__init__.py`.
2. Update the changelog, documentation, package metadata, and `version.json` in
   the release candidate.
3. Run all tests and build the final `.deb` and source ZIP.
4. Calculate new SHA-256 hashes for the final artifacts.
5. Create the GitHub release and upload the artifacts.
6. Confirm that `/releases/latest` opens the published release.
7. Only then publish the final `version.json` on `main`.
8. Confirm that the raw URL returns HTTP 200 and the intended JSON.

Never publish a mandatory manifest before its usable release and artifacts are
available. The human-facing download destination is:

```text
https://github.com/ehstbr/IMAP-Exporter/releases/latest
```
