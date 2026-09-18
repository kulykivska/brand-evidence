"""Append-only, hash-chained evidence log.

entry_hash = sha256(prev_hash + occurred_at + event_type + canonical_json(payload)).
The first entry's prev_hash is GENESIS. Rows are never updated or deleted (see db.py triggers).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from brand_evidence.core.clock import now_iso
from brand_evidence.core.ids import uuid7
from brand_evidence.core.models import Base, ChainCheckpoint, EvidenceEntry

GENESIS = "0" * 64


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_entry_hash(
    prev_hash: str, occurred_at: str, event_type: str, payload: dict[str, Any]
) -> str:
    material = prev_hash + occurred_at + event_type + canonical_json(payload)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def last_entry(session: Session) -> EvidenceEntry | None:
    return session.scalars(
        select(EvidenceEntry).order_by(EvidenceEntry.seq.desc()).limit(1)
    ).first()


def append(session: Session, event_type: str, payload: dict[str, Any]) -> EvidenceEntry:
    """Append one entry within the caller's transaction. The caller commits."""
    previous = last_entry(session)
    prev_hash = previous.entry_hash if previous else GENESIS
    occurred_at = now_iso()
    entry = EvidenceEntry(
        occurred_at=occurred_at,
        event_type=event_type,
        payload=payload,
        prev_hash=prev_hash,
        entry_hash=compute_entry_hash(prev_hash, occurred_at, event_type, payload),
    )
    session.add(entry)
    session.flush()
    return entry


def record(session: Session, event_type: str, row: Base) -> EvidenceEntry:
    """Flush the row so defaults are populated, then log its full content."""
    session.add(row)
    session.flush()
    return append(session, event_type, row.as_payload())


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    entries: int
    first_bad_seq: int | None = None
    reason: str | None = None
    # Set when only part of the chain was walked: the seq the walk started at
    # and when everything below it was last checked in full.
    checked_from: int | None = None
    checkpoint_at: str | None = None

    def describe(self) -> str:
        if not self.ok:
            return f"chain BROKEN at seq {self.first_bad_seq}: {self.reason}"
        if self.checked_from is None:
            return f"chain valid: {self.entries} entries"
        return (
            f"chain valid: {self.entries} entries "
            f"(seq {self.checked_from} onwards; earlier verified in full at {self.checkpoint_at})"
        )


def verify(session: Session) -> VerifyResult:
    """Walk the whole chain and report the first break."""
    return _walk(session, GENESIS, None, 0)


def latest_checkpoint(session: Session) -> ChainCheckpoint | None:
    return session.scalars(
        select(ChainCheckpoint).order_by(ChainCheckpoint.seq.desc()).limit(1)
    ).first()


def verify_since_checkpoint(session: Session, *, record: bool = True) -> VerifyResult:
    """Check the entries appended since the last full verification.

    The daily digest and report used to re-hash every entry ever written, which
    grows without bound while telling them nothing new. What is below the
    checkpoint was verified in full when the checkpoint was written; `verify`
    and `export` still walk the whole chain, so tampering with an old entry is
    caught there.
    """
    checkpoint = latest_checkpoint(session)
    if checkpoint is None:
        result = verify(session)
    else:
        result = _anchored(session, checkpoint) or _walk(
            session,
            checkpoint.entry_hash,
            checkpoint.seq + 1,
            _count_to(session, checkpoint.seq),
            checked_from=checkpoint.seq + 1,
            checkpoint_at=checkpoint.verified_at,
        )
    if result.ok and record:
        write_checkpoint(session)
    return result


def _count_to(session: Session, seq: int) -> int:
    return (
        session.scalar(
            select(func.count()).select_from(EvidenceEntry).where(EvidenceEntry.seq <= seq)
        )
        or 0
    )


def _anchored(session: Session, checkpoint: ChainCheckpoint) -> VerifyResult | None:
    """Check the checkpoint against the log before trusting it. None means it holds.

    Without this the routine check starts above the checkpoint and so cannot see
    that the entries it skipped were deleted, or that the head was cut back to
    below it: a truncated chain reported as valid, which is the one tampering
    this table is placed to catch.
    """
    anchor = session.get(EvidenceEntry, checkpoint.seq)
    below = _count_to(session, checkpoint.seq)
    if anchor is None or anchor.entry_hash != checkpoint.entry_hash:
        return VerifyResult(False, below, checkpoint.seq, "checkpoint does not match its entry")
    recomputed = compute_entry_hash(
        anchor.prev_hash, anchor.occurred_at, anchor.event_type, anchor.payload
    )
    if recomputed != anchor.entry_hash:
        return VerifyResult(False, below, checkpoint.seq, "entry_hash does not match content")
    first = session.scalar(select(func.min(EvidenceEntry.seq)))
    if first is None or below != checkpoint.seq - first + 1:
        return VerifyResult(False, below, checkpoint.seq, "entries below the checkpoint are gone")
    head = last_entry(session)
    if head is None or head.seq < checkpoint.seq:
        return VerifyResult(
            False, below, checkpoint.seq, "the chain is shorter than the checkpoint"
        )
    return None


def write_checkpoint(session: Session) -> ChainCheckpoint | None:
    """Mark the current head as verified. Callers hold a write transaction.

    One row per advance, not one per check: the digest runs daily and the head
    often has not moved.
    """
    head = last_entry(session)
    if head is None:
        return None
    current = latest_checkpoint(session)
    if current is not None and current.seq >= head.seq:
        return current
    row = ChainCheckpoint(
        id=uuid7(), seq=head.seq, entry_hash=head.entry_hash, verified_at=now_iso()
    )
    session.add(row)
    session.flush()
    return row


def _walk(
    session: Session,
    expected_prev: str,
    expected_seq: int | None,
    count: int,
    *,
    checked_from: int | None = None,
    checkpoint_at: str | None = None,
) -> VerifyResult:
    query = select(EvidenceEntry).order_by(EvidenceEntry.seq.asc())
    if checked_from is not None:
        query = query.where(EvidenceEntry.seq >= checked_from)
    for entry in session.scalars(query.execution_options(yield_per=500)):
        count += 1
        if expected_seq is None:
            expected_seq = entry.seq
        if entry.seq != expected_seq:
            return VerifyResult(False, count, entry.seq, f"gap: expected seq {expected_seq}")
        if entry.prev_hash != expected_prev:
            return VerifyResult(False, count, entry.seq, "prev_hash does not match previous entry")
        recomputed = compute_entry_hash(
            entry.prev_hash, entry.occurred_at, entry.event_type, entry.payload
        )
        if recomputed != entry.entry_hash:
            return VerifyResult(False, count, entry.seq, "entry_hash does not match content")
        expected_prev = entry.entry_hash
        expected_seq = entry.seq + 1
    return VerifyResult(True, count, checked_from=checked_from, checkpoint_at=checkpoint_at)
