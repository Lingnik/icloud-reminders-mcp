"""CalDAV access to iCloud Reminders (VTODO): discovery, CRUD, ETag concurrency.

Network lives here and nowhere else. The mapping to/from JSON is delegated to
``vtodo`` so this module stays about protocol behaviour: shard discovery,
allowlist enforcement, optimistic concurrency, and transient-error backoff.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from caldav import DAVClient
from caldav.lib.error import AuthorizationError, DAVError, NotFoundError

from .config import Config
from .errors import (
    ConflictError,
    DeleteNotAllowedError,
    DiscoveryError,
    ListNotFoundError,
    ReminderNotFoundError,
    UpstreamError,
)
from .vtodo import (
    UNCHANGED,
    apply_updates,
    build_vtodo,
    mark_completed,
    mark_incomplete,
    parse_due,
    vtodo_to_dict,
)

# caldav renamed this across versions; import defensively so we work on 3.2+.
try:  # pragma: no cover - import shim
    from caldav.lib.error import ETagMismatchError
except ImportError:  # pragma: no cover
    class ETagMismatchError(DAVError):  # type: ignore[no-redef]
        """Fallback if the installed caldav lacks a dedicated class."""

_TRANSIENT_STATUS = {429, 500, 502, 503, 504}


def _list_id_from_url(url: object) -> str:
    """Derive a stable id from a collection URL: its last path segment."""
    return str(url).rstrip("/").rsplit("/", 1)[-1]


def _is_transient(exc: BaseException) -> bool:
    name = type(exc).__name__
    if name in {"ConnectionError", "Timeout", "ConnectTimeout", "ReadTimeout"}:
        return True
    if isinstance(exc, (ETagMismatchError, AuthorizationError, NotFoundError)):
        return False
    if isinstance(exc, DAVError):
        status = getattr(exc, "status", None)
        if status in _TRANSIENT_STATUS:
            return True
        text = str(exc)
        return any(str(code) in text for code in _TRANSIENT_STATUS)
    return False


@dataclass
class _Collection:
    list_id: str
    name: str
    calendar: Any


class RemindersClient:
    """A thin, safe wrapper over ``caldav`` for VTODO reminder lists."""

    def __init__(
        self,
        config: Config,
        *,
        client: DAVClient | None = None,
        client_factory: Callable[[], DAVClient] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        max_attempts: int = 4,
    ) -> None:
        self.config = config
        self._sleeper = sleeper
        self._max_attempts = max_attempts
        self._client = client
        self._client_factory = client_factory
        self._principal: Any = None
        self._collections: list[_Collection] | None = None

    # --- connection / discovery --------------------------------------------

    def _connect(self) -> Any:
        if self._principal is not None:
            return self._principal
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                self._client = DAVClient(
                    url=self.config.caldav_url,
                    username=self.config.username,
                    password=self.config.app_password,
                    timeout=self.config.request_timeout,
                )
        try:
            # principal() follows iCloud's redirect to the account's pNN shard.
            self._principal = self._retry(self._client.principal)
        except AuthorizationError as exc:
            raise UpstreamError(
                "iCloud rejected the credentials. Check ICLOUD_USERNAME and that "
                "ICLOUD_APP_PASSWORD is a valid app-specific password (not your "
                "Apple ID password)."
            ) from exc
        except DAVError as exc:
            raise DiscoveryError(f"CalDAV discovery failed: {exc}") from exc
        return self._principal

    def _load_collections(self) -> list[_Collection]:
        if self._collections is not None:
            return self._collections
        principal = self._connect()
        try:
            calendars = self._retry(principal.calendars)
        except DAVError as exc:
            raise DiscoveryError(f"Could not enumerate calendars: {exc}") from exc

        collections: list[_Collection] = []
        for cal in calendars:
            try:
                comps = self._retry(cal.get_supported_components)
            except DAVError:
                comps = []
            # Reminder lists advertise VTODO; event calendars advertise VEVENT.
            # A missing component set means "supports all" per RFC 4791, so keep
            # it too, but skip anything that is explicitly events-only.
            if comps and "VTODO" not in comps:
                continue
            name = self._safe_name(cal)
            if not self.config.list_allowed(name):
                continue
            collections.append(
                _Collection(list_id=_list_id_from_url(cal.url), name=name, calendar=cal)
            )
        self._collections = collections
        return collections

    def _safe_name(self, cal: Any) -> str:
        try:
            return str(self._retry(lambda: cal.name)) or _list_id_from_url(cal.url)
        except DAVError:
            return _list_id_from_url(cal.url)

    # --- retry --------------------------------------------------------------

    def _retry(self, fn: Callable[[], Any]) -> Any:
        delay = 0.5
        last_exc: BaseException | None = None
        for attempt in range(self._max_attempts):
            try:
                return fn()
            except Exception as exc:
                if not _is_transient(exc) or attempt == self._max_attempts - 1:
                    raise
                last_exc = exc
                self._sleeper(min(delay, 8.0))
                delay *= 2
        assert last_exc is not None  # pragma: no cover
        raise last_exc

    # --- resolution ---------------------------------------------------------

    def _resolve_list(self, ref: str | None, *, require: bool = False) -> _Collection:
        collections = self._load_collections()
        if ref is None:
            if len(collections) == 1:
                return collections[0]
            if require:
                names = ", ".join(c.name for c in collections) or "(none)"
                raise ListNotFoundError(
                    "A target list is required because several exist. "
                    f"Available: {names}."
                )
            raise ListNotFoundError("No list specified and no unique default exists.")
        lowered = ref.strip().lower()
        for coll in collections:
            if coll.list_id == ref or coll.name.strip().lower() == lowered:
                return coll
        names = ", ".join(c.name for c in collections) or "(none)"
        raise ListNotFoundError(f"List {ref!r} not found. Available: {names}.")

    def _find_todo(self, uid: str, ref: str | None) -> tuple[Any, _Collection]:
        collections = [self._resolve_list(ref)] if ref else self._load_collections()
        for coll in collections:
            try:
                obj = self._retry(lambda c=coll: c.calendar.todo_by_uid(uid))
            except NotFoundError:
                continue
            return obj, coll
        raise ReminderNotFoundError(f"No reminder with uid {uid!r} was found.")

    # --- mapping ------------------------------------------------------------

    def _obj_to_dict(self, obj: Any, coll: _Collection) -> dict[str, Any]:
        comp = obj.icalendar_component
        etag: str | None
        try:
            etag = obj.etag
        except Exception:  # etag is best-effort; never fail a read over it
            etag = None
        return vtodo_to_dict(
            comp,
            list_id=coll.list_id,
            list_name=coll.name,
            list_url=str(coll.calendar.url),
            etag=etag,
        )

    # --- public API ---------------------------------------------------------

    def list_lists(self) -> list[dict[str, Any]]:
        result = []
        for coll in self._load_collections():
            try:
                count = len(self._retry(lambda c=coll: c.calendar.todos()))
            except DAVError:
                count = None
            result.append(
                {
                    "list_id": coll.list_id,
                    "list_name": coll.name,
                    "list_url": str(coll.calendar.url),
                    "count": count,
                }
            )
        return result

    def list_reminders(
        self,
        *,
        list_ref: str | None = None,
        completed: bool = False,
        due_before: str | None = None,
        due_after: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        collections = [self._resolve_list(list_ref)] if list_ref else self._load_collections()
        before = _as_bound(due_before) if due_before else None
        after = _as_bound(due_after) if due_after else None

        rows: list[dict[str, Any]] = []
        for coll in collections:
            todos = self._retry(
                lambda c=coll: c.calendar.search(todo=True, include_completed=completed)
            )
            for obj in todos:
                comp = obj.icalendar_component
                if comp is None or getattr(comp, "name", None) != "VTODO":
                    continue  # guard against iCloud returning the collection itself
                row = self._obj_to_dict(obj, coll)
                if not completed and row["completed"]:
                    continue
                if (before or after) and not _due_in_range(row, before, after):
                    continue
                rows.append(row)

        rows.sort(key=lambda r: (r["due"] is None, r["due"] or "", (r["title"] or "").lower()))
        total = len(rows)
        page = rows[offset : offset + limit]
        return {
            "reminders": page,
            "total": total,
            "returned": len(page),
            "offset": offset,
            "limit": limit,
            "truncated": offset + limit < total,
        }

    def get_reminder(self, uid: str, *, list_ref: str | None = None) -> dict[str, Any]:
        obj, coll = self._find_todo(uid, list_ref)
        return self._obj_to_dict(obj, coll)

    def create_reminder(
        self,
        *,
        title: str,
        list_ref: str | None = None,
        due: str | None = None,
        notes: str | None = None,
        priority: int | None = None,
        url: str | None = None,
    ) -> dict[str, Any]:
        coll = self._resolve_list(list_ref, require=True)
        due_val: date | datetime | None = parse_due(due) if due else None
        cal_obj, uid = build_vtodo(
            title=title, due=due_val, notes=notes, priority=priority, url=url
        )
        obj = self._retry(lambda: coll.calendar.save_todo(cal_obj.to_ical()))
        try:
            return self._obj_to_dict(obj, coll)
        except Exception:  # fall back to a fresh fetch if the returned obj is thin
            refetched, _ = self._find_todo(uid, coll.list_id)
            return self._obj_to_dict(refetched, coll)

    def complete_reminder(self, uid: str, *, list_ref: str | None = None) -> dict[str, Any]:
        return self._edit_with_retry(uid, list_ref, lambda todo: mark_completed(todo))

    def update_reminder(
        self,
        uid: str,
        *,
        list_ref: str | None = None,
        title: Any = UNCHANGED,
        notes: Any = UNCHANGED,
        due: Any = UNCHANGED,
        priority: Any = UNCHANGED,
        url: Any = UNCHANGED,
        completed: Any = UNCHANGED,
    ) -> dict[str, Any]:
        if due is UNCHANGED:
            due_val: Any = UNCHANGED
        elif due in (None, ""):
            due_val = due
        else:
            due_val = parse_due(due)

        def mutate(todo: Any) -> None:
            apply_updates(
                todo, title=title, notes=notes, due=due_val, priority=priority, url=url
            )
            if completed is not UNCHANGED:
                if completed:
                    mark_completed(todo)
                else:
                    mark_incomplete(todo)

        return self._edit_with_retry(uid, list_ref, mutate)

    def delete_reminder(
        self, uid: str, *, list_ref: str | None = None, confirm: bool = False
    ) -> dict[str, Any]:
        if not self.config.allow_delete:
            raise DeleteNotAllowedError(
                "Deletion is disabled. Set REMINDERS_ALLOW_DELETE=true to enable it."
            )
        if not confirm:
            raise DeleteNotAllowedError(
                "Refusing to delete without an explicit confirm=true argument."
            )
        obj, coll = self._find_todo(uid, list_ref)
        self._retry(obj.delete)
        return {"uid": uid, "list_id": coll.list_id, "deleted": True}

    # --- internal edit loop -------------------------------------------------

    def _edit_with_retry(
        self, uid: str, list_ref: str | None, mutate: Callable[[Any], None]
    ) -> dict[str, Any]:
        _, coll = self._find_todo(uid, list_ref)
        for attempt in range(self._max_attempts):
            obj = self._retry(lambda: coll.calendar.todo_by_uid(uid))
            with obj.edit_icalendar_component() as todo:
                mutate(todo)
            try:
                self._retry(obj.save)
            except ETagMismatchError as exc:
                if attempt == self._max_attempts - 1:
                    raise ConflictError(
                        f"Could not update reminder {uid!r}: it kept changing "
                        "underneath us (ETag conflict)."
                    ) from exc
                continue
            return self._obj_to_dict(obj, coll)
        raise ConflictError(f"Could not update reminder {uid!r} after retries.")


# --- due-date filtering helpers --------------------------------------------


def _as_bound(value: str) -> datetime:
    parsed = parse_due(value)
    if isinstance(parsed, datetime):
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return datetime(parsed.year, parsed.month, parsed.day, tzinfo=UTC)


def _due_in_range(row: dict[str, Any], before: datetime | None, after: datetime | None) -> bool:
    if row["due"] is None:
        return False
    due = parse_due(row["due"])
    if isinstance(due, datetime):
        due_dt = due if due.tzinfo else due.replace(tzinfo=UTC)
    else:
        due_dt = datetime(due.year, due.month, due.day, tzinfo=UTC)
    if after and due_dt < after:
        return False
    return not (before and due_dt > before)
