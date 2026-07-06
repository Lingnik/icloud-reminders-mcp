"""Pure VTODO <-> dict mapping and construction. No network, no CalDAV.

Design rule (from the design review): updates NEVER rebuild a VTODO from JSON.
We mutate the parsed ``icalendar`` component in place, touching only fields this
server understands, so unknown properties (RRULE, VALARM, RELATED-TO, X-APPLE-*,
GEO, CATEGORIES, ...) survive a read-modify-write untouched.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from icalendar import Calendar, Todo

PRODID = "-//icloud-reminders-mcp//EN"

# Sentinel meaning "caller did not supply this field" (leave unchanged). This is
# distinct from ``None``/`""` which mean "clear this field".
UNCHANGED: Any = object()


# --- priority ---------------------------------------------------------------
#
# RFC 5545 PRIORITY: 0 = undefined, 1-4 = high, 5 = medium, 6-9 = low. Apple's
# Reminders UI only has none/low/medium/high and will collapse arbitrary ints to
# 1/5/9 on edit, so we return the raw int AND a friendly label but promise
# fidelity on neither beyond the four buckets.

_LABEL_TO_PRIORITY = {"none": 0, "high": 1, "medium": 5, "low": 9}


def priority_to_label(priority: int | None) -> str:
    if not priority:  # None or 0
        return "none"
    if 1 <= priority <= 4:
        return "high"
    if priority == 5:
        return "medium"
    return "low"  # 6-9


def label_to_priority(label: str) -> int:
    try:
        return _LABEL_TO_PRIORITY[label.strip().lower()]
    except KeyError as exc:
        raise ValueError(
            f"unknown priority label {label!r}; expected one of {list(_LABEL_TO_PRIORITY)}"
        ) from exc


# --- due dates --------------------------------------------------------------


def parse_due(value: str) -> date | datetime:
    """Parse an ISO-8601 string into a ``date`` (all-day) or ``datetime`` (timed).

    A bare ``YYYY-MM-DD`` becomes an all-day due (``VALUE=DATE``); anything with a
    time component becomes a ``DATE-TIME``. A trailing ``Z`` is accepted as UTC.
    """
    v = value.strip()
    if len(v) == 10 and v.count("-") == 2 and "T" not in v:
        return date.fromisoformat(v)
    if v.endswith(("Z", "z")):
        v = v[:-1] + "+00:00"
    return datetime.fromisoformat(v)


def _format_due(dt: date | datetime | None) -> tuple[str | None, bool]:
    if dt is None:
        return None, False
    if isinstance(dt, datetime):
        return dt.isoformat(), False
    return dt.isoformat(), True  # bare date -> all-day


# --- component helpers ------------------------------------------------------


def _text(todo: Todo, key: str) -> str | None:
    val = todo.get(key)
    return str(val) if val is not None else None


def _dt_iso(todo: Todo, key: str) -> str | None:
    prop = todo.get(key)
    if prop is None:
        return None
    dt = getattr(prop, "dt", None)
    return dt.isoformat() if dt is not None else None


def _set_or_clear(todo: Todo, key: str, value: Any) -> None:
    if key in todo:
        del todo[key]
    if value not in (None, ""):
        todo.add(key, value)


def _bump_timestamps(todo: Todo, now: datetime) -> None:
    for key in ("last-modified", "dtstamp"):
        if key in todo:
            del todo[key]
        todo.add(key, now)


# --- mapping ----------------------------------------------------------------


def vtodo_to_dict(
    todo: Todo,
    *,
    list_id: str,
    list_name: str,
    list_url: str,
    etag: str | None = None,
) -> dict[str, Any]:
    """Normalise a parsed VTODO component into the server's JSON shape."""
    due_prop = todo.get("due")
    due_dt = getattr(due_prop, "dt", None) if due_prop is not None else None
    due_str, all_day = _format_due(due_dt)

    status = (_text(todo, "status") or "").upper()
    completed_at = _dt_iso(todo, "completed")
    percent_raw = todo.get("percent-complete")
    percent = int(percent_raw) if percent_raw is not None else None
    completed = status == "COMPLETED" or completed_at is not None or percent == 100

    priority_raw = todo.get("priority")
    priority = int(priority_raw) if priority_raw is not None else 0

    return {
        "uid": _text(todo, "uid"),
        "list_id": list_id,
        "list_name": list_name,
        "list_url": list_url,
        "title": _text(todo, "summary"),
        "notes": _text(todo, "description"),
        "due": due_str,
        "due_all_day": all_day,
        "completed": completed,
        "completed_at": completed_at,
        "priority": priority,
        "priority_label": priority_to_label(priority),
        "percent_complete": percent,
        "url": _text(todo, "url"),
        "created": _dt_iso(todo, "created"),
        "modified": _dt_iso(todo, "last-modified"),
        "etag": etag,
    }


