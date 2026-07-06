"""Config parsing, allowlist, and flag tests — no network."""

from __future__ import annotations

import pytest

from icloud_reminders_mcp.config import Config
from icloud_reminders_mcp.errors import ConfigError

BASE = {
    "ICLOUD_USERNAME": "me@icloud.com",
    "ICLOUD_APP_PASSWORD": "aaaa-bbbb-cccc-dddd",
}


def test_missing_required_raises():
    with pytest.raises(ConfigError):
        Config.from_env({})
    with pytest.raises(ConfigError):
        Config.from_env({"ICLOUD_USERNAME": "me@icloud.com"})


def test_defaults():
    cfg = Config.from_env(BASE)
    assert cfg.caldav_url == "https://caldav.icloud.com/"
    assert cfg.allow_delete is False
    assert cfg.list_allowlist == ()
    assert cfg.request_timeout == 30.0


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on"])
def test_allow_delete_truthy(value):
    assert Config.from_env({**BASE, "REMINDERS_ALLOW_DELETE": value}).allow_delete is True


@pytest.mark.parametrize("value", ["false", "0", "", "no", "off"])
def test_allow_delete_falsy(value):
    assert Config.from_env({**BASE, "REMINDERS_ALLOW_DELETE": value}).allow_delete is False


def test_allowlist_parsing_and_matching():
    cfg = Config.from_env({**BASE, "REMINDERS_LIST_ALLOWLIST": " Family TODOs , Groceries "})
    assert cfg.list_allowlist == ("Family TODOs", "Groceries")
    assert cfg.list_allowed("family todos") is True
    assert cfg.list_allowed("Groceries") is True
    assert cfg.list_allowed("Secret") is False
    assert cfg.list_allowed(None) is False


def test_empty_allowlist_allows_everything():
    cfg = Config.from_env(BASE)
    assert cfg.list_allowed("Anything") is True
    assert cfg.list_allowed(None) is True


def test_redacted_never_leaks_password():
    cfg = Config.from_env(BASE)
    red = cfg.redacted()
    assert "app_password" not in red
    assert BASE["ICLOUD_APP_PASSWORD"] not in repr(red)
    # Apple ID local-part is masked so logs/issues don't reveal the full email.
    assert red["username"] == "m***@icloud.com"


def test_timeout_validation():
    with pytest.raises(ConfigError):
        Config.from_env({**BASE, "ICLOUD_REQUEST_TIMEOUT": "abc"})
    assert Config.from_env({**BASE, "ICLOUD_REQUEST_TIMEOUT": "12.5"}).request_timeout == 12.5
