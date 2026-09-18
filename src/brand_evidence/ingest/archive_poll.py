"""Confirms pending archive snapshots with exponential backoff; gives up after MAX_ATTEMPTS."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from brand_evidence.app import App
from brand_evidence.capture.archiver import MAX_ATTEMPTS, RateLimiter, next_attempt_due
from brand_evidence.core import evidence_log
from brand_evidence.core.clock import now_iso
from brand_evidence.core.db import session_scope
from brand_evidence.core.logging import get_logger
from brand_evidence.core.models import ArchiveSnapshot
from brand_evidence.core.runs import tracked_run

log = get_logger(__name__)


def _target_url(snapshot: ArchiveSnapshot) -> str:
    marker = "/save/"
    if marker in snapshot.request_url:
        return snapshot.request_url.split(marker, 1)[1]
    return snapshot.request_url


@dataclass(frozen=True)
class _Due:
    """One snapshot's work, read once so the network call needs no session."""

    id: str
    target: str
    submitted_at: str
    attempts: int
    provider_ref: str | None


def _due_snapshots(app: App) -> tuple[list[_Due], int, int]:
    """Read the work list in one short transaction."""
    now = now_iso()
    due: list[_Due] = []
    deferred = 0
    with app.sessions() as session:
        pending = session.scalars(
            select(ArchiveSnapshot).where(ArchiveSnapshot.status == "pending")
        ).all()
        for snap in pending:
            if next_attempt_due(snap.submitted_at, snap.attempts) > now:
                deferred += 1
                continue
            due.append(
                _Due(
                    snap.id,
                    _target_url(snap),
                    snap.submitted_at,
                    snap.attempts,
                    snap.provider_ref,
                )
            )
    return due, len(pending), deferred


def run_archive_poll(app: App) -> str:
    """Poll pending snapshots. The write lock is held only to read the work list
    and to record each answer, never across a network call: an archive taking a
    minute per URL used to lock out every reader for the length of the run."""
    with tracked_run(app.sessions, "archive_poll") as ctx:
        limiter = RateLimiter(app.settings.archive_min_interval_seconds)
        confirmed = failed = skipped = 0
        due, pending_seen, deferred = _due_snapshots(app)
        for item in due:
            limiter.wait()
            provider_ref = item.provider_ref
            try:
                # submitted_at, not now: the provider uses it as the floor for
                # which captures count, and "now" excludes every real snapshot.
                url = app.archive.check(item.target, item.submitted_at, provider_ref)
                if url is None and item.attempts + 1 < MAX_ATTEMPTS and not provider_ref:
                    # No job to wait on: ask the archive again and keep the new job id.
                    resubmitted = app.archive.submit(item.target)
                    provider_ref = resubmitted.provider_ref
                    url = resubmitted.snapshot_url
            except Exception as exc:  # noqa: BLE001
                ctx.mark_partial(f"archive check failed for {item.id}: {exc}")
                url = None
            outcome = _record_attempt(app, item.id, provider_ref, url)
            if outcome == "confirmed":
                confirmed += 1
            elif outcome == "failed":
                failed += 1
            elif outcome == "skipped":
                skipped += 1
        ctx.stats.update(
            {
                "pending_seen": pending_seen,
                "confirmed": confirmed,
                "failed": failed,
                "deferred": deferred,
                # Answered by another run while this one was on the network.
                "skipped": skipped,
            }
        )
        return ctx.run_id


def _record_attempt(
    app: App, snapshot_id: str, provider_ref: str | None, url: str | None
) -> str:
    """Write one answer in its own transaction. Returns the new status."""
    with session_scope(app.sessions) as session:
        snap = session.get(ArchiveSnapshot, snapshot_id)
        if snap is None or snap.status != "pending":
            log.info("archive_snapshot_already_answered", snapshot_id=snapshot_id)
            return "skipped"
        # Incremented here, not from the value read before the network call:
        # two overlapping polls both writing an absolute count lose an attempt.
        snap.attempts += 1
        attempts = snap.attempts
        snap.provider_ref = provider_ref
        if url:
            snap.snapshot_url = url
            snap.status = "confirmed"
            snap.confirmed_at = now_iso()
            evidence_log.append(session, "archive_snapshot.confirmed", snap.as_payload())
            return "confirmed"
        if attempts >= MAX_ATTEMPTS:
            snap.status = "failed"
            evidence_log.append(session, "archive_snapshot.failed", snap.as_payload())
            return "failed"
        evidence_log.append(
            session,
            "archive_snapshot.attempted",
            {"id": snap.id, "attempts": attempts},
        )
        return "pending"
