"""Engine and session factory.

SQLite in WAL mode. Two things keep the evidence log linear under the
scheduled jobs, which do overlap: a writing transaction (session_scope)
starts as BEGIN IMMEDIATE, so it takes the write lock before it reads the
chain head rather than after, and a unique index on prev_hash makes a fork
unrepresentable even if something else appends. Read-only sessions stay
DEFERRED and keep WAL's concurrency. Rows are also write-protected by
triggers."""

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
    # Checkpoints say how much of the chain has been verified, so a forged one
    # silences the daily check. They get the same protection as the log.
    """CREATE TRIGGER IF NOT EXISTS chain_checkpoints_no_update
       BEFORE UPDATE ON chain_checkpoints
       BEGIN SELECT RAISE(ABORT, 'chain_checkpoints is append-only'); END;""",
    """CREATE TRIGGER IF NOT EXISTS chain_checkpoints_no_delete
       BEFORE DELETE ON chain_checkpoints
       BEGIN SELECT RAISE(ABORT, 'chain_checkpoints is append-only'); END;""",
)


# How long a writer waits for another writer. A capture run holds its
# transaction for as long as the network takes, so this is generous.
BUSY_TIMEOUT_MS = 30_000
# Execution option naming the SQLite transaction mode for one transaction.
TXN_OPTION = "brand_evidence_txn"


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
        # Writers ask for IMMEDIATE so the lock is taken before they read the
        # chain head; readers stay DEFERRED and keep WAL's concurrency.
        mode = conn.get_execution_options().get(TXN_OPTION, "DEFERRED")
        conn.exec_driver_sql(f"BEGIN {mode}")

    return engine


def schema_present(engine: Engine) -> bool:
    return inspect(engine).has_table("evidence_log")


def install_triggers(engine: Engine) -> None:
    if not schema_present(engine) or not inspect(engine).has_table("chain_checkpoints"):
        return
    with engine.begin() as conn:
        for statement in APPEND_ONLY_TRIGGERS:
            conn.execute(text(statement))


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """A writing transaction: it commits on exit, and it takes SQLite's write
    lock up front so reading the evidence chain head and appending after it
    cannot interleave with another writer."""
    session = factory()
    try:
        session.connection(execution_options={TXN_OPTION: "IMMEDIATE"})
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
