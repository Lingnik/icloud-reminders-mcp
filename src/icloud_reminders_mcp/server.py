"""MCP server: registers the reminder tools over stdio.

Parameter conventions for write tools (documented for callers):
- An omitted / null optional field means "leave unchanged".
- An empty string ("") clears a clearable field (notes, due, url).
- `priority` uses the raw iCalendar scale 0-9 (0 = none, 1-4 high, 5 medium,
  6-9 low); Apple's UI collapses these to none/low/medium/high.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .caldav_client import RemindersClient
from .config import Config
from .errors import RemindersError
from .vtodo import UNCHANGED

logger = logging.getLogger("icloud_reminders_mcp")

mcp = FastMCP("icloud-reminders")

_client: RemindersClient | None = None


def get_client() -> RemindersClient:
    global _client
    if _client is None:
        _client = RemindersClient(Config.from_env())
    return _client


def _unchanged_if_none(value: Any) -> Any:
    return UNCHANGED if value is None else value


# --- read tools -------------------------------------------------------------

_READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)


@mcp.tool(annotations=_READ)
def list_lists() -> list[dict[str, Any]]:
    """List the iCloud reminder lists (VTODO collections) this server can see.

    Returns each list's stable `list_id`, mutable `list_name`, URL, and item count.
    """
    return get_client().list_lists()


@mcp.tool(annotations=_READ)
def list_reminders(
    list: str | None = None,
    completed: bool = False,
    due_before: str | None = None,
    due_after: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """List reminders, newest-due first. Defaults to incomplete only.

    `list` accepts a list_id or display name (all lists if omitted). `due_before`
    / `due_after` are ISO-8601 dates or datetimes. Results are paginated via
    `limit`/`offset`; the response includes `total` and a `truncated` flag.
    """
    return get_client().list_reminders(
        list_ref=list,
        completed=completed,
        due_before=due_before,
        due_after=due_after,
        limit=limit,
        offset=offset,
    )


@mcp.tool(annotations=_READ)
def get_reminder(uid: str, list: str | None = None) -> dict[str, Any]:
    """Fetch a single reminder by its uid (optionally scoped to one list)."""
    return get_client().get_reminder(uid, list_ref=list)


# --- write tools ------------------------------------------------------------


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=True))
def create_reminder(
    title: str,
    list: str | None = None,
    due: str | None = None,
    notes: str | None = None,
    priority: int | None = None,
    url: str | None = None,
) -> dict[str, Any]:
    """Create a reminder (VTODO). `list` is required unless exactly one exists.

    `due` is an ISO-8601 date (all-day) or datetime with timezone.
    """
    return get_client().create_reminder(
        title=title, list_ref=list, due=due, notes=notes, priority=priority, url=url
    )


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True, openWorldHint=True))
def complete_reminder(uid: str, list: str | None = None) -> dict[str, Any]:
    """Mark a reminder complete (STATUS:COMPLETED + COMPLETED + PERCENT-COMPLETE:100)."""
    return get_client().complete_reminder(uid, list_ref=list)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True, openWorldHint=True))
def update_reminder(
    uid: str,
    list: str | None = None,
    title: str | None = None,
    due: str | None = None,
    notes: str | None = None,
    priority: int | None = None,
    url: str | None = None,
    completed: bool | None = None,
) -> dict[str, Any]:
    """Patch mutable fields of a reminder, preserving everything else.

    Omitted/null fields are left unchanged; pass an empty string ("") to clear
    `notes`, `due`, or `url`. Set `completed` true/false to complete/reopen.
    Uses ETag optimistic concurrency and retries on conflict.
    """
    return get_client().update_reminder(
        uid,
        list_ref=list,
        title=_unchanged_if_none(title),
        notes=_unchanged_if_none(notes),
        due=_unchanged_if_none(due),
        priority=_unchanged_if_none(priority),
        url=_unchanged_if_none(url),
        completed=_unchanged_if_none(completed),
    )


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
def delete_reminder(uid: str, list: str | None = None, confirm: bool = False) -> dict[str, Any]:
    """Permanently delete a reminder. Gated: requires REMINDERS_ALLOW_DELETE=true
    on the server AND an explicit confirm=true argument."""
    return get_client().delete_reminder(uid, list_ref=list, confirm=confirm)


# --- entrypoint -------------------------------------------------------------


def _configure_logging() -> None:
    # stdout is the MCP transport; all logs must go to stderr.
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _startup_discovery(client: RemindersClient) -> None:
    """Best-effort auth + discovery so misconfiguration is obvious in the logs.

    Logs list names only (never contents) and never crashes the server: a
    transient failure here shouldn't stop tools from working later.
    """
    try:
        logger.info("config: %s", client.config.redacted())
        lists = client.list_lists()
        names = ", ".join(item["list_name"] for item in lists) or "(none)"
        logger.info("discovered %d reminder list(s): %s", len(lists), names)
        if not lists:
            logger.warning(
                "No VTODO-capable lists found. On iOS 13+ only reminder lists that "
                "were never 'upgraded' remain reachable via CalDAV."
            )
    except RemindersError as exc:
        logger.warning("startup discovery failed (tools may still work): %s", exc)
    except Exception as exc:  # never let discovery kill startup
        logger.warning("unexpected startup discovery error: %s", exc)


def main() -> None:
    _configure_logging()
    try:
        client = get_client()
    except RemindersError as exc:
        logger.error("configuration error: %s", exc)
        raise SystemExit(2) from exc
    _startup_discovery(client)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
