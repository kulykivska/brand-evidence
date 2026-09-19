"""Is this installation actually recording anything?

Every failure this tool can have is quiet. A launchd job that never loaded, a
disk with no room for the next capture, an expired search key, a schema one
migration behind - each shows up days later as a digest that never arrived, in
a tool whose whole value is that it was recording when it said it was.
"""

from __future__ import annotations

import logging
import plistlib
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select

from brand_evidence.app import App
from brand_evidence.core import evidence_log
from brand_evidence.core.db import APPEND_ONLY_TRIGGERS, schema_present
from brand_evidence.core.models import ArchiveSnapshot, EvidenceEntry, Mention, Run
from brand_evidence.schedule import AGENTS_DIR, JOBS, label_for
from brand_evidence.sources.registry import build_sources

OK = "ok"
WARN = "warn"
FAIL = "fail"

# A daily job that has not run since yesterday is not a daily job.
STALE_AFTER = timedelta(hours=36)
# Below this the next capture is the one that fails.
LOW_SPACE_BYTES = 2 * 1024**3


@dataclass(frozen=True)
class Check:
    area: str
    status: str
    detail: str

    def __str__(self) -> str:
        mark = {OK: "[ok]  ", WARN: "[warn]", FAIL: "[FAIL]"}[self.status]
        return f"{mark} {self.area}: {self.detail}"


def run_checks(app: App, *, project: str | None = None) -> list[Check]:
    """Every check, in the order an operator would want to read them."""
    checks: list[Check] = []
    checks.extend(_paths(app))
    checks.extend(_schema(app))
    if not schema_present(app.engine):
        # Everything below reads tables. Judging them would mean a page of
        # errors about one problem.
        checks.append(Check("rest", WARN, "not checked: there is no schema to read"))
        return checks
    checks.extend(_chain(app))
    checks.extend(_sources(app))
    checks.extend(_schedule(project))
    checks.extend(_runs(app))
    checks.extend(_backlog(app))
    return checks


def worst(checks: list[Check]) -> str:
    statuses = {c.status for c in checks}
    if FAIL in statuses:
        return FAIL
    return WARN if WARN in statuses else OK


def _paths(app: App) -> list[Check]:
    out: list[Check] = []
    for name, path in (
        ("database", Path(app.settings.db_path)),
        ("store", Path(app.settings.store_path)),
        ("digests", Path(app.settings.digest_path)),
    ):
        parent = path if path.is_dir() else path.parent
        if not parent.exists():
            out.append(Check(name, FAIL, f"{parent} does not exist"))
            continue
        try:
            probe = parent / ".brand-evidence-write-probe"
            probe.touch()
            probe.unlink()
        except OSError as exc:
            out.append(Check(name, FAIL, f"{parent} is not writable: {exc}"))
            continue
        out.append(Check(name, OK, str(path)))

    store = Path(app.settings.store_path)
    database = Path(app.settings.db_path)
    usage = shutil.disk_usage(store.parent)
    store_bytes = sum(f.stat().st_size for f in store.rglob("*") if f.is_file())
    db_bytes = database.stat().st_size if database.exists() else 0
    detail = (
        f"{_gb(usage.free)} free; store {_gb(store_bytes)}, database {_gb(db_bytes)}. "
        f"An export needs about {_gb((store_bytes + db_bytes) * 2)} more."
    )
    status = WARN if usage.free < LOW_SPACE_BYTES else OK
    out.append(Check("disk", status, detail))
    return out


def _schema(app: App) -> list[Check]:
    if not schema_present(app.engine):
        return [Check("schema", FAIL, "no tables; run `brand-evidence init`")]
    missing = _missing_triggers(app)
    checks = [Check("schema", OK, "tables present")]
    if missing:
        # The triggers are what make the log append-only against a direct SQL
        # session. Without them the guarantee is a convention.
        checks.append(
            Check("triggers", FAIL, f"append-only triggers missing: {', '.join(missing)}")
        )
    else:
        checks.append(Check("triggers", OK, "append-only triggers installed"))
    checks.append(_alembic(app))
    return checks


def _missing_triggers(app: App) -> list[str]:
    names = [
        statement.split("EXISTS ", 1)[1].split("\n", 1)[0].strip()
        for statement in APPEND_ONLY_TRIGGERS
    ]
    with app.engine.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall()
    installed = {row[0] for row in rows}
    return [name for name in names if name not in installed]


def _alembic(app: App) -> Check:
    try:
        from alembic.config import Config
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory
    except ImportError:  # pragma: no cover - alembic is a dependency
        return Check("migrations", WARN, "alembic is not installed")
    # Alembic logs its plugin setup at import; this check is a one-line answer,
    # not a migration run.
    logging.getLogger("alembic").setLevel(logging.WARNING)
    root = Path(__file__).resolve().parents[2]
    ini = root / "alembic.ini"
    if not ini.exists():
        return Check("migrations", WARN, f"no alembic.ini beside the package ({ini})")
    script = ScriptDirectory.from_config(Config(str(ini)))
    head = script.get_current_head()
    with app.engine.connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision()
    if current == head:
        return Check("migrations", OK, f"at head ({head})")
    if current is None:
        # Tables but no version: built by create_all, which is for tests. A
        # migration cannot be applied to it safely, so say which one it is.
        return Check(
            "migrations",
            WARN,
            f"schema was not built by alembic (head is {head}); fine for a test "
            "database, not for one holding evidence",
        )
    return Check(
        "migrations",
        FAIL,
        f"database is at {current}, head is {head}; run `brand-evidence init`",
    )


