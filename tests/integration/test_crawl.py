from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from brand_evidence.app import App
from brand_evidence.core import evidence_log
from brand_evidence.core.models import ArchiveSnapshot, Artifact, Mention, Run
from brand_evidence.ingest.crawler import run_crawl
from tests.factories import BrokenSource, FakeArchive, FakeCapturer, StaticSource, hit

SINCE = datetime(2026, 9, 1, tzinfo=UTC)


def test_crawl_with_one_failing_source_is_partial(be: App) -> None:
    be.archive = FakeArchive()
    capturer = FakeCapturer()
    sources = [
        StaticSource("hn_algolia", [hit("https://news.ycombinator.com/item?id=1")]),
        BrokenSource(),
    ]
    run_id = run_crawl(
        be, since=SINCE, sources=sources, capturer=capturer, robots_check=lambda _u: True
    )
    with be.sessions() as s:
        run = s.get(Run, run_id)
        assert run is not None
        assert run.status == "partial"
        assert run.stats["failed_sources"] == ["broken"]
        assert run.stats["new_mentions"] == 1
        assert run.finished_at is not None
        assert s.scalars(select(Mention)).one().status == "new"
        assert len(s.scalars(select(Artifact)).all()) == 3
        assert s.scalars(select(ArchiveSnapshot)).one().subject_type == "mention"
        assert evidence_log.verify(s).ok
    assert capturer.calls == ["https://news.ycombinator.com/item?id=1"]


def test_crawl_dedupes_and_merges_terms_without_recapture(be: App) -> None:
    be.archive = FakeArchive()
    first = FakeCapturer()
    run_crawl(
        be,
        since=SINCE,
        capturer=first,
        robots_check=lambda _u: True,
        sources=[
            StaticSource(
                "a", [hit("https://blog.example.com/post/?utm_source=x", terms=["Northwind"])]
            )
        ],
    )
    second = FakeCapturer()
    run_crawl(
        be,
        since=SINCE,
        capturer=second,
        robots_check=lambda _u: True,
        sources=[StaticSource("b", [hit("https://blog.example.com/post", terms=["North Wind"])])],
    )
    with be.sessions() as s:
        mentions = s.scalars(select(Mention)).all()
        assert len(mentions) == 1
        assert mentions[0].url == "https://blog.example.com/post"
        assert mentions[0].matched_terms == ["North Wind", "Northwind"]
        runs = s.scalars(select(Run).order_by(Run.started_at)).all()
        assert [r.status for r in runs] == ["ok", "ok"]
        assert runs[1].stats["updated_mentions"] == 1 and runs[1].stats["new_mentions"] == 0
    assert len(first.calls) == 1 and second.calls == []


def test_crawl_with_nothing_found_is_ok_with_zero_counts(be: App) -> None:
    run_id = run_crawl(be, since=SINCE, sources=[StaticSource("a", [])], capture=False)
    with be.sessions() as s:
        run = s.get(Run, run_id)
        assert run is not None and run.status == "ok" and run.stats["new_mentions"] == 0


def test_robots_disallow_skips_capture(be: App) -> None:
    be.archive = FakeArchive()
    capturer = FakeCapturer()
    run_id = run_crawl(
        be,
        since=SINCE,
        capturer=capturer,
        robots_check=lambda _u: False,
        sources=[StaticSource("a", [hit("https://closed.example.com/x")])],
    )
    assert capturer.calls == []
    with be.sessions() as s:
        run = s.get(Run, run_id)
        assert run is not None and run.stats["capture_skipped"] == ["https://closed.example.com/x"]
