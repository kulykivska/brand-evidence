"""Routine chain checks cost what is new, not what is stored.

The digest ran daily and re-hashed the whole log each time. A checkpoint records
how far the chain was verified in full; the daily paths continue from there, and
`verify` and `export` still walk every entry.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from brand_evidence.app import App
from brand_evidence.core import evidence_log
from brand_evidence.core.db import session_scope
from brand_evidence.core.models import ChainCheckpoint


def append_many(be: App, count: int, prefix: str = "test") -> None:
    with session_scope(be.sessions) as session:
        for n in range(count):
            evidence_log.append(session, f"{prefix}.event", {"n": n})


def count_hashed(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls = [0]
    original = evidence_log.compute_entry_hash

    def counting(*args: object, **kwargs: object) -> str:
        calls[0] += 1
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(evidence_log, "compute_entry_hash", counting)
    return calls


def test_a_second_check_only_hashes_what_was_appended_since(
    be: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    append_many(be, 20)
    with session_scope(be.sessions) as session:
        assert evidence_log.verify_since_checkpoint(session).ok
    append_many(be, 3, prefix="later")

    calls = count_hashed(monkeypatch)
    with session_scope(be.sessions) as session:
        result = evidence_log.verify_since_checkpoint(session)
    assert result.ok
    # Three appended since the checkpoint, plus the anchor the checkpoint names;
    # the other nineteen are not re-hashed.
    assert calls[0] == 4
    assert result.entries == 23
    assert result.checked_from is not None
    assert "earlier verified in full" in result.describe()


def test_a_break_after_the_checkpoint_is_caught(be: App) -> None:
    append_many(be, 5)
    with session_scope(be.sessions) as session:
        assert evidence_log.verify_since_checkpoint(session).ok
    append_many(be, 2, prefix="later")
    with be.engine.begin() as conn:
        conn.execute(text("DROP TRIGGER evidence_log_no_update"))
        conn.exec_driver_sql(
            "UPDATE evidence_log SET payload='{\"n\":999}' "
            "WHERE seq=(SELECT max(seq) FROM evidence_log)"
        )
    with session_scope(be.sessions) as session:
        result = evidence_log.verify_since_checkpoint(session, record=False)
    assert not result.ok
    assert "entry_hash does not match content" in result.describe()


def test_a_break_below_the_checkpoint_is_caught_by_the_full_walk(be: App) -> None:
    """The tradeoff, stated as a test: the daily path re-checks the entry the
    checkpoint names and the length below it, but not every entry's content, so
    an older one altered underneath it is caught by `verify` and `export`."""
    append_many(be, 5)
    with session_scope(be.sessions) as session:
        assert evidence_log.verify_since_checkpoint(session).ok
    with be.engine.begin() as conn:
        conn.execute(text("DROP TRIGGER evidence_log_no_update"))
        conn.exec_driver_sql("UPDATE evidence_log SET payload='{\"n\":999}' WHERE seq=2")
    with session_scope(be.sessions) as session:
        assert evidence_log.verify_since_checkpoint(session, record=False).ok
        full = evidence_log.verify(session)
    assert not full.ok
    assert full.first_bad_seq == 2


def test_an_empty_log_needs_no_checkpoint(be: App) -> None:
    with session_scope(be.sessions) as session:
        result = evidence_log.verify_since_checkpoint(session)
        assert result.ok and result.entries == 0
        assert evidence_log.latest_checkpoint(session) is None


def test_a_truncated_chain_does_not_verify_clean(be: App) -> None:
    """The one tampering this table exists to catch: entries removed from the
    head, which leaves nothing above the checkpoint to walk."""
    append_many(be, 10)
    with session_scope(be.sessions) as session:
        assert evidence_log.verify_since_checkpoint(session).ok
    with be.engine.begin() as conn:
        conn.execute(text("DROP TRIGGER evidence_log_no_delete"))
        conn.exec_driver_sql("DELETE FROM evidence_log WHERE seq > 7")
    with session_scope(be.sessions) as session:
        result = evidence_log.verify_since_checkpoint(session, record=False)
    assert not result.ok
    # The entry the checkpoint names went with the truncation.
    assert "checkpoint does not match its entry" in result.describe()


def test_entries_deleted_below_the_checkpoint_are_noticed(be: App) -> None:
    append_many(be, 10)
    with session_scope(be.sessions) as session:
        assert evidence_log.verify_since_checkpoint(session).ok
    append_many(be, 2, prefix="later")
    with be.engine.begin() as conn:
        conn.execute(text("DROP TRIGGER evidence_log_no_delete"))
        conn.exec_driver_sql("DELETE FROM evidence_log WHERE seq = 4")
    with session_scope(be.sessions) as session:
        result = evidence_log.verify_since_checkpoint(session, record=False)
    assert not result.ok
    assert "below the checkpoint are gone" in result.describe()


def test_a_forged_checkpoint_is_not_believed(be: App) -> None:
    append_many(be, 5)
    with session_scope(be.sessions) as session:
        head = evidence_log.last_entry(session)
        assert head is not None
        session.add(
            ChainCheckpoint(
                id="forged",
                seq=head.seq + 50,
                entry_hash="f" * 64,
                verified_at="2026-09-17T00:00:00+00:00",
            )
        )
    with session_scope(be.sessions) as session:
        result = evidence_log.verify_since_checkpoint(session, record=False)
    assert not result.ok
    assert "checkpoint does not match its entry" in result.describe()


def test_checkpoints_cannot_be_rewritten(be: App) -> None:
    append_many(be, 3)
    with session_scope(be.sessions) as session:
        assert evidence_log.verify_since_checkpoint(session).ok
    with pytest.raises(IntegrityError, match="append-only"), be.engine.begin() as conn:
        conn.exec_driver_sql("UPDATE chain_checkpoints SET seq = 999")


def test_a_still_head_does_not_grow_the_table(be: App) -> None:
    append_many(be, 3)
    for _ in range(4):
        with session_scope(be.sessions) as session:
            assert evidence_log.verify_since_checkpoint(session).ok
    with be.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ChainCheckpoint)) == 1
