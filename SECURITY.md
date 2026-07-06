# Security

## Threat model in one line

The only credential is an Apple **app-specific password**, which at Apple's end
grants access to your **entire iCloud account** (Mail, Contacts, Calendar,
Find My, …), not just Reminders. Apple provides no way to scope it. Everything
below is a *client-side* mitigation on top of that reality.

## Kill switch

Revoke the app-specific password at
[appleid.apple.com](https://appleid.apple.com) → **Sign-In & Security →
App-Specific Passwords**. It takes effect immediately. Changing your Apple ID
password also invalidates all app-specific passwords.

## What this server does to reduce blast radius

- **Least-privilege tool surface.** Only the seven reminder tools; no access to
  other iCloud data types.
- **Delete is off by default.** `delete_reminder` is disabled unless
  `REMINDERS_ALLOW_DELETE=true`, and even then requires an explicit
  `confirm: true` on every call.
- **Optional list allowlist.** `REMINDERS_LIST_ALLOWLIST` restricts the server
  to named lists; all others are invisible to every tool.
- **No secret logging.** The app password is never logged. Config is logged only
  through a redacted view. Request/response bodies (which contain reminder
  content) are not logged.
- **TLS only.** All traffic goes to `https://caldav.icloud.com` (and the
  discovered `pNN` shard). Certificate verification is on and not overridable.

## Handling secrets

- Never commit real credentials. Only `.env.example` (placeholders) is tracked;
  `.env` is gitignored.
- Prefer a secret manager. On the reference host, values live in 1Password and
  are injected with `op run --env-file ~/.secrets -- …`, so no secret ever
  touches disk in plaintext or an MCP config file.

## Prompt-injection note (MCP-specific)

Reminder **titles and notes flow into an LLM**. A reminder authored by someone
else (e.g. a shared/family list) could contain prompt-injection text. This
server cannot neutralize that; the delete gate and allowlist are defense in
depth. Treat reminder content as untrusted input in whatever agent consumes it.

## Reporting

This is a personal project. Open a GitHub issue for non-sensitive reports. For
anything sensitive, avoid including real reminder content, list names, or
credentials in the report.
