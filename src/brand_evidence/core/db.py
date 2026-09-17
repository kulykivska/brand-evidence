"""Engine and session factory.

SQLite in WAL mode. Two things keep the evidence log linear under the
scheduled jobs, which do overlap: every transaction starts as BEGIN
IMMEDIATE, so a writer takes the write lock before it reads the chain head
rather than after, and a unique index on prev_hash makes a fork
unrepresentable even if something else appends. Rows are also write-
protected by triggers."""

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


# How long a writer waits for another writer. A capture run holds its
# transaction for as long as the network takes, so this is generous.
BUSY_TIMEOUT_MS = 30_000


def make_engine(db_path: Path) -> Engine:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}", future=True)

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _record) -> None:  # type: ignore[no-untyped-def]
        # pysqlite begins a transaction only at the first write, which is the
        # gap that let two processes read the same chain head.
        dbapi_conn.isolation_level = None
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=FULL")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn) -> None:  # type: ignore[no-untyped-def]
        # IMMEDIATE takes the write lock before the head is read. Readers pay
        # for it too: in WAL they no longer run alongside a writer (see #6).
        conn.exec_driver_sql("BEGIN IMMEDIATE")

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
