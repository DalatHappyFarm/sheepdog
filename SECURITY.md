# Security policy

## Reporting a vulnerability

Please report security problems privately.
Use GitHub private vulnerability reporting on this repository (Security tab, "Report a vulnerability").
Do not open a public issue for a vulnerability.
We aim to acknowledge a report within 7 days.

## In scope

- Token auth and the sign-in cookie (`internal/server`, `sheepdog.py`).
- Hook ingest and the folder allowlist (`SHEEPDOG_ALLOW`).
- Redaction of secrets and the rewriting of paths and names before storage.
- The pages in `web/` (XSS, CSP, token handling).
- The CLI (`task`, `agent`, `brief`, `wait`, `done`, `archive`, `hook`, `purge`, `scrub`).

## Out of scope

- Your own deployment's token, host or network setup.
- Reports that need the access token.
- Denial of service by volume beyond the documented body and rate limits.

## Security model in short

- One bearer token (`SHEEPDOG_TOKEN`) guards every route except the public brand images and the sign-in page; the server binds to 127.0.0.1 by default.
- Hooks are accepted only from the folders in `SHEEPDOG_ALLOW` when it is set; anything else is dropped before it is stored.
- Secrets (tokens, passwords, keys, bearer headers) are redacted, and home, temp and root paths, the user name and the host name are rewritten, before anything is written to the database.
- The server does not run anything: it only stores events and shows them.
