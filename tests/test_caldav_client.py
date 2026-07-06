"""Integration tests against a fake CalDAV layer — no real network."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date

import pytest
from caldav.lib.error import NotFoundError
from icalendar import Calendar

from icloud_reminders_mcp import vtodo
from icloud_reminders_mcp.caldav_client import ETagMismatchError, RemindersClient
from icloud_reminders_mcp.config import Config
from icloud_reminders_mcp.errors import (
    ConflictError,
    DeleteNotAllowedError,
    ListNotFoundError,
    ReminderNotFoundError,
)

BASE = {"ICLOUD_USERNAME": "me@icloud.com", "ICLOUD_APP_PASSWORD": "aaaa-bbbb-cccc-dddd"}


def make_ical(title: str, **kwargs) -> bytes:
    cal, _ = vtodo.build_vtodo(title=title, **kwargs)
    return cal.to_ical()


def completed_ical(title: str) -> bytes:
    cal, _ = vtodo.build_vtodo(title=title)
    vtodo.mark_completed(cal.walk("VTODO")[0])
    return cal.to_ical()


class FakeObj:
    def __init__(self, calendar: FakeCalendar, uid: str):
        self._calendar = calendar
        self.uid = uid
        self.etag = f"etag-{uid}"

    @property
    def icalendar_component(self):
        return self._calendar.store[self.uid].walk("VTODO")[0]

    @contextmanager
    def edit_icalendar_component(self):
        yield self.icalendar_component  # mutated in place on the stored calendar

    def save(self):
        self._calendar.on_save()

    def delete(self):
        del self._calendar.store[self.uid]


class FakeCalendar:
    def __init__(self, url: str, name: str, comps=("VTODO",), objects=()):
        self.url = url
        self._name = name
        self._comps = list(comps)
        self.store: dict[str, Calendar] = {}
        self.save_conflicts = 0
        for ical in objects:
            self.add(ical)

    @property
    def name(self) -> str:
        return self._name

    def get_supported_components(self):
        return list(self._comps)

    def add(self, ical: bytes) -> str:
        cal = Calendar.from_ical(ical)
        uid = str(cal.walk("VTODO")[0]["uid"])
        self.store[uid] = cal
        return uid

    def todos(self):
        return [FakeObj(self, uid) for uid in self.store]

    def search(self, todo=True, include_completed=False):
        objs = []
        for uid, cal in self.store.items():
            status = str(cal.walk("VTODO")[0].get("status") or "").upper()
            if not include_completed and status == "COMPLETED":
                continue
            objs.append(FakeObj(self, uid))
        return objs

    def save_todo(self, ical):
        uid = self.add(ical if isinstance(ical, bytes) else ical.encode())
        return FakeObj(self, uid)

    def todo_by_uid(self, uid):
        if uid not in self.store:
            raise NotFoundError(uid)
        return FakeObj(self, uid)

    def on_save(self):
        if self.save_conflicts > 0:
            self.save_conflicts -= 1
            raise ETagMismatchError("412 Precondition Failed")


class FakePrincipal:
    def __init__(self, calendars):
        self._calendars = calendars

    def calendars(self):
        return self._calendars


class FakeClient:
    def __init__(self, calendars):
        self._principal = FakePrincipal(calendars)

    def principal(self):
        return self._principal


def make_client(calendars, **cfg) -> RemindersClient:
    config = Config.from_env({**BASE, **cfg})
    return RemindersClient(config, client=FakeClient(calendars), sleeper=lambda _s: None)


# --- discovery --------------------------------------------------------------


def test_discovery_filters_to_vtodo_and_derives_id():
    events = FakeCalendar("https://p1.icloud.com/cal/events/", "Events", comps=("VEVENT",))
    tasks = FakeCalendar("https://p1.icloud.com/cal/reminders/", "Reminders", comps=("VTODO",))
    client = make_client([events, tasks])
    lists = client.list_lists()
    assert [item["list_name"] for item in lists] == ["Reminders"]
    assert lists[0]["list_id"] == "reminders"
    assert lists[0]["count"] == 0


def test_missing_component_set_is_treated_as_task_capable():
    unknown = FakeCalendar("https://p1/cal/x/", "Legacy", comps=())
    client = make_client([unknown])
    assert [item["list_name"] for item in client.list_lists()] == ["Legacy"]


def test_allowlist_restricts_visible_lists():
    fam = FakeCalendar("https://p1/cal/fam/", "Family")
    secret = FakeCalendar("https://p1/cal/secret/", "Secret")
    client = make_client([fam, secret], REMINDERS_LIST_ALLOWLIST="Family")
    assert [item["list_name"] for item in client.list_lists()] == ["Family"]


# --- read -------------------------------------------------------------------


def test_list_reminders_defaults_to_incomplete_and_paginates():
    cal = FakeCalendar("https://p1/cal/r/", "R")
    cal.add(make_ical("open-a"))
    cal.add(completed_ical("done-b"))
    client = make_client([cal])

    res = client.list_reminders()
    titles = [r["title"] for r in res["reminders"]]
    assert titles == ["open-a"]
    assert res["total"] == 1

    res_all = client.list_reminders(completed=True)
    assert res_all["total"] == 2

    page = client.list_reminders(completed=True, limit=1, offset=0)
    assert page["returned"] == 1
    assert page["truncated"] is True


def test_due_range_filter_excludes_out_of_range_and_undated():
    cal = FakeCalendar("https://p1/cal/r/", "R")
    cal.add(make_ical("early", due=date(2026, 7, 1)))
    cal.add(make_ical("late", due=date(2026, 7, 20)))
    cal.add(make_ical("nodue"))
    client = make_client([cal])
    res = client.list_reminders(due_after="2026-07-10")
    assert [r["title"] for r in res["reminders"]] == ["late"]


def test_get_reminder_not_found():
    client = make_client([FakeCalendar("https://p1/cal/r/", "R")])
    with pytest.raises(ReminderNotFoundError):
        client.get_reminder("nope")


# --- create / update / complete --------------------------------------------


def test_create_then_get_roundtrip():
    cal = FakeCalendar("https://p1/cal/r/", "R")
    client = make_client([cal])
    created = client.create_reminder(title="Buy milk", due="2026-07-10", priority=1, notes="n")
    assert created["title"] == "Buy milk"
    got = client.get_reminder(created["uid"])
    assert got["priority"] == 1
    assert got["due_all_day"] is True
    assert got["notes"] == "n"


def test_create_requires_list_when_ambiguous():
    a = FakeCalendar("https://p1/cal/a/", "A")
    b = FakeCalendar("https://p1/cal/b/", "B")
    client = make_client([a, b])
    with pytest.raises(ListNotFoundError):
        client.create_reminder(title="x")


def test_update_patches_and_preserves():
    cal = FakeCalendar("https://p1/cal/r/", "R")
    uid = cal.add(make_ical("orig", notes="keep"))
    client = make_client([cal])
    res = client.update_reminder(uid, title="new")
    assert res["title"] == "new"
    assert res["notes"] == "keep"


def test_complete_reminder():
    cal = FakeCalendar("https://p1/cal/r/", "R")
    uid = cal.add(make_ical("t"))
    client = make_client([cal])
    res = client.complete_reminder(uid)
    assert res["completed"] is True
    assert res["percent_complete"] == 100


def test_update_retries_on_etag_conflict():
    cal = FakeCalendar("https://p1/cal/r/", "R")
    uid = cal.add(make_ical("orig"))
    cal.save_conflicts = 1  # first save 412, second succeeds
    client = make_client([cal])
    res = client.update_reminder(uid, title="new")
    assert res["title"] == "new"


def test_update_conflict_exhausted_raises():
    cal = FakeCalendar("https://p1/cal/r/", "R")
    uid = cal.add(make_ical("orig"))
    cal.save_conflicts = 99
    client = make_client([cal])
    with pytest.raises(ConflictError):
        client.update_reminder(uid, title="new")


# --- delete gating ----------------------------------------------------------


def test_delete_disabled_by_default():
    cal = FakeCalendar("https://p1/cal/r/", "R")
    uid = cal.add(make_ical("t"))
    client = make_client([cal])
    with pytest.raises(DeleteNotAllowedError):
        client.delete_reminder(uid, confirm=True)


def test_delete_requires_confirm():
    cal = FakeCalendar("https://p1/cal/r/", "R")
    uid = cal.add(make_ical("t"))
    client = make_client([cal], REMINDERS_ALLOW_DELETE="true")
    with pytest.raises(DeleteNotAllowedError):
        client.delete_reminder(uid, confirm=False)


def test_delete_succeeds_when_enabled_and_confirmed():
    cal = FakeCalendar("https://p1/cal/r/", "R")
    uid = cal.add(make_ical("t"))
    client = make_client([cal], REMINDERS_ALLOW_DELETE="true")
    res = client.delete_reminder(uid, confirm=True)
    assert res["deleted"] is True
    with pytest.raises(ReminderNotFoundError):
        client.get_reminder(uid)
