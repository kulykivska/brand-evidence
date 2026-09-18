"""An interrupted ingest is finished, not refused."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from brand_evidence.app import App
from brand_evidence.core.models import Artifact, Post, Run
from brand_evidence.ingest.backfill import run_backfill
from brand_evidence.ingest.hook import DuplicatePostError, ingest_post
from tests.factories import FakeArchive, FakeCapturer

URL = "https://x.com/northwind/status/1"


class BrokenCapturer(FakeCapturer):
    def capture_url(self, url: str, *, allow_local: bool = False):  # type: ignore[no-untyped-def]
        self.calls.append(url)
        raise TimeoutError("playwright timed out")


def ingest(be: App, capturer: FakeCapturer):  # type: ignore[no-untyped-def]
    return ingest_post(
        be,
        platform="x",
        url=URL,
        body="Northwind ships a thing",
        published_at=None,
        capturer=capturer,
    )


def test_a_post_whose_capture_failed_is_completed_on_the_next_call(be: App) -> None:
    """A Playwright timeout used to leave a post in the hash chain with zero
    artifacts, and every retry answered "already recorded"."""
    be.archive = FakeArchive()
    with pytest.raises(TimeoutError):
        ingest(be, BrokenCapturer())

    with be.sessions() as s:
        post = s.scalars(select(Post)).one()
        assert s.scalars(select(Artifact)).all() == []

    result = ingest(be, FakeCapturer())
    assert result.created is False
    assert result.post_id == post.id
    assert set(result.artifact_hashes) == {"screenshot_png", "pdf", "html"}

    with be.sessions() as s:
        assert len(s.scalars(select(Artifact)).all()) == 3
        assert len(s.scalars(select(Post)).all()) == 1


def test_a_genuinely_duplicate_post_is_still_refused(be: App) -> None:
    be.archive = FakeArchive()
    ingest(be, FakeCapturer())
    with pytest.raises(DuplicatePostError):
        ingest(be, FakeCapturer())


def test_a_backfill_writes_one_run_not_one_per_row(be: App, tmp_path: Path) -> None:
    """ingest_post opened a run of its own, so a 500-row CSV filed 500 run rows
    in the digest - and every duplicate row filed one marked failed."""
    be.archive = FakeArchive()
    csv_path = tmp_path / "posts.csv"
    csv_path.write_text(
        "platform,url,body,published_at\n"
        "x,https://x.com/northwind/status/10,First,2026-01-01T00:00:00+00:00\n"
        "x,https://x.com/northwind/status/11,Second,2026-01-02T00:00:00+00:00\n",
        encoding="utf-8",
    )
    counts = run_backfill(be, csv_path, capture=False, history=False)
    assert counts["imported"] == 2
    again = run_backfill(be, csv_path, capture=False, history=False)
    # Without capture a post has no artifacts by design, so "no artifacts" must
    # not read as "interrupted, resume it".
    assert (again["imported"], again["duplicates"]) == (0, 2)
    with be.sessions() as session:
        jobs = [r.job for r in session.scalars(select(Run))]
    assert jobs == ["backfill", "backfill"]
