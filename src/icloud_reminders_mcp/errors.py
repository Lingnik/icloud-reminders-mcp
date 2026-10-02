"""Typed error hierarchy for icloud-reminders-mcp.

Kept dependency-free so it can be imported by every module without cycles.
"""

from __future__ import annotations


class RemindersError(Exception):
    """Base class for all errors raised by this package."""


class ConfigError(RemindersError):
    """Configuration is missing or invalid (e.g. required env var unset)."""


class DiscoveryError(RemindersError):
    """CalDAV discovery failed (principal/calendars could not be resolved)."""


class ListNotFoundError(RemindersError):
    """A named reminder list was not found or is not permitted by the allowlist."""


class InvalidUidError(RemindersError):
    """A reminder uid argument was empty or whitespace-only."""


class ReminderNotFoundError(RemindersError):
    """A reminder (VTODO) with the given uid was not found."""


class DeleteNotAllowedError(RemindersError):
    """A delete was requested but is disabled or not explicitly confirmed."""


class ConflictError(RemindersError):
    """Optimistic-concurrency conflict (ETag/If-Match, HTTP 412) after retries."""


class UpstreamError(RemindersError):
    """iCloud/CalDAV returned an error we could not recover from."""
