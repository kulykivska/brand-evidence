"""Append-only, hash-chained evidence log.

entry_hash = sha256(prev_hash + occurred_at + event_type + canonical_json(payload)).
The first entry's prev_hash is GENESIS. Rows are never updated or deleted (see db.py triggers).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from brand_evidence.core.clock import now_iso
from brand_evidence.core.models import Base, EvidenceEntry

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

    def describe(self) -> str:
        if self.ok:
            return f"chain valid: {self.entries} entries"
        return f"chain BROKEN at seq {self.first_bad_seq}: {self.reason}"


def verify(session: Session) -> VerifyResult:
    """Walk the whole chain and report the first break."""
    expected_prev = GENESIS
    expected_seq = None
    count = 0
    for entry in session.scalars(select(EvidenceEntry).order_by(EvidenceEntry.seq.asc())):
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
    return VerifyResult(True, count)
