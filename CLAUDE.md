# icloud-reminders-mcp — project conventions

## Backlog: GitHub issues, always

All backlog items live as GitHub issues on `Lingnik/icloud-reminders-mcp` —
never TODO comments, never doc lists. When work items, ideas, deferred fixes, or
follow-ups surface, file an issue immediately with `gh issue create`. Reference
issues in commits (`Closes #N` / `Refs #N`). Check `gh issue list` first.

## Secrets

- Never hardcode credentials or write a real `.env`. Only `.env.example` is
  tracked. This repo is **public** — no real values, list names, or captured
  account fixtures ever get committed.
- Runtime secrets come from 1Password: `op run --env-file ~/.secrets -- …`. The
  refs are `ICLOUD_USERNAME` / `ICLOUD_APP_PASSWORD` →
  `op://kalipi-claude/icloud-reminders-mcp/{username,password}`.

## Architecture

- `config.py` — env parsing, allowlist, flags. No network.
- `vtodo.py` — pure VTODO⇄dict mapping + construction. No network. Updates
  **mutate the parsed component in place**; never rebuild a VTODO from JSON, so
  unknown properties (RRULE/VALARM/RELATED-TO/X-APPLE-*) are preserved.
- `caldav_client.py` — all network: discovery, CRUD, ETag concurrency, backoff.
- `server.py` — FastMCP tool registration (stdio).
- `errors.py` — typed error hierarchy.

## Testing

- Everything under `tests/` is network-free (pure logic + a fake CalDAV layer).
  Fixtures are synthetic — never from a real account.
- `scripts/doctor.py` is the only thing that hits the real account. **Never run
  it in CI.**
- Before pushing: `uv run ruff check .` and `uv run pytest` must pass.

## iCloud gotchas (hard-won; keep in mind)

- Discover from the root; never hardcode the `pNN-caldav.icloud.com` shard.
- Only non-"upgraded" (pre-CloudKit) reminder lists sync via CalDAV; some
  accounts have none.
- iCloud may normalize/drop properties on write and hides completed todos from
  the default query (`include_completed=True` to see them).
- Keep `create_list` (MKCALENDAR) out until round-trip fidelity is proven —
  CalDAV-created lists may not surface in the iOS app.
