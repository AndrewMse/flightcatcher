"""Session durability.

Playwright isn't needed here: the cookie snapshot/restore logic is exercised
against a fake context, which is the part that decides whether a session
survives a restart.
"""

from __future__ import annotations

import json

import pytest

from flightcatcher.browser import BrowserSession
from flightcatcher.settings import BrowserSettings

COOKIES = [
    {"name": "session", "value": "abc", "domain": ".wizzair.com", "path": "/"},
    {"name": "RequestVerificationToken", "value": "xyz", "domain": ".wizzair.com"},
]


class FakeContext:
    def __init__(self, cookies=None, fail_read=False):
        self._cookies = list(cookies or COOKIES)
        self.added: list = []
        self.fail_read = fail_read

    async def cookies(self):
        if self.fail_read:
            raise RuntimeError("browser is gone")
        return self._cookies

    async def add_cookies(self, cookies):
        self.added.extend(cookies)


def make_session(tmp_path, context=None) -> BrowserSession:
    settings = BrowserSettings()
    settings.profile_dir = tmp_path / "profile"
    settings.cookie_backup = tmp_path / "cookies.json"
    session = BrowserSession.__new__(BrowserSession)  # skip the Playwright check
    session.settings = settings
    session.headless = True
    session._playwright = None
    session.context = context if context is not None else FakeContext()
    return session


async def test_cookies_are_saved_and_restored(tmp_path) -> None:
    session = make_session(tmp_path)
    assert await session.save_cookies() == 2

    backup = session.settings.cookie_backup
    assert json.loads(backup.read_text())[0]["name"] == "session"

    fresh = make_session(tmp_path, context=FakeContext(cookies=[]))
    assert await fresh.restore_cookies() == 2
    assert [c["name"] for c in fresh.context.added] == [
        "session",
        "RequestVerificationToken",
    ]


async def test_backup_is_not_world_readable(tmp_path) -> None:
    """A live session cookie is a credential."""
    session = make_session(tmp_path)
    await session.save_cookies()
    assert session.settings.cookie_backup.stat().st_mode & 0o077 == 0


async def test_restoring_without_a_backup_is_harmless(tmp_path) -> None:
    session = make_session(tmp_path)
    assert await session.restore_cookies() == 0
    assert session.context.added == []


async def test_corrupt_backup_does_not_explode(tmp_path) -> None:
    session = make_session(tmp_path)
    session.settings.cookie_backup.parent.mkdir(parents=True, exist_ok=True)
    session.settings.cookie_backup.write_text("{ not json")
    assert await session.restore_cookies() == 0


async def test_saving_survives_a_dead_browser(tmp_path) -> None:
    """Shutdown snapshots must never turn into a crash on the way out."""
    session = make_session(tmp_path, context=FakeContext(fail_read=True))
    assert await session.save_cookies() == 0


async def test_saving_without_a_context_is_a_no_op(tmp_path) -> None:
    session = make_session(tmp_path)
    session.context = None
    assert await session.save_cookies() == 0
    assert await session.restore_cookies() == 0


async def test_touch_snapshots_only_when_the_session_is_alive(tmp_path, monkeypatch) -> None:
    session = make_session(tmp_path)
    pages = []

    class FakePage:
        async def close(self):
            pages.append("closed")

    async def fake_new_page():
        return FakePage()

    session.new_page = fake_new_page

    async def logged_in(page=None):
        return True

    session.is_logged_in = logged_in
    assert await session.touch() is True
    assert session.settings.cookie_backup.exists()
    assert pages == ["closed"]

    # A dead session must not overwrite a good backup with nothing.
    session.settings.cookie_backup.unlink()

    async def logged_out(page=None):
        return False

    session.is_logged_in = logged_out
    assert await session.touch() is False
    assert not session.settings.cookie_backup.exists()
