from __future__ import annotations

import pytest
from sqlalchemy import text

from brand_evidence.app import App
from brand_evidence.core import evidence_log
from brand_evidence.core.db import session_scope
from brand_evidence.core.evidence_log import GENESIS, canonical_json, compute_entry_hash


def test_canonical_json_is_order_independent() -> None:
    assert canonical_json({"b": 1, "a": [1, 2]}) == canonical_json({"a": [1, 2], "b": 1})
    assert canonical_json({"a": "é"}) == '{"a":"é"}'


def test_hash_formula_matches_spec() -> None:
    import hashlib

    payload = {"x": 1}
    expected = hashlib.sha256(
        (GENESIS + "2026-01-01T00:00:00+00:00" + "t" + '{"x":1}').encode()
    ).hexdigest()
    assert compute_entry_hash(GENESIS, "2026-01-01T00:00:00+00:00", "t", payload) == expected


def test_chain_links_and_verifies(be: App) -> None:
    with session_scope(be.sessions) as s:
        first = evidence_log.append(s, "a", {"n": 1})
        second = evidence_log.append(s, "b", {"n": 2})
        assert first.prev_hash == GENESIS
        assert second.prev_hash == first.entry_hash
    with be.sessions() as s:
        result = evidence_log.verify(s)
    assert result.ok and result.entries == 2


def test_empty_chain_is_valid(be: App) -> None:
    with be.sessions() as s:
        assert evidence_log.verify(s).ok


def test_triggers_refuse_update_and_delete(be: App) -> None:
    with session_scope(be.sessions) as s:
        evidence_log.append(s, "a", {"n": 1})
    with be.engine.begin() as conn:
        with pytest.raises(Exception, match="append-only"):
            conn.execute(text("UPDATE evidence_log SET event_type='x' WHERE seq=1"))
    with be.engine.begin() as conn:
        with pytest.raises(Exception, match="append-only"):
            conn.execute(text("DELETE FROM evidence_log WHERE seq=1"))


def test_verify_reports_exact_seq_after_tampering(be: App) -> None:
    with session_scope(be.sessions) as s:
        for i in range(5):
            evidence_log.append(s, "row", {"n": i})
    # Simulate an attacker who drops the guard triggers and edits a middle row.
    with be.engine.begin() as conn:
        conn.execute(text("DROP TRIGGER evidence_log_no_update"))
        conn.exec_driver_sql("UPDATE evidence_log SET payload='{\"n\":99}' WHERE seq=3")
    with be.sessions() as s:
        result = evidence_log.verify(s)
    assert not result.ok
    assert result.first_bad_seq == 3
    assert "entry_hash" in (result.reason or "")


def test_verify_detects_deleted_row(be: App) -> None:
    with session_scope(be.sessions) as s:
        for i in range(4):
            evidence_log.append(s, "row", {"n": i})
    with be.engine.begin() as conn:
        conn.execute(text("DROP TRIGGER evidence_log_no_delete"))
        conn.execute(text("DELETE FROM evidence_log WHERE seq=2"))
    with be.sessions() as s:
        result = evidence_log.verify(s)
    assert not result.ok and result.first_bad_seq == 3
