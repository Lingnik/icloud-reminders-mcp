"""Pure mapping/construction tests — no network."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from icalendar import Calendar
from icalendar.prop import vRecur

from icloud_reminders_mcp import vtodo


def _todo(cal: Calendar):
    return cal.walk("VTODO")[0]


def _reparse(cal: Calendar):
    return Calendar.from_ical(cal.to_ical()).walk("VTODO")[0]


def test_priority_to_label_buckets():
    assert vtodo.priority_to_label(None) == "none"
    assert vtodo.priority_to_label(0) == "none"
    assert vtodo.priority_to_label(1) == "high"
    assert vtodo.priority_to_label(4) == "high"
    assert vtodo.priority_to_label(5) == "medium"
    assert vtodo.priority_to_label(6) == "low"
    assert vtodo.priority_to_label(9) == "low"


def test_label_to_priority_roundtrip_and_error():
    for label in ("none", "high", "medium", "low"):
        assert vtodo.priority_to_label(vtodo.label_to_priority(label)) == label
    with pytest.raises(ValueError):
        vtodo.label_to_priority("urgent")


def test_parse_due_date_vs_datetime():
    assert vtodo.parse_due("2026-07-10") == date(2026, 7, 10)
    dt = vtodo.parse_due("2026-07-10T17:00:00-05:00")
    assert isinstance(dt, datetime)
    assert dt.utcoffset().total_seconds() == -5 * 3600
    z = vtodo.parse_due("2026-07-10T17:00:00Z")
    assert z.utcoffset().total_seconds() == 0


def test_build_and_map_tz_datetime_roundtrip():
    due = datetime(2026, 7, 10, 17, 0, tzinfo=ZoneInfo("America/Chicago"))
    cal, uid = vtodo.build_vtodo(title="T", due=due, notes="n", priority=1, url="https://x")
    todo = _reparse(cal)
    d = vtodo.vtodo_to_dict(todo, list_id="L", list_name="List", list_url="u", etag="e1")
    assert d["uid"] == uid
    assert d["title"] == "T"
    assert d["notes"] == "n"
    assert d["priority"] == 1
    assert d["priority_label"] == "high"
    assert d["url"] == "https://x"
    assert d["etag"] == "e1"
    assert d["due_all_day"] is False
    assert vtodo.parse_due(d["due"]) == due  # same instant survives the round-trip


def test_all_day_due_preserved():
    cal, _ = vtodo.build_vtodo(title="A", due=date(2026, 7, 10))
    d = vtodo.vtodo_to_dict(_reparse(cal), list_id="L", list_name="n", list_url="u")
    assert d["due"] == "2026-07-10"
    assert d["due_all_day"] is True


def test_apply_updates_preserves_unknown_properties():
    cal, _ = vtodo.build_vtodo(title="orig", notes="keep")
    todo = _todo(cal)
    todo.add("rrule", vRecur({"FREQ": ["WEEKLY"]}))
    todo.add("x-apple-custom", "keepme")
    todo.add("related-to", "parent-uid")

    vtodo.apply_updates(todo, title="new", notes="hello")

    assert str(todo["summary"]) == "new"
    assert str(todo["description"]) == "hello"
    # everything this server doesn't understand must survive untouched
    assert todo.get("rrule") is not None
    assert str(todo["x-apple-custom"]) == "keepme"
    assert str(todo["related-to"]) == "parent-uid"


def test_apply_updates_unchanged_vs_clear():
    cal, _ = vtodo.build_vtodo(title="t", notes="keep", url="https://x")
    todo = _todo(cal)

    # not passing notes leaves it unchanged
    changed = vtodo.apply_updates(todo, title="t2")
    assert changed is True
    assert str(todo["description"]) == "keep"

    # empty string clears
    vtodo.apply_updates(todo, notes="", url="")
    assert "description" not in todo
    assert "url" not in todo


def test_apply_updates_noop_returns_false():
    cal, _ = vtodo.build_vtodo(title="t")
    assert vtodo.apply_updates(_todo(cal)) is False


def test_complete_then_incomplete():
    cal, _ = vtodo.build_vtodo(title="t")
    todo = _todo(cal)

    vtodo.mark_completed(todo)
    d = vtodo.vtodo_to_dict(todo, list_id="L", list_name="n", list_url="u")
    assert d["completed"] is True
    assert d["completed_at"] is not None
    assert d["percent_complete"] == 100

    vtodo.mark_incomplete(todo)
    d2 = vtodo.vtodo_to_dict(todo, list_id="L", list_name="n", list_url="u")
    assert d2["completed"] is False
    assert d2["completed_at"] is None
    assert d2["percent_complete"] == 0
