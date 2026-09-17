"""Engine and session factory. SQLite in WAL mode; evidence_log is write-protected by triggers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

# Defence in depth: even a direct SQL session cannot rewrite history without dropping these.
APPEND_ONLY_TRIGGERS = (
    """CREATE TRIGGER IF NOT EXISTS evidence_log_no_update
       BEFORE UPDATE ON evidence_log
       BEGIN SELECT RAISE(ABORT, 'evidence_log is append-only'); END;""",
    """CREATE TRIGGER IF NOT EXISTS evidence_log_no_delete
       BEFORE DELETE ON evidence_log
       BEGIN SELECT RAISE(ABORT, 'evidence_log is append-only'); END;""",
)


def make_engine(db_path: Path) -> Engine:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}", future=True)

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=FULL")
        cursor.close()

    return engine


def schema_present(engine: Engine) -> bool:
    return inspect(engine).has_table("evidence_log")


def install_triggers(engine: Engine) -> None:
    if not schema_present(engine):
        return
    with engine.begin() as conn:
        for statement in APPEND_ONLY_TRIGGERS:
            conn.execute(text(statement))


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
