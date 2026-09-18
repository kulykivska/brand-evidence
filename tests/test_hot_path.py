"""The daily jobs must not repeat work, and must not lock the database while
they are on the network.

Both behaviours are invisible in a small database and decisive in a real one: a
run capturing thirty mentions launched thirty browsers, asked one host for its
robots.txt thirty times, and held the single write lock for the whole archive
poll, which is exactly when the digest wants to read.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from brand_evidence.app import App
from brand_evidence.capture.screenshot import Capturer
from brand_evidence.core import evidence_log
from brand_evidence.core.db import session_scope
from brand_evidence.core.models import ArchiveSnapshot, Run
from brand_evidence.core.runs import tracked_run
from brand_evidence.ingest.archive_poll import _record_attempt, run_archive_poll
from brand_evidence.ingest.crawler import UNAVAILABLE, RobotsCache
from brand_evidence.ingest.hook import DuplicatePostError, ingest_post
from tests.factories import FakeArchive, FakeCapturer


class FakePage:
    def goto(self, url: str, **_kwargs: Any) -> Any:
        self.url = url
        return type("R", (), {"status": 200})()

    def wait_for_timeout(self, _ms: int) -> None: ...

    def content(self) -> str:
        return "<html>fake</html>"

    def screenshot(self, **_kwargs: Any) -> bytes:
        return b"\x89PNG"

    def emulate_media(self, **_kwargs: Any) -> None: ...

    def pdf(self, **_kwargs: Any) -> bytes:
        return b"%PDF"

    def evaluate(self, _script: str) -> str:
        return "fake-agent"


class FakeContext:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser

    def new_page(self) -> FakePage:
        return FakePage()

    def close(self) -> None:
        self.browser.closed_contexts += 1


class FakeBrowser:
    def __init__(self) -> None:
        self.contexts = 0
        self.closed_contexts = 0
        self.closed = False
        self.connected = True
        self.browser_type = type("T", (), {"name": "chromium"})()
        self.version = "test"

    def new_context(self, **_kwargs: Any) -> FakeContext:
        self.contexts += 1
        return FakeContext(self)

    def is_connected(self) -> bool:
        return self.connected

    def close(self) -> None:
        self.closed = True
        self.connected = False


class FakePlaywright:
    launches = 0
    last: FakeBrowser | None = None

    def __init__(self) -> None:
        self.chromium = type("C", (), {"launch": lambda _self, **_k: FakePlaywright._launch()})()

    @staticmethod
    def _launch() -> FakeBrowser:
        FakePlaywright.launches += 1
        FakePlaywright.last = FakeBrowser()
        return FakePlaywright.last

    def start(self) -> FakePlaywright:
        return self

    def stop(self) -> None:
        return None


def test_one_browser_serves_every_page_in_a_run(monkeypatch: pytest.MonkeyPatch) -> None:
    FakePlaywright.launches = 0
    monkeypatch.setattr("playwright.sync_api.sync_playwright", FakePlaywright)
    capturer = Capturer()
    try:
        for n in range(5):
            result = capturer.capture_url(f"https://news.example.com/{n}")
            assert result.png == b"\x89PNG"
    finally:
        capturer.close()
    browser = FakePlaywright.last
    assert FakePlaywright.launches == 1
    assert browser is not None
    # One context per page, each closed: nothing carries from one to the next.
    assert (browser.contexts, browser.closed_contexts) == (5, 5)
    assert browser.closed


def test_a_browser_that_died_is_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chromium OOM-killed mid-run used to fail every remaining capture, and
    each of those failures spent one of the mention's three attempts."""
    FakePlaywright.launches = 0
    monkeypatch.setattr("playwright.sync_api.sync_playwright", FakePlaywright)
    capturer = Capturer()
    try:
        capturer.capture_url("https://news.example.com/a")
        dead = FakePlaywright.last
        assert dead is not None
        dead.connected = False
        capturer.capture_url("https://news.example.com/b")
    finally:
        capturer.close()
    assert FakePlaywright.launches == 2


def test_close_is_idempotent_and_the_capturer_survives_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakePlaywright.launches = 0
    monkeypatch.setattr("playwright.sync_api.sync_playwright", FakePlaywright)
    capturer = Capturer()
    capturer.capture_url("https://news.example.com/a")
    capturer.close()
    capturer.close()
    capturer.capture_url("https://news.example.com/b")
    capturer.close()
    assert FakePlaywright.launches == 2


def test_robots_is_fetched_once_per_host() -> None:
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cache = RobotsCache(client)
    try:
        assert cache.allows("https://news.example.com/a")
        assert cache.allows("https://news.example.com/b")
        assert not cache.allows("https://news.example.com/private/x")
        assert cache.allows("https://other.example.com/a")
    finally:
        cache.close()
    assert fetched == [
        "https://news.example.com/robots.txt",
        "https://other.example.com/robots.txt",
    ]


