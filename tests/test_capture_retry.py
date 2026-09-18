"""Evidence owed but not captured is captured later, not dropped.

A crawl used to capture only the mentions it had just discovered. Anything
the per-run budget cut off, and anything whose capture threw, was never
looked at again: the mention stayed in the database and the evidence for it
was lost quietly, with only a stats key as the trace.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from brand_evidence.app import App
from brand_evidence.core.models import Artifact, EvidenceEntry, Mention, Run
from brand_evidence.ingest.crawler import MAX_CAPTURE_ATTEMPTS, UNAVAILABLE, run_crawl
from tests.factories import FakeArchive, FakeCapturer, StaticSource, hit

SINCE = datetime(2026, 9, 1, tzinfo=UTC)
ALLOW = lambda _u: True  # noqa: E731 - a one-expression stub reads better inline


def urls(n: int) -> list[str]:
    return [f"https://example.com/post/{i}" for i in range(n)]


class FlakyCapturer(FakeCapturer):
    """Fails for one URL, captures everything else."""

    def __init__(self, failing_url: str) -> None:
        super().__init__()
        self.failing_url = failing_url

    def capture_url(self, url: str, *, allow_local: bool = False):  # type: ignore[no-untyped-def]
        if url == self.failing_url:
            self.calls.append(url)
            raise RuntimeError("page never loaded")
        return super().capture_url(url, allow_local=allow_local)


def test_mentions_past_the_budget_are_captured_by_the_next_run(be: App) -> None:
    be.archive = FakeArchive()
    be.settings.max_captures_per_run = 2
    source = StaticSource("hn_algolia", [hit(u) for u in urls(5)])

    first = FakeCapturer()
    run_id = run_crawl(be, since=SINCE, sources=[source], capturer=first, robots_check=ALLOW)
    with be.sessions() as s:
        run = s.get(Run, run_id)
        assert run is not None
        assert run.stats["captured"] == 2
        # The digest can now say how many are owed.
        assert run.stats["capture_pending"] == 3
        assert len(s.scalars(select(Artifact)).all()) == 2 * 3  # png, pdf, html

    # A second crawl that discovers nothing new still owes three captures.
    second = FakeCapturer()
    run_crawl(be, since=SINCE, sources=[StaticSource("hn_algolia", [])], capturer=second,
              robots_check=ALLOW)
    assert len(second.calls) == 2

    third = FakeCapturer()
    run_crawl(be, since=SINCE, sources=[StaticSource("hn_algolia", [])], capturer=third,
              robots_check=ALLOW)
    assert len(third.calls) == 1

    with be.sessions() as s:
        states = {m.url: m.capture_state for m in s.scalars(select(Mention)).all()}
        assert set(states.values()) == {"captured"}
        assert len(s.scalars(select(Artifact)).all()) == 5 * 3


def test_a_failing_capture_is_retried_then_given_up_on(be: App) -> None:
    be.archive = FakeArchive()
    failing = "https://example.com/post/0"
    source = StaticSource("hn_algolia", [hit(failing)])

    for attempt in range(1, MAX_CAPTURE_ATTEMPTS + 1):
        run_crawl(
            be,
            since=SINCE,
            sources=[source],
            capturer=FlakyCapturer(failing),
            robots_check=ALLOW,
        )
        with be.sessions() as s:
            mention = s.scalars(select(Mention)).one()
            assert mention.capture_attempts == attempt
            expected = "failed" if attempt >= MAX_CAPTURE_ATTEMPTS else "pending"
            assert mention.capture_state == expected

    # Given up on: a fourth run does not try it again, so one dead URL cannot
    # eat every future run's budget.
    capturer = FlakyCapturer(failing)
    run_crawl(be, since=SINCE, sources=[source], capturer=capturer, robots_check=ALLOW)
    assert capturer.calls == []


def test_a_skipped_host_is_not_retried(be: App) -> None:
    """robots and the skip list do not change between runs, so those are
    terminal rather than pending."""
    be.archive = FakeArchive()
    source = StaticSource("hn_algolia", [hit("https://example.com/post/0")])
    run_crawl(
        be, since=SINCE, sources=[source], capturer=FakeCapturer(), robots_check=lambda _u: False
    )
    with be.sessions() as s:
        assert s.scalars(select(Mention)).one().capture_state == "skipped"

    capturer = FakeCapturer()
    run_crawl(be, since=SINCE, sources=[source], capturer=capturer, robots_check=ALLOW)
    assert capturer.calls == []


def test_a_host_that_will_not_answer_leaves_the_mention_for_next_time(be: App) -> None:
    """A 429 on robots.txt is not permission and not a refusal: writing the
    mention off as skipped would lose it over a rate limit."""
    source = StaticSource("hn_algolia", [hit("https://busy.example.com/a")])
    run_crawl(
        be,
        since=SINCE,
        sources=[source],
        capturer=FakeCapturer(),
        robots_check=lambda _u: UNAVAILABLE,
    )
    with be.sessions() as session:
        mention = session.scalars(select(Mention)).one()
        assert mention.capture_state == "pending"
        run = session.scalars(select(Run).where(Run.job == "crawl")).one()
    assert run.status == "partial"
    assert run.stats["robots_unavailable"] == 1


def test_a_capture_state_change_is_in_the_evidence_log(be: App) -> None:
    source = StaticSource("hn_algolia", [hit("https://news.example.com/a")])
    run_crawl(be, since=SINCE, sources=[source], capturer=FakeCapturer(), robots_check=ALLOW)
    with be.sessions() as session:
        states = [
            e.payload
            for e in session.scalars(select(EvidenceEntry))
            if e.event_type == "mention.capture_state"
        ]
    assert [p["capture_state"] for p in states] == ["captured"]