# --- construction / mutation ------------------------------------------------


def build_vtodo(
    *,
    title: str,
    due: date | datetime | None = None,
    notes: str | None = None,
    priority: int | None = None,
    url: str | None = None,
    uid: str | None = None,
    now: datetime | None = None,
) -> tuple[Calendar, str]:
    """Build a fresh VCALENDAR wrapping one VTODO. Returns ``(calendar, uid)``.

    UID is client-generated so create is idempotent/retry-safe; the CalDAV
    resource should be PUT to ``<calendar-url>/<uid>.ics``.
    """
    uid = uid or f"{uuid.uuid4()}@icloud-reminders-mcp"
    now = now or datetime.now(UTC)

    cal = Calendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")

    todo = Todo()
    todo.add("uid", uid)
    todo.add("dtstamp", now)
    todo.add("created", now)
    todo.add("last-modified", now)
    todo.add("summary", title)
    todo.add("status", "NEEDS-ACTION")
    todo.add("percent-complete", 0)
    if notes:
        todo.add("description", notes)
    if priority is not None:
        todo.add("priority", int(priority))
    if url:
        todo.add("url", url)
    if due is not None:
        todo.add("due", due)

    cal.add_component(todo)
    cal.add_missing_timezones()  # embed VTIMEZONE so tz-aware DUE round-trips
    return cal, uid


def apply_updates(
    todo: Todo,
    *,
    title: Any = UNCHANGED,
    notes: Any = UNCHANGED,
    due: Any = UNCHANGED,
    priority: Any = UNCHANGED,
    url: Any = UNCHANGED,
    now: datetime | None = None,
) -> bool:
    """Mutate ``todo`` in place. Returns True if anything changed.

    Each field: ``UNCHANGED`` (default) leaves it alone; ``None``/`""` clears it
    (where clearable); any other value sets it. ``title`` cannot be cleared.
    Unknown properties on ``todo`` are never touched.
    """
    now = now or datetime.now(UTC)
    changed = False

    if title is not UNCHANGED and title not in (None, ""):
        if "summary" in todo:
            del todo["summary"]
        todo.add("summary", title)
        changed = True

    if notes is not UNCHANGED:
        _set_or_clear(todo, "description", notes)
        changed = True

    if url is not UNCHANGED:
        _set_or_clear(todo, "url", url)
        changed = True

    if priority is not UNCHANGED and priority is not None:
        if "priority" in todo:
            del todo["priority"]
        todo.add("priority", int(priority))
        changed = True

    if due is not UNCHANGED:
        if "due" in todo:
            del todo["due"]
        if due not in (None, ""):
            todo.add("due", due)
        changed = True

    if changed:
        _bump_timestamps(todo, now)
    return changed


def mark_completed(todo: Todo, *, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    for key in ("status", "completed", "percent-complete"):
        if key in todo:
            del todo[key]
    todo.add("status", "COMPLETED")
    todo.add("completed", now.astimezone(UTC))
    todo.add("percent-complete", 100)
    _bump_timestamps(todo, now)


def mark_incomplete(todo: Todo, *, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    for key in ("status", "completed", "percent-complete"):
        if key in todo:
            del todo[key]
    todo.add("status", "NEEDS-ACTION")
    todo.add("percent-complete", 0)
    _bump_timestamps(todo, now)