def test_a_host_that_will_not_answer_is_asked_once_and_not_crawled() -> None:
    """RFC 9309: unavailable is not permission. It is also not a refusal - the
    mention stays pending instead of being written off on a 429."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, text="slow down")

    cache = RobotsCache(httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        assert cache.verdict("https://busy.example.com/a") == UNAVAILABLE
        assert cache.verdict("https://busy.example.com/b") == UNAVAILABLE
        assert not cache.allows("https://busy.example.com/a")
    finally:
        cache.close()
    assert calls["n"] == 1


def test_a_host_that_refuses_the_connection_is_not_crawled_either() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    cache = RobotsCache(httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        assert cache.verdict("https://down.example.com/a") == UNAVAILABLE
    finally:
        cache.close()


def test_credentials_in_a_url_do_not_become_a_cache_key_or_a_request() -> None:
    """A feed emitting a fresh userinfo per URL used to force one robots.txt
    fetch per URL, and the credential went into the request and the log."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text="User-agent: *\nDisallow:\n")

    cache = RobotsCache(httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        assert cache.allows("https://tok1@news.example.com/a")
        assert cache.allows("https://tok2@news.example.com/b")
    finally:
        cache.close()
    assert seen == ["https://news.example.com/robots.txt"]


class LockWatchingArchive(FakeArchive):
    """Answers `check` only after proving the write lock is free."""

    def __init__(self, db_path: Path) -> None:
        super().__init__()
        self.db_path = db_path
        self.lock_was_held: list[bool] = []

    def check(self, url: str, submitted_at: str, provider_ref: str | None) -> str | None:
        conn = sqlite3.connect(str(self.db_path), timeout=1.0)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.rollback()
            self.lock_was_held.append(False)
        except sqlite3.OperationalError:
            self.lock_was_held.append(True)
        finally:
            conn.close()
        return super().check(url, submitted_at, provider_ref)


def test_archive_poll_does_not_hold_the_write_lock_while_it_waits(be: App) -> None:
    be.archive = FakeArchive()
    ingest_post(
        be,
        platform="x",
        url="https://x.com/northwind/status/1",
        body="Northwind ships a thing",
        published_at=None,
        capturer=FakeCapturer(),
    )
    watcher = LockWatchingArchive(Path(be.settings.db_path))
    watcher.check_result = "https://web.archive.org/web/2026/x"
    be.archive = watcher
    run_archive_poll(be)
    assert watcher.lock_was_held == [False]
    with session_scope(be.sessions) as session:
        assert session.scalars(select(ArchiveSnapshot)).one().status == "confirmed"


def test_the_poll_asks_the_archive_about_the_time_the_snapshot_was_submitted(be: App) -> None:
    """Not "now": the provider uses it as the floor for which captures count,
    and now excludes every snapshot that exists."""
    archive = FakeArchive()
    be.archive = archive
    ingest_post(
        be,
        platform="x",
        url="https://x.com/northwind/status/1",
        body="Northwind ships a thing",
        published_at=None,
        capturer=FakeCapturer(),
    )
    with session_scope(be.sessions) as session:
        submitted_at = session.scalars(select(ArchiveSnapshot)).one().submitted_at
    run_archive_poll(be)
    assert archive.checked_since == [submitted_at]


def test_two_polls_do_not_lose_an_attempt(be: App) -> None:
    """attempts was read before the network call and written back absolute, so
    two overlapping polls both wrote the same number and one attempt vanished -
    a snapshot could then never reach MAX_ATTEMPTS and never resolve."""
    be.archive = FakeArchive()
    ingest_post(
        be,
        platform="x",
        url="https://x.com/northwind/status/2",
        body="Northwind ships another",
        published_at=None,
        capturer=FakeCapturer(),
    )
    with session_scope(be.sessions) as session:
        snapshot_id = session.scalars(select(ArchiveSnapshot)).one().id
    _record_attempt(be, snapshot_id, None, None)
    _record_attempt(be, snapshot_id, None, None)
    with be.sessions() as session:
        assert session.get(ArchiveSnapshot, snapshot_id).attempts == 2


def test_a_duplicate_post_is_not_a_failed_run(be: App) -> None:
    """Re-posting a URL filed a traceback the operator had to read and dismiss."""
    be.archive = FakeArchive()
    for _ in range(2):
        try:
            ingest_post(
                be,
                platform="x",
                url="https://x.com/northwind/status/3",
                body="Northwind again",
                published_at=None,
                capturer=FakeCapturer(),
            )
        except DuplicatePostError:
            pass
    with be.sessions() as session:
        runs = session.scalars(select(Run).where(Run.job == "hook")).all()
    assert [r.status for r in runs] == ["ok", "ok"]
    assert runs[1].stats["duplicate"] is True
    assert runs[1].error is None


def test_run_bookkeeping_survives_another_writer(be: App) -> None:
    """tracked_run wrote its rows on a plain session, which begins DEFERRED: a
    read-then-write there fails instantly with SQLITE_BUSY_SNAPSHOT, and no
    busy_timeout retries that. The job that reports failures must not be it."""
    with tracked_run(be.sessions, "test") as ctx:
        with session_scope(be.sessions) as session:
            evidence_log.append(session, "test.interleaved", {"n": 1})
        ctx.stats["done"] = True
    with be.sessions() as session:
        run = session.scalars(select(Run).where(Run.job == "test")).one()
    assert (run.status, run.stats) == ("ok", {"done": True})
