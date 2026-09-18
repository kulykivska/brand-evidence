from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from brand_evidence.app import App
from brand_evidence.core import evidence_log
from brand_evidence.core.models import ArchiveSnapshot, Artifact, EvidenceEntry, Post
from brand_evidence.ingest.hook import DuplicatePostError, ingest_post
from tests.factories import FakeArchive, FakeCapturer

FIXTURE = Path(__file__).parents[1] / "fixtures" / "post.html"


def test_hook_produces_post_artifacts_archive_and_four_log_entries(be: App) -> None:
    be.archive = FakeArchive()
    result = ingest_post(
        be,
        platform="threads",
        url="https://www.threads.net/@northwind/post/abc",
        body="Northwind called the podium again.",
        published_at="2026-09-14T10:00:00+00:00",
        external_id="abc",
        capturer=FakeCapturer(),
    )
    with be.sessions() as s:
        post = s.get(Post, result.post_id)
        assert post is not None and post.brand_mentioned and post.source_path == "hook"
        kinds = sorted(a.kind for a in s.scalars(select(Artifact)).all())
        assert kinds == ["html", "pdf", "screenshot_png"]
        snap = s.scalars(select(ArchiveSnapshot)).one()
        assert snap.status == "pending" and snap.subject_id == result.post_id
        events = [
            e.event_type for e in s.scalars(select(EvidenceEntry).order_by(EvidenceEntry.seq))
        ]
        assert events == [
            "post.created",
            "artifact.created",
            "artifact.created",
            "artifact.created",
            "archive_snapshot.submitted",
        ]
        assert evidence_log.verify(s).ok
    for digest in result.artifact_hashes.values():
        assert be.store.verify(digest)


def test_hook_rejects_duplicate_url(be: App) -> None:
    be.archive = FakeArchive()
    kwargs = dict(
        platform="linkedin", url="https://linkedin.com/posts/x", body="hi", published_at=None
    )
    ingest_post(be, capturer=FakeCapturer(), **kwargs)  # type: ignore[arg-type]
    with pytest.raises(DuplicatePostError):
        ingest_post(be, capturer=FakeCapturer(), **kwargs)  # type: ignore[arg-type]


def test_archive_failure_is_recorded_not_hidden(be: App) -> None:
    be.archive = FakeArchive(fail=True)
    result = ingest_post(
        be,
        platform="x",
        url="https://x.com/northwind/status/1",
        body="Northwind",
        published_at=None,
        capturer=FakeCapturer(),
    )
    assert result.archive_status == "pending"
    with be.sessions() as s:
        snap = s.scalars(select(ArchiveSnapshot)).one()
        assert snap.status == "pending" and snap.request_url == "https://x.com/northwind/status/1"


def _chromium_available() -> bool:
    caches = ("Library/Caches/ms-playwright", ".cache/ms-playwright")
    return any(
        any(Path.home().joinpath(c).glob("chromium*")) for c in caches
    )


@pytest.mark.skipif(
    not _chromium_available(), reason="Playwright Chromium not installed"
)
def test_real_playwright_capture_of_local_fixture(be: App) -> None:
    from brand_evidence.ingest.capture_pipeline import make_capturer

    be.archive = FakeArchive()
    url = FIXTURE.resolve().as_uri()
    # The capturer holds a browser open now, so a caller supplying one closes it.
    with make_capturer(be) as capturer:
        result = ingest_post(
            be,
            platform="other",
            url=url,
            body="Northwind fixture",
            published_at=None,
            capturer=capturer,
            allow_local=True,
        )
    with be.sessions() as s:
        png = s.scalars(select(Artifact).where(Artifact.kind == "screenshot_png")).one()
        html = s.scalars(select(Artifact).where(Artifact.kind == "html")).one()
        assert png.capture_meta["viewport"] == {"width": 1440, "height": 900}
        assert png.capture_meta["device_scale_factor"] == 2
        assert png.capture_meta["authenticated"] is False
        assert "playwright_version" in png.capture_meta
    assert be.store.get(png.sha256)[:8] == b"\x89PNG\r\n\x1a\n"
    assert b"Northwind fixture page" in be.store.get(html.sha256)
    assert be.store.get(result.artifact_hashes["pdf"])[:4] == b"%PDF"
