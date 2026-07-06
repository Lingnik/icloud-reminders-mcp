"""Environment-driven configuration, allowlist, and safety flags. No network."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from .errors import ConfigError

DEFAULT_CALDAV_URL = "https://caldav.icloud.com/"

_TRUE = {"1", "true", "yes", "on"}


def _parse_bool(raw: str | None, default: bool = False) -> bool:
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUE


def _split_allowlist(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(name.strip() for name in raw.split(",") if name.strip())


@dataclass(frozen=True)
class Config:
    username: str
    app_password: str
    caldav_url: str = DEFAULT_CALDAV_URL
    allow_delete: bool = False
    list_allowlist: tuple[str, ...] = ()
    request_timeout: float = 30.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env

        username = (env.get("ICLOUD_USERNAME") or "").strip()
        app_password = (env.get("ICLOUD_APP_PASSWORD") or "").strip()
        missing = [
            name
            for name, value in (
                ("ICLOUD_USERNAME", username),
                ("ICLOUD_APP_PASSWORD", app_password),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                "Missing required environment variable(s): "
                + ", ".join(missing)
                + ". See .env.example; on this host inject them with "
                "`op run --env-file ~/.secrets -- icloud-reminders-mcp`."
            )

        caldav_url = (env.get("ICLOUD_CALDAV_URL") or DEFAULT_CALDAV_URL).strip()

        timeout_raw = (env.get("ICLOUD_REQUEST_TIMEOUT") or "").strip()
        try:
            request_timeout = float(timeout_raw) if timeout_raw else 30.0
        except ValueError as exc:
            raise ConfigError(
                f"ICLOUD_REQUEST_TIMEOUT must be a number, got {timeout_raw!r}"
            ) from exc

        return cls(
            username=username,
            app_password=app_password,
            caldav_url=caldav_url,
            allow_delete=_parse_bool(env.get("REMINDERS_ALLOW_DELETE"), False),
            list_allowlist=_split_allowlist(env.get("REMINDERS_LIST_ALLOWLIST")),
            request_timeout=request_timeout,
        )

    def list_allowed(self, name: str | None) -> bool:
        """Is a list with this display name permitted? Empty allowlist = all."""
        if not self.list_allowlist:
            return True
        if not name:
            return False
        lowered = name.strip().lower()
        return any(lowered == allowed.lower() for allowed in self.list_allowlist)

    def redacted(self) -> dict[str, object]:
        """A log-safe view of the config. Never includes the app password."""
        return {
            "username": self.username,
            "caldav_url": self.caldav_url,
            "allow_delete": self.allow_delete,
            "list_allowlist": list(self.list_allowlist),
            "request_timeout": self.request_timeout,
        }
