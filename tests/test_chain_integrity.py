"""The evidence chain stays linear even when two jobs run at once.

The scheduled jobs overlap by design: crawl at 07:00, digest at 07:30 and
archive-poll on a free-running two-hour interval. Before this, two processes
could each read the chain head and each append after it, and the append-only
triggers then made the fork permanent: every later verify, digest and export
reported a broken chain, forever, with no way to repair it.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from brand_evidence.app import App
from brand_evidence.core import evidence_log
from brand_evidence.core.db import make_engine, make_session_factory, session_scope
from brand_evidence.core.models import EvidenceEntry


def test_two_entries_cannot_claim_the_same_predecessor(be: App) -> None:
    """The structural backstop: even a direct insert cannot fork the chain."""
    with session_scope(be.sessions) as session:
        evidence_log.append(session, "test.one", {"n": 1})
    session = be.sessions()
    try:
        head = evidence_log.last_entry(session)
        assert head is not None
        forged = EvidenceEntry(
            occurred_at="2026-09-17T00:00:00+00:00",
            event_type="test.fork",
            payload={"n": 2},
            prev_hash=head.prev_hash,
            entry_hash="f" * 64,
        )
        session.add(forged)
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
    finally:
        session.close()


def test_concurrent_appends_produce_one_linear_chain(be: App) -> None:
    """Two writers on the same database, each with its own engine, as two
    processes would be."""
    db_path = Path(be.settings.db_path)
    errors: list[Exception] = []
    start = threading.Barrier(2)

    def append_one(marker: int) -> None:
        engine = make_engine(db_path)
        sessions = make_session_factory(engine)
        try:
            start.wait(timeout=5)
            with session_scope(sessions) as session:
                evidence_log.append(session, "test.concurrent", {"marker": marker})
        except Exception as exc:  # noqa: BLE001 - reported through `errors`
            errors.append(exc)
        finally:
            engine.dispose()

    threads = [threading.Thread(target=append_one, args=(i,)) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    with session_scope(be.sessions) as session:
        entries = session.query(EvidenceEntry).order_by(EvidenceEntry.seq).all()
        assert len(entries) == 2
        assert entries[1].prev_hash == entries[0].entry_hash
        assert evidence_log.verify(session).ok
