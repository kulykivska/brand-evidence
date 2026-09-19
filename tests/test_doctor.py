"""Every failure this tool can have is quiet, so the doctor has to be loud.

The checks that matter are the ones an operator cannot see any other way: a
schema one migration behind, the append-only triggers dropped, a job that has
not run since Tuesday, a chain that no longer verifies.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from brand_evidence import doctor
from brand_evidence.app import App
from brand_evidence.core import evidence_log
from brand_evidence.core.db import session_scope
from brand_evidence.core.models import Run
from brand_evidence.core.runs import tracked_run


def status_of(checks: list[doctor.Check], area: str) -> str:
    return next(c.status for c in checks if c.area == area)


def details_of(checks: list[doctor.Check], area: str) -> list[str]:
    return [c.detail for c in checks if c.area == area]


def test_a_fresh_install_is_usable_with_warnings(be: App) -> None:
    checks = doctor.run_checks(be)
    assert status_of(checks, "schema") == doctor.OK
    assert status_of(checks, "triggers") == doctor.OK
    assert status_of(checks, "evidence chain") == doctor.WARN
    # Warnings do not fail: nothing is broken, it is just empty.
    assert doctor.worst(checks) in {doctor.OK, doctor.WARN}


def test_dropped_triggers_are_a_failure(be: App) -> None:
    """Without them the append-only guarantee is a convention, and this is the
    only place anyone would notice."""
    with be.engine.begin() as conn:
        conn.execute(text("DROP TRIGGER evidence_log_no_update"))
    checks = doctor.run_checks(be)
    assert status_of(checks, "triggers") == doctor.FAIL
    assert "evidence_log_no_update" in details_of(checks, "triggers")[0]
    assert doctor.worst(checks) == doctor.FAIL


def test_a_broken_chain_is_a_failure(be: App) -> None:
    with session_scope(be.sessions) as session:
        evidence_log.append(session, "test.one", {"n": 1})
        evidence_log.append(session, "test.two", {"n": 2})
    with be.engine.begin() as conn:
        conn.execute(text("DROP TRIGGER evidence_log_no_update"))
        conn.exec_driver_sql("UPDATE evidence_log SET payload = '{\"n\":9}' WHERE seq = 1")
    checks = doctor.run_checks(be)
    assert status_of(checks, "evidence chain") == doctor.FAIL
    assert "BROKEN" in details_of(checks, "evidence chain")[0]


def test_a_job_that_has_not_run_since_yesterday_is_named(be: App) -> None:
    with session_scope(be.sessions) as session:
        session.add(
            Run(
                id="old",
                job="crawl",
                started_at="2026-09-01T07:00:00+00:00",
                finished_at="2026-09-01T07:01:00+00:00",
                status="ok",
                stats={},
            )
        )
    checks = doctor.run_checks(be)
    assert status_of(checks, "runs/crawl") == doctor.WARN
    assert "nothing since" in details_of(checks, "runs/crawl")[0]


def test_a_run_stuck_running_is_a_failure(be: App) -> None:
    """A job killed mid-flight leaves its row at `running` forever, and the
    digest reports it as if it were still working."""
    with session_scope(be.sessions) as session:
        session.add(
            Run(
                id="stuck",
                job="digest",
                started_at="2026-09-01T07:30:00+00:00",
                status="running",
                stats={},
            )
        )
    checks = doctor.run_checks(be)
    assert status_of(checks, "runs/digest") == doctor.FAIL
    assert "stuck" in details_of(checks, "runs/digest")[0]


def test_a_failed_run_is_a_failure(be: App) -> None:
    with pytest.raises(RuntimeError), tracked_run(be.sessions, "crawl"):
        raise RuntimeError("the source exploded")
    checks = doctor.run_checks(be)
    assert status_of(checks, "runs/crawl") == doctor.FAIL


def test_sources_without_credentials_are_named_not_hidden(be: App) -> None:
    """A source disabled for a missing key looks exactly like a source that
    found nothing, which is how a brand goes unmonitored for a month."""
    details = details_of(doctor.run_checks(be), "sources")
    assert any("skipped for missing credentials" in d for d in details)


def test_a_database_that_was_never_initialised_fails(settings, tmp_path: Path) -> None:
    from brand_evidence.app import build_app

    app = build_app(settings, create_schema=False)
    try:
        checks = doctor.run_checks(app)
        assert status_of(checks, "schema") == doctor.FAIL
        assert "init" in details_of(checks, "schema")[0]
    finally:
        app.engine.dispose()


def test_an_unwritable_store_fails(be: App, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "touch", refuse)
    checks = doctor.run_checks(be)
    assert status_of(checks, "store") == doctor.FAIL


def test_the_disk_line_says_what_an_export_would_need(be: App) -> None:
    detail = details_of(doctor.run_checks(be), "disk")[0]
    assert "free" in detail
    assert "An export needs about" in detail


@pytest.mark.skipif(sys.platform == "darwin", reason="launchd exists here")
def test_elsewhere_the_schedule_check_says_so(be: App) -> None:
    detail = details_of(doctor.run_checks(be), "schedule")[0]
    assert "macOS only" in detail
