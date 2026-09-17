"""Shared capture -> store -> archive -> evidence steps used by both ingestion paths."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from brand_evidence.app import App
from brand_evidence.capture.archiver import RateLimiter
from brand_evidence.capture.screenshot import Capturer, CaptureResult
from brand_evidence.core import evidence_log
from brand_evidence.core.clock import now_iso
from brand_evidence.core.ids import uuid7
from brand_evidence.core.logging import get_logger
from brand_evidence.core.models import ArchiveSnapshot, Artifact

log = get_logger(__name__)

KINDS = (("screenshot_png", "png"), ("pdf", "pdf"), ("html", "html"))


def store_artifacts(
    app: App, session: Session, subject_type: str, subject_id: str, result: CaptureResult
) -> list[Artifact]:
    rows: list[Artifact] = []
    for kind, attr in KINDS:
        data: bytes = getattr(result, attr)
        blob = app.store.put(data)
        row = Artifact(
            id=uuid7(),
            subject_type=subject_type,
            subject_id=subject_id,
            kind=kind,
            sha256=blob.sha256,
            bytes=blob.size,
            captured_at=result.captured_at,
            capture_meta={**result.meta, "source_url": result.url},
        )
        evidence_log.record(session, "artifact.created", row)
        rows.append(row)
    return rows


def submit_archive(
    app: App,
    session: Session,
    subject_type: str,
    subject_id: str,
    url: str,
    limiter: RateLimiter | None = None,
) -> ArchiveSnapshot:
    """Record the request as pending first, then submit. A network failure stays pending."""
    if limiter:
        limiter.wait()
    row = ArchiveSnapshot(
        id=uuid7(),
        subject_type=subject_type,
        subject_id=subject_id,
        provider=app.archive.name,
        request_url="",
        status="pending",
        submitted_at=now_iso(),
        attempts=0,
    )
    try:
        submitted = app.archive.submit(url)
        row.request_url = submitted.request_url
        row.provider_ref = submitted.provider_ref
        if submitted.snapshot_url:
            row.snapshot_url = submitted.snapshot_url
            row.status = "confirmed"
            row.confirmed_at = now_iso()
    except Exception as exc:  # noqa: BLE001 - recorded, never hidden
        log.warning("archive_submit_failed", url=url, error=str(exc))
        row.request_url = url
    evidence_log.record(session, "archive_snapshot.submitted", row)
    return row


def make_capturer(app: App) -> Capturer:
    s = app.settings
    return Capturer(s.viewport_width, s.viewport_height, s.device_scale_factor)


def meta_summary(rows: list[Artifact]) -> dict[str, Any]:
    return {r.kind: r.sha256 for r in rows}


def record_history(app: App, session: Session, subject_type: str, subject_id: str, url: str) -> int:
    """Store snapshots the archive already held before we asked. Returns how many were new."""
    from sqlalchemy import select

    try:
        found = app.archive.history(url)
    except Exception as exc:  # noqa: BLE001 - recorded, never hidden
        log.warning("archive_history_failed", url=url, error=str(exc))
        return -1
    known = set(
        session.scalars(
            select(ArchiveSnapshot.snapshot_url).where(ArchiveSnapshot.subject_id == subject_id)
        )
    )
    added = 0
    for snap in found:
        if snap.snapshot_url in known:
            continue
        row = ArchiveSnapshot(
            id=uuid7(),
            subject_type=subject_type,
            subject_id=subject_id,
            provider=app.archive.name,
            request_url=url,
            provider_ref=snap.provider_ref,
            snapshot_url=snap.snapshot_url,
            status="historical",
            submitted_at=now_iso(),
            confirmed_at=snap.captured_at,
            attempts=0,
        )
        evidence_log.record(session, "archive_snapshot.historical", row)
        added += 1
    return added


ATTACHABLE_KINDS = ("platform_export", "email", "document", "image", "other")


def attach_file(
    app: App,
    session: Session,
    subject_type: str,
    subject_id: str,
    kind: str,
    data: bytes,
    *,
    filename: str,
    note: str | None = None,
) -> Artifact:
    """Store an externally obtained file (platform export, email, PDF) as an artifact."""
    if kind not in ATTACHABLE_KINDS:
        raise ValueError(f"kind must be one of {ATTACHABLE_KINDS}")
    blob = app.store.put(data)
    row = Artifact(
        id=uuid7(),
        subject_type=subject_type,
        subject_id=subject_id,
        kind=kind,
        sha256=blob.sha256,
        bytes=blob.size,
        captured_at=now_iso(),
        capture_meta={"origin": "attached", "filename": filename, "note": note},
    )
    evidence_log.record(session, "artifact.attached", row)
    return row


def timestamp_chain_head(app: App, session: Session) -> Artifact | None:
    """Anchor the current chain head with a trusted timestamp. Returns None when disabled."""
    if app.tsa is None:
        return None
    head = evidence_log.last_entry(session)
    if head is None:
        return None
    material = f"{head.seq}:{head.entry_hash}".encode()
    token = app.tsa.stamp(material)
    blob = app.store.put(token.tsr)
    row = Artifact(
        id=uuid7(),
        subject_type="project",
        subject_id="project",
        kind="timestamp",
        sha256=blob.sha256,
        bytes=blob.size,
        captured_at=token.gen_time,
        capture_meta={
            "origin": "tsa",
            "tsa_url": token.tsa_url,
            "policy": token.policy,
            "serial": token.serial,
            "stamped_seq": head.seq,
            "stamped_entry_hash": head.entry_hash,
            "stamped_material": material.decode(),
            "digest_sha256": token.digest_hex,
        },
    )
    evidence_log.record(session, "chain.timestamped", row)
    return row
