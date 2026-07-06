# icloud-reminders-mcp

A local [MCP](https://modelcontextprotocol.io) server that gives an AI agent
(e.g. Claude Code) **read + write access to Apple Reminders** over **iCloud
CalDAV** — no Mac required. It talks only to Apple's servers and the local MCP
client, and manages the same reminder lists that sync to your iPhone/iPad,
including lists shared via Family Sharing.

Reminders are `VTODO` items in CalDAV; each reminder *list* is a task-capable
calendar collection. Calendar events (`VEVENT`), Contacts, Mail, and Notes are
out of scope.

> [!IMPORTANT]
> **Viability depends on your account.** Since iOS 13 / macOS 10.15, Apple
> moved Reminders to a newer (CloudKit) store. **Only reminder lists that were
> never "upgraded" to the new format remain reachable over CalDAV.** On some
> accounts that is *zero* lists. Run `scripts/doctor.py` (below) once before
> relying on this — it is a go/no-go probe. Apple may remove CalDAV access at
> any time; this is an undocumented, partially-compliant surface.

## Why CalDAV (and not `pyicloud`)

Apple exposes no REST/JSON API for Reminders. CalDAV (RFC 4791) is the only
official-protocol, cross-platform route. Auth is a **static app-specific
password** — no interactive 2FA, no token refresh. We deliberately avoid
`pyicloud` (a private web API that broke on Apple's SRP-6a auth change).

## Requirements

- Python **3.11+**
- An Apple ID with an **app-specific password** (see below)
- [`uv`](https://docs.astral.sh/uv/) (recommended) or `pip` + `venv`

## Install

```bash
git clone https://github.com/Lingnik/icloud-reminders-mcp
cd icloud-reminders-mcp
uv venv && uv pip install -e ".[dev]"
```

## Generate an app-specific password

1. Sign in at [appleid.apple.com](https://appleid.apple.com).
2. **Sign-In & Security → App-Specific Passwords → Generate**.
3. Label it (e.g. `icloud-reminders-mcp`). You get `xxxx-xxxx-xxxx-xxxx`.
4. This is the **kill switch**: revoke it there to instantly cut access.
   Changing your Apple ID password also invalidates it.

> An app-specific password is **all-or-nothing** — it grants access to your
> whole iCloud account at Apple's end, not just Reminders. Treat it as a
> secret, and see [SECURITY.md](SECURITY.md).

## Configure

Configuration is via environment variables only (never committed). Copy
[`.env.example`](.env.example) to `.env` and fill it in, **or** — preferred on a
host with [1Password CLI](https://developer.1password.com/docs/cli/) — keep the
real values in 1Password and inject them at launch:

```bash
# ~/.secrets holds op:// references, not secrets:
#   ICLOUD_USERNAME="op://<vault>/icloud-reminders-mcp/username"
#   ICLOUD_APP_PASSWORD="op://<vault>/icloud-reminders-mcp/password"
op run --env-file ~/.secrets -- icloud-reminders-mcp
```

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `ICLOUD_USERNAME` | yes | — | Apple ID email |
| `ICLOUD_APP_PASSWORD` | yes | — | App-specific password (`xxxx-xxxx-xxxx-xxxx`) |
| `ICLOUD_CALDAV_URL` | no | `https://caldav.icloud.com/` | Root URL; the library discovers your account's `pNN` shard from it — do not hardcode a shard |
| `REMINDERS_ALLOW_DELETE` | no | `false` | Enable the destructive `delete_reminder` tool |
| `REMINDERS_LIST_ALLOWLIST` | no | *(all)* | Comma-separated list names the server may touch |
| `ICLOUD_REQUEST_TIMEOUT` | no | `30` | Per-request timeout (seconds) |

## Go/no-go probe

Run once against your real account to confirm CalDAV reachability and a full
create → read → complete → delete round-trip:

```bash
op run --env-file ~/.secrets -- python scripts/doctor.py --list "Reminders"
```

It fails loudly if no VTODO-capable list is found, and reports which fields
survived the round-trip. It is never run in CI. After it succeeds, confirm the
throwaway reminder appeared (and cleared) on your iPhone.

## Use with Claude Code

Add to your MCP config (e.g. `~/.claude/mcp.json` or via `claude mcp add`).
Using 1Password so no secret is written into the config file:

```json
{
  "mcpServers": {
    "icloud-reminders": {
      "command": "op",
      "args": [
        "run", "--env-file", "/home/you/.secrets", "--",
        "uv", "--directory", "/home/you/git/icloud-reminders-mcp", "run",
        "icloud-reminders-mcp"
      ]
    }
  }
}
```

If you would rather put values inline (less safe), set them under `"env"` and
call `uv` directly:

```json
{
  "mcpServers": {
    "icloud-reminders": {
      "command": "uv",
      "args": ["--directory", "/home/you/git/icloud-reminders-mcp", "run", "icloud-reminders-mcp"],
      "env": {
        "ICLOUD_USERNAME": "you@icloud.com",
        "ICLOUD_APP_PASSWORD": "xxxx-xxxx-xxxx-xxxx",
        "REMINDERS_ALLOW_DELETE": "false"
      }
    }
  }
}
```

## Tools

| Tool | Type | Purpose |
|---|---|---|
| `list_lists` | read | Reminder lists (`list_id`, `list_name`, url, count) |
| `list_reminders` | read | Filter by list / completed / due range; paginated (`limit`/`offset`) |
| `get_reminder` | read | One reminder by uid |
| `create_reminder` | write | New VTODO (title, list, due, notes, priority, url) |
| `complete_reminder` | write | Mark complete |
| `update_reminder` | write | Patch fields; preserves everything it doesn't manage |
| `delete_reminder` | **destructive** | Gated by `REMINDERS_ALLOW_DELETE` **and** `confirm: true` |

**Reminder JSON shape** (normalized from VTODO):

```json
{
  "uid": "…", "list_id": "…", "list_name": "Family TODOs", "list_url": "…",
  "title": "…", "notes": "…|null",
  "due": "2026-07-10T09:00:00-07:00|2026-07-10|null", "due_all_day": false,
  "completed": false, "completed_at": "…|null",
  "priority": 0, "priority_label": "none|low|medium|high",
  "percent_complete": 0, "url": "…|null",
  "created": "…", "modified": "…", "etag": "…"
}
```

Notes on the model:

- **Lists are keyed by `list_id`** (stable, URL-derived), with `list_name` as a
  mutable label. Tools accept either an id or a name.
- **Priority** is the raw iCalendar 0–9 integer plus a friendly label. Apple's
  UI only has none/low/medium/high and will collapse arbitrary integers to
  1/5/9 when you edit in the app — fidelity beyond the four buckets is not
  guaranteed.
- **Updates preserve unknown properties.** `update_reminder`/`complete_reminder`
  parse the existing VTODO and mutate only known fields, so `RRULE`, `VALARM`,
  `RELATED-TO` (subtasks), and `X-APPLE-*` extensions survive. Editing
  recurrence/subtasks is **not** supported in v1 (they're preserved, not
  managed).
- **Update field semantics:** an omitted/null field is left unchanged; pass an
  empty string (`""`) to clear `notes`, `due`, or `url`.

## Development

```bash
uv run ruff check .
uv run pytest
```

Tests are network-free: pure mapping/config unit tests plus a fake-CalDAV layer
exercising discovery filtering, pagination, ETag-conflict retry, and delete
gating. Fixtures are synthetic — never captured from a real account.

## Troubleshooting

- **No lists found / empty.** Most likely the CloudKit-upgrade issue above. Try
  a list you know is old, or one freshly created that you have not migrated.
- **401 / auth rejected.** You're using your Apple ID password, not an
  app-specific password, or it was revoked/expired. Generate a new one.
- **PROPFIND hangs / flaky connects.** A known iCloud quirk on dual-stack
  hosts; try forcing **IPv4 or IPv6** (e.g. disable one stack, or set a
  hosts/route preference) and retry. Raise `ICLOUD_REQUEST_TIMEOUT` if needed.
- **A field didn't stick.** iCloud may normalize or drop properties on write.
  The doctor script's round-trip report tells you what your account actually
  preserves.

## License

MIT — see [LICENSE](LICENSE).
