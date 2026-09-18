"""Run bookkeeping: a row is written before any work starts, so a crash leaves a trace."""

from __future__ import annotations

import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from brand_evidence.core.clock import now_iso
from brand_evidence.core.db import session_scope
from brand_evidence.core.ids import uuid7
from brand_evidence.core.models import Run


class RunContext:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.stats: dict[str, Any] = {}
        self.partial = False
        self.errors: list[str] = []

    def mark_partial(self, message: str) -> None:
        self.partial = True
        self.errors.append(message)


@contextmanager
def tracked_run(factory: sessionmaker[Session], job: str) -> Iterator[RunContext]:
    run_id = uuid7()
    # session_scope, not a bare session: a plain session begins DEFERRED, and a
    # read-then-write there fails instantly with SQLITE_BUSY_SNAPSHOT, which no
    # busy_timeout retries. Run bookkeeping must not be the thing that breaks.
    with session_scope(factory) as session:
        session.add(Run(id=run_id, job=job, started_at=now_iso(), status="running"))
    ctx = RunContext(run_id)
    try:
        yield ctx
    except Exception as exc:
        _finish(factory, ctx, "failed", f"{exc}\n{traceback.format_exc()}")
        raise
    else:
        status = "partial" if ctx.partial else "ok"
        _finish(factory, ctx, status, "\n".join(ctx.errors) or None)


def _finish(
    factory: sessionmaker[Session], ctx: RunContext, status: str, error: str | None
) -> None:
    with session_scope(factory) as session:
        run = session.get(Run, ctx.run_id)
        if run is None:
            return
        run.finished_at = now_iso()
        run.status = status
        run.stats = ctx.stats
        run.error = error
