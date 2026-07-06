#!/usr/bin/env python3
"""End-to-end smoke test / go-no-go probe against a REAL iCloud account.

This is the viability gate: iCloud only exposes reminder lists over CalDAV that
were never "upgraded" to the newer (CloudKit) format. Run this once before
trusting the server. It:

  1. authenticates and enumerates VTODO-capable lists (fails loudly if none);
  2. creates a throwaway reminder with a tz-aware due, priority and notes;
  3. reads it back and reports which fields survived the round-trip;
  4. completes it, then deletes it (only if REMINDERS_ALLOW_DELETE=true, else
     leaves it completed with a warning).

NEVER run this in CI — it writes to a real account and may print list names.

Usage (on this host, with secrets from 1Password):
    op run --env-file ~/.secrets -- python scripts/doctor.py --list "Reminders"
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from icloud_reminders_mcp.caldav_client import RemindersClient
from icloud_reminders_mcp.config import Config
from icloud_reminders_mcp.errors import RemindersError


def _ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def _warn(msg: str) -> None:
    print(f"  ⚠ {msg}")


def _fail(msg: str) -> None:
    print(f"  ✗ {msg}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", help="target list (id or name) for the write test")
    parser.add_argument(
        "--keep", action="store_true", help="do not complete/delete the test reminder"
    )
    args = parser.parse_args()

    try:
        config = Config.from_env()
    except RemindersError as exc:
        _fail(str(exc))
        return 2

    client = RemindersClient(config)
    print("== 1. Discovery ==")
    try:
        lists = client.list_lists()
    except RemindersError as exc:
        _fail(f"discovery failed: {exc}")
        return 1

    if not lists:
        _fail("No VTODO-capable reminder lists found over CalDAV.")
        _warn(
            "On iOS 13+ only lists never 'upgraded' to the new format sync via "
            "CalDAV. This account may have no compatible lists — the server "
            "cannot work until at least one exists."
        )
        return 1
    _ok(f"found {len(lists)} list(s): " + ", ".join(item['list_name'] for item in lists))

    target = args.list or (lists[0]["list_id"] if len(lists) == 1 else None)
    if target is None:
        _warn("multiple lists exist; pass --list <name> to run the write test.")
        _warn("stopping after discovery.")
        return 0

    print(f"== 2. Round-trip write test on list {target!r} ==")
    tz = ZoneInfo("America/Chicago")
    due = (datetime.now(tz) + timedelta(days=1)).replace(microsecond=0)
    stamp = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    sent = {
        "title": f"[doctor] icloud-reminders-mcp check {stamp}",
        "notes": "throwaway — safe to delete",
        "priority": 1,
        "due": due.isoformat(),
    }
    try:
        created = client.create_reminder(list_ref=target, **sent)
    except RemindersError as exc:
        _fail(f"create failed: {exc}")
        return 1
    uid = created["uid"]
    _ok(f"created uid={uid}")

    got = client.get_reminder(uid, list_ref=target)
    for field, expected in (("title", sent["title"]), ("notes", sent["notes"])):
        actual = got.get(field)
        verdict = "preserved" if actual == expected else f"CHANGED -> {actual!r}"
        (_ok if actual == expected else _warn)(f"{field}: {verdict}")
    (_ok if got.get("priority") == 1 else _warn)(
        f"priority: got {got.get('priority')} ({got.get('priority_label')})"
    )
    (_ok if got.get("due") else _warn)(
        f"due: got {got.get('due')} (all_day={got.get('due_all_day')})"
    )

    if args.keep:
        _warn(f"--keep set; leaving reminder {uid} in place. Delete it manually.")
        return 0

    print("== 3. Complete ==")
    completed = client.complete_reminder(uid, list_ref=target)
    (_ok if completed.get("completed") else _fail)(f"completed={completed.get('completed')}")

    print("== 4. Delete ==")
    if not config.allow_delete:
        _warn("REMINDERS_ALLOW_DELETE is not true; leaving the completed test reminder.")
        _warn(f"Delete it manually or re-run with REMINDERS_ALLOW_DELETE=true. uid={uid}")
        return 0
    client.delete_reminder(uid, list_ref=target, confirm=True)
    _ok(f"deleted uid={uid}")
    print("\nGO: full create/read/complete/delete round-trip succeeded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
