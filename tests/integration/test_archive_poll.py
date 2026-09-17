from __future__ import annotations

from sqlalchemy import select

from brand_evidence.app import App
from brand_evidence.capture.archiver import MAX_ATTEMPTS
from brand_evidence.core.db import session_scope
from brand_evidence.core.models import ArchiveSnapshot, EvidenceEntry, Run
from brand_evidence.ingest.archive_poll import run_archive_poll
from brand_evidence.ingest.hook import ingest_post
from tests.factories import FakeArchive, FakeCapturer


def _pending_post(be: App) -> str:
    be.archive = FakeArchive()
    ingest_post(
        be,
        platform="threads",
        url="https://threads.net/p/1",
        body="Northwind",
        published_at=None,
        capturer=FakeCapturer(),
    )
    with be.sessions() as s:
        return s.scalars(select(ArchiveSnapshot)).one().id


def test_poll_moves_pending_to_confirmed(be: App) -> None:
    snap_id = _pending_post(be)
    be.archive.check_result = "https://web.archive.org/web/20260914000000/https://threads.net/p/1"  # type: ignore[attr-defined]
    run_id = run_archive_poll(be)
    with be.sessions() as s:
        snap = s.get(ArchiveSnapshot, snap_id)
        assert snap is not None and snap.status == "confirmed"
        assert snap.snapshot_url and snap.confirmed_at and snap.attempts == 1
        assert snap.provider_ref == "spn2-1" and be.archive.last_ref == "spn2-1"  # type: ignore[attr-defined]
        assert s.get(Run, run_id).stats["confirmed"] == 1  # type: ignore[union-attr]
        assert "archive_snapshot.confirmed" in [
            e.event_type for e in s.scalars(select(EvidenceEntry))
        ]


def test_poll_respects_backoff_then_fails_after_budget(be: App) -> None:
    snap_id = _pending_post(be)
    run_archive_poll(be)  # attempt 1 (due immediately)
    run_archive_poll(be)  # attempt 2 not due yet (1 minute backoff)
    with be.sessions() as s:
        assert s.get(ArchiveSnapshot, snap_id).attempts == 1  # type: ignore[union-attr]
    with session_scope(be.sessions) as s:
        snap = s.get(ArchiveSnapshot, snap_id)
        assert snap is not None
        snap.submitted_at = "2020-01-01T00:00:00+00:00"
        snap.attempts = MAX_ATTEMPTS - 1
    run_archive_poll(be)
    with be.sessions() as s:
        snap = s.get(ArchiveSnapshot, snap_id)
        assert snap is not None and snap.status == "failed" and snap.attempts == MAX_ATTEMPTS