def _chain(app: App) -> list[Check]:
    with app.sessions() as session:
        entries = session.scalar(select(func.count()).select_from(EvidenceEntry)) or 0
        if not entries:
            return [Check("evidence chain", WARN, "empty: nothing has been recorded yet")]
        result = evidence_log.verify_since_checkpoint(session, record=False)
    status = OK if result.ok else FAIL
    return [Check("evidence chain", status, result.describe())]


def _sources(app: App) -> list[Check]:
    enabled, skipped = build_sources(app.settings, app.sources_config)
    checks = [
        Check("sources", OK if enabled else FAIL, f"{len(enabled)} enabled: {_names(enabled)}")
    ]
    if skipped:
        # Named, because a source disabled for a missing key looks exactly like
        # a source that found nothing.
        checks.append(Check("sources", WARN, f"skipped for missing credentials: {skipped}"))
    return checks


def _names(sources: Sequence[object]) -> str:
    return ", ".join(sorted(getattr(s, "name", "?") for s in sources)) or "none"


def _schedule(project: str | None) -> list[Check]:
    if sys.platform != "darwin":
        return [Check("schedule", WARN, "launchd is macOS only; schedule these jobs yourself")]
    try:
        listed = subprocess.run(  # noqa: S603
            ["/bin/launchctl", "list"], capture_output=True, text=True, check=False, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return [Check("schedule", WARN, f"could not ask launchctl: {exc}")]

    checks: list[Check] = []
    for job in JOBS:
        label = label_for(job, project)
        plist = AGENTS_DIR / f"{label}.plist"
        if not plist.exists():
            # Not a failure: plenty of people schedule these another way. It is
            # worth saying, because "I installed it" and "it runs" differ.
            checks.append(
                Check(f"schedule/{job}", WARN, "no launchd job; `schedule install` makes one")
            )
            continue
        line = next((x for x in listed.splitlines() if x.endswith(label)), "")
        if not line:
            checks.append(
                Check(f"schedule/{job}", FAIL, "plist exists but the job is not loaded")
            )
            continue
        status = line.split()[1] if len(line.split()) > 1 else "-"
        if status not in {"0", "-"}:
            # launchctl reports the last exit status; anything else means the
            # job ran and failed, which no log rotation will tell you.
            checks.append(Check(f"schedule/{job}", FAIL, f"last exit status {status}"))
        else:
            checks.append(Check(f"schedule/{job}", OK, _when(plist)))
    return checks


def _when(plist: Path) -> str:
    try:
        data = plistlib.loads(plist.read_bytes())
    except (OSError, plistlib.InvalidFileException):
        return "loaded"
    if "StartInterval" in data:
        return f"loaded, every {int(data['StartInterval']) // 60} min"
    when = data.get("StartCalendarInterval", {})
    if isinstance(when, dict) and "Hour" in when:
        return f"loaded, daily at {int(when['Hour']):02d}:{int(when.get('Minute', 0)):02d}"
    return "loaded"


def _runs(app: App) -> list[Check]:
    now = datetime.now(UTC)
    checks: list[Check] = []
    with app.sessions() as session:
        for job in ("crawl", "digest", "archive_poll", "timestamp"):
            last = session.scalars(
                select(Run).where(Run.job == job).order_by(Run.started_at.desc()).limit(1)
            ).first()
            if last is None:
                checks.append(Check(f"runs/{job}", WARN, "has never run"))
                continue
            age = now - _parse(last.started_at)
            detail = f"last run {last.started_at[:16]} ({last.status})"
            if last.status == "running" and age > STALE_AFTER:
                checks.append(Check(f"runs/{job}", FAIL, f"{detail}: stuck since then"))
            elif age > STALE_AFTER:
                checks.append(Check(f"runs/{job}", WARN, f"{detail}: nothing since"))
            elif last.status == "failed":
                checks.append(Check(f"runs/{job}", FAIL, detail))
            else:
                checks.append(Check(f"runs/{job}", OK, detail))
    return checks


def _backlog(app: App) -> list[Check]:
    with app.sessions() as session:
        pending_captures = (
            session.scalar(
                select(func.count()).select_from(Mention).where(Mention.capture_state == "pending")
            )
            or 0
        )
        pending_archive = (
            session.scalar(
                select(func.count())
                .select_from(ArchiveSnapshot)
                .where(ArchiveSnapshot.status == "pending")
            )
            or 0
        )
        failed_archive = (
            session.scalar(
                select(func.count())
                .select_from(ArchiveSnapshot)
                .where(ArchiveSnapshot.status == "failed")
            )
            or 0
        )
    checks = [
        Check(
            "backlog",
            WARN if pending_captures else OK,
            f"{pending_captures} mention(s) awaiting capture, "
            f"{pending_archive} snapshot(s) pending",
        )
    ]
    if failed_archive:
        checks.append(
            Check("archive", WARN, f"{failed_archive} snapshot(s) gave up after every retry")
        )
    return checks


def _parse(stamp: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return datetime.now(UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _gb(size: float) -> str:
    return f"{size / 1024**3:.1f} GB" if size >= 1024**3 else f"{size / 1024**2:.0f} MB"
