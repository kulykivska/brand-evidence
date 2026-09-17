from __future__ import annotations

from pathlib import Path

import httpx
import respx
from sqlalchemy import select

from brand_evidence.app import App
from brand_evidence.capture.archiver import HistoricalSnapshot, WaybackProvider
from brand_evidence.core import evidence_log
from brand_evidence.core.db import session_scope
from brand_evidence.core.models import ArchiveSnapshot, Artifact
from brand_evidence.ingest.backfill import run_backfill
from brand_evidence.ingest.capture_pipeline import attach_file, record_history
from brand_evidence.ingest.hook import ingest_post
from tests.factories import FakeArchive, FakeCapturer

OLD = HistoricalSnapshot(
    snapshot_url="https://web.archive.org/web/20260301120000/https://threads.net/p/old",
    captured_at="2026-03-01T12:00:00.000000+00:00",
    provider_ref="20260301120000",
)


@respx.mock
def test_cdx_history_parses_rows_oldest_first() -> None:
    respx.get("https://web.archive.org/cdx/search/cdx").mock(
        return_value=httpx.Response(
            200,
            json=[
                ["timestamp", "original", "statuscode"],
                ["20260301120000", "https://threads.net/p/old", "200"],
                ["20260615080000", "https://threads.net/p/old", "200"],
            ],
        )
    )
    found = WaybackProvider("ak", "sk", httpx.Client()).history("https://threads.net/p/old")
    assert [h.provider_ref for h in found] == ["20260301120000", "20260615080000"]
    assert found[0].captured_at.startswith("2026-03-01T12:00:00")
    assert found[0].snapshot_url == OLD.snapshot_url


def test_backfill_records_prior_snapshots_with_archive_dates(be: App, tmp_path: Path) -> None:
    be.archive = FakeArchive()
    be.archive.historical = [OLD]  # type: ignore[attr-defined]
    csv = tmp_path / "posts.csv"
    csv.write_text(
        "platform,url,body,published_at\n"
        "threads,https://threads.net/p/old,Northwind launch,2026-02-28T10:00:00Z\n"
    )
    counts = run_backfill(be, csv, capture=False, history=True)
    assert counts["imported"] == 1 and counts["historical_snapshots"] == 1
    with be.sessions() as s:
        snap = s.scalars(
            select(ArchiveSnapshot).where(ArchiveSnapshot.status == "historical")
        ).one()
        assert snap.snapshot_url == OLD.snapshot_url
        assert snap.confirmed_at == OLD.captured_at  # the archive's date, not ours
        assert snap.submitted_at > "2026-09"  # when we looked it up
        assert evidence_log.verify(s).ok
    # Looking again adds nothing.
    with session_scope(be.sessions) as s:
        post_id = snap.subject_id
        assert record_history(be, s, "post", post_id, "https://threads.net/p/old") == 0


def test_history_lookup_failure_is_reported_not_raised(be: App) -> None:
    be.archive = FakeArchive(fail=True)
    with session_scope(be.sessions) as s:
        assert record_history(be, s, "post", "x", "https://example.com") == -1


def test_attach_platform_export_and_email(be: App) -> None:
    be.archive = FakeArchive()
    result = ingest_post(
        be,
        platform="linkedin",
        url="https://linkedin.com/posts/1",
        body="Northwind",
        published_at="2026-03-01T00:00:00Z",
        capturer=FakeCapturer(),
    )
    with session_scope(be.sessions) as s:
        export = attach_file(
            be,
            s,
            "project",
            "project",
            "platform_export",
            b"PK export",
            filename="linkedin.zip",
            note="requested 2026-09-15",
        )
        mail = attach_file(
            be, s, "post", result.post_id, "email", b"From: linkedin", filename="n.eml"
        )
    with be.sessions() as s:
        rows = {a.kind: a for a in s.scalars(select(Artifact)).all()}
        assert rows["platform_export"].subject_type == "project"
        assert rows["platform_export"].capture_meta["note"] == "requested 2026-09-15"
        assert rows["email"].subject_id == result.post_id
        events = [e.event_type for e in s.scalars(select(evidence_log.EvidenceEntry))]
        assert events.count("artifact.attached") == 2
        assert evidence_log.verify(s).ok
    assert be.store.verify(export.sha256) and be.store.verify(mail.sha256)
