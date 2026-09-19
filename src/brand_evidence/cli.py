"""Typer CLI. Every command here maps to one requirement in §9."""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError
from sqlalchemy import select

from brand_evidence import __version__

app = typer.Typer(
    help="Contemporaneous evidence of brand use plus mention monitoring.",
    no_args_is_help=True,
    add_completion=False,
)
mentions_app = typer.Typer(help="Triage third-party mentions.", no_args_is_help=True)
schedule_app = typer.Typer(help="Manage the launchd jobs.", no_args_is_help=True)
projects_app = typer.Typer(help="Registry of projects (one .env each).", no_args_is_help=True)
app.add_typer(mentions_app, name="mentions")
app.add_typer(schedule_app, name="schedule")
app.add_typer(projects_app, name="projects")

_state: dict[str, str | None] = {"project": None, "env_file": None}


def _app(*, require_schema: bool = True):  # type: ignore[no-untyped-def]
    from brand_evidence.app import build_app
    from brand_evidence.core.db import schema_present

    try:
        env_file = Path(_state["env_file"]) if _state["env_file"] else None
        be = build_app(env_file=env_file)
    except ValidationError as exc:
        typer.secho("Configuration incomplete. Fix .env and retry:", fg="red", err=True)
        for err in exc.errors():
            loc = ".".join(str(x) for x in err["loc"]) or "(model)"
            typer.secho(f"  BE_{loc.upper()}: {err['msg']}", err=True)
        raise typer.Exit(2) from None
    if require_schema and not schema_present(be.engine):
        typer.secho("Database schema missing. Run `brand-evidence init` first.", fg="red", err=True)
        raise typer.Exit(2)
    return be


def _parse_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    from brand_evidence.core.clock import parse_iso

    return parse_iso(value)


@app.callback()
def _root(
    version: bool = typer.Option(False, "--version", is_eager=True),
    project: Annotated[
        str | None,
        typer.Option("--project", "-p", envvar="BE_PROJECT", help="Project name from the registry"),
    ] = None,
) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit()
    if project:
        from brand_evidence.config.projects import load_registry

        try:
            entry = load_registry().get(project)
        except KeyError as exc:
            typer.secho(str(exc), fg="red", err=True)
            raise typer.Exit(2) from None
        _state["project"] = project
        _state["env_file"] = str(entry.env_file)
        if entry.workdir:
            os.chdir(entry.workdir)


@app.command()
def hook(
    platform: Annotated[str, typer.Option(help="linkedin | threads | x | other")],
    url: Annotated[str, typer.Option(help="Canonical public URL of the post")],
    body_file: Annotated[
        Path, typer.Option(exists=True, readable=True, help="File with the post text")
    ],
    published_at: Annotated[str | None, typer.Option(help="ISO-8601 from the platform")] = None,
    external_id: Annotated[str | None, typer.Option()] = None,
    language: Annotated[str, typer.Option(help="en | uk | ru")] = "en",
    no_capture: Annotated[bool, typer.Option("--no-capture", help="Record the post only")] = False,
) -> None:
    """Push path: record a post the moment it is published."""
    from brand_evidence.ingest.hook import DuplicatePostError, ingest_post

    be = _app()
    try:
        result = ingest_post(
            be,
            platform=platform,
            url=url,
            body=body_file.read_text(encoding="utf-8"),
            published_at=published_at,
            external_id=external_id,
            language=language,
            capture=not no_capture,
        )
    except DuplicatePostError as exc:
        typer.secho(str(exc), fg="yellow", err=True)
        raise typer.Exit(1) from None
    typer.echo(f"post {result.post_id}")
    for kind, digest in result.artifact_hashes.items():
        typer.echo(f"  {kind:15} {digest}")
    typer.echo(f"  archive         {result.archive_status}")


@app.command()
def crawl(
    since: Annotated[str | None, typer.Option(help="ISO-8601; default 48h ago")] = None,
    source: Annotated[str | None, typer.Option(help="Run only this source")] = None,
    no_capture: Annotated[bool, typer.Option("--no-capture")] = False,
) -> None:
    """Pull path: search enabled sources for brand mentions."""
    from brand_evidence.core.models import Run
    from brand_evidence.digest.classification import classify_new_mentions
    from brand_evidence.ingest.crawler import run_crawl

    be = _app()
    run_id = run_crawl(be, since=_parse_iso(since), only_source=source, capture=not no_capture)
    with be.sessions() as session:
        run = session.get(Run, run_id)
        status = run.status if run else "failed"
        typer.echo(f"run {run_id} {status} {run.stats if run else {}}")
    tagged = classify_new_mentions(be)
    if tagged:
        typer.echo(f"advisory tags added: {tagged}")
    if status == "failed":
        raise typer.Exit(1)


@app.command("archive-poll")
def archive_poll() -> None:
    """Confirm pending archive submissions."""
    from brand_evidence.core.models import Run
    from brand_evidence.ingest.archive_poll import run_archive_poll

    be = _app()
    run_id = run_archive_poll(be)
    with be.sessions() as session:
        run = session.get(Run, run_id)
        typer.echo(f"run {run_id} {run.status if run else 'missing'} {run.stats if run else {}}")


@app.command()
def digest(
    for_date: Annotated[
        str | None, typer.Option("--date", help="YYYY-MM-DD, default today")
    ] = None,
    email: Annotated[bool, typer.Option("--email", help="Also send via BE_SMTP_URL")] = False,
) -> None:
    """Write digests/YYYY-MM-DD.md and print it."""
    from brand_evidence.digest.builder import build_digest

    be = _app()
    if email and not be.settings.smtp_url:
        typer.secho("--email requires BE_SMTP_URL", fg="red", err=True)
        raise typer.Exit(2)
    out = build_digest(be, date.fromisoformat(for_date) if for_date else None, email=email)
    typer.echo(out.markdown)
    typer.secho(f"written {out.path}", err=True)


@app.command("sync-posts")
def sync_posts(no_capture: Annotated[bool, typer.Option("--no-capture")] = False) -> None:
    """Record new posts from the configured publications feed (BE_OWN_PUBLICATIONS_URL)."""
    from brand_evidence.core.runs import tracked_run
    from brand_evidence.ingest.publications_sync import sync_publications

    be = _app()
    if not be.settings.own_publications_enabled:
        typer.secho("BE_OWN_PUBLICATIONS_URL is not set", fg="red", err=True)
        raise typer.Exit(2)
    with tracked_run(be.sessions, "sync_posts") as ctx:
        created = sync_publications(be, ctx, capture=not no_capture)
    typer.echo(f"new posts {created}  {ctx.stats.get('own_publications')}")


@app.command()
def report(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output")] = False,
    hours: int = 24,
) -> None:
    """Standing report for the interface layer: did / found / need."""
    from brand_evidence.digest.report import build_report

    be = _app()
    data = build_report(be, project=_state["project"], hours=hours)
    if as_json:
        typer.echo(json.dumps(data, indent=2))
        return
    typer.echo(f"brand-evidence{' / ' + data['project'] if data['project'] else ''}")
    typer.echo(f"did:   {data['did']}")
    typer.echo(
        f"found: {len(data['found']['new_mentions'])} new mention(s); {data['found']['chain']}"
    )
    typer.echo("need:  " + ("; ".join(data["need"]) or "nothing"))


@app.command("archive-history")
def archive_history(
    url: Annotated[str | None, typer.Option(help="One URL; default: every recorded post")] = None,
) -> None:
    """Record snapshots the archive already held before this system existed."""
    from brand_evidence.core.db import session_scope
    from brand_evidence.core.models import Post
    from brand_evidence.core.runs import tracked_run
    from brand_evidence.ingest.capture_pipeline import record_history

    be = _app()
    with tracked_run(be.sessions, "archive_history") as ctx, session_scope(be.sessions) as session:
        stmt = select(Post)
        if url:
            stmt = stmt.where(Post.url == url)
        posts = session.scalars(stmt).all()
        total = 0
        for post in posts:
            added = record_history(be, session, "post", post.id, post.url)
            if added < 0:
                ctx.mark_partial(f"history lookup failed for {post.url}")
                continue
            total += added
            typer.echo(f"{post.url}: {added} historical snapshot(s)")
        ctx.stats = {"posts": len(posts), "historical_snapshots": total}
    typer.echo(f"recorded {total} historical snapshot(s) for {len(posts)} post(s)")


@app.command()
def attach(
    file: Annotated[Path, typer.Option(exists=True, readable=True)],
    kind: Annotated[str, typer.Option(help="platform_export | email | document | image | other")],
    post_id: Annotated[
        str | None, typer.Option(help="Attach to a post; default: the project")
    ] = None,
    note: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Store an external file (platform data export, notification email, PDF) as evidence."""
    from brand_evidence.core.db import session_scope
    from brand_evidence.core.models import Post
    from brand_evidence.ingest.capture_pipeline import attach_file

    be = _app()
    with session_scope(be.sessions) as session:
        if post_id and session.get(Post, post_id) is None:
            typer.secho("post not found", fg="red", err=True)
            raise typer.Exit(1)
        subject_type, subject_id = ("post", post_id) if post_id else ("project", "project")
        try:
            row = attach_file(
                be,
                session,
                subject_type,
                subject_id,
                kind,
                file.read_bytes(),
                filename=file.name,
                note=note,
            )
        except ValueError as exc:
            typer.secho(str(exc), fg="red", err=True)
            raise typer.Exit(2) from None
    typer.echo(f"artifact {row.id}\n  {kind:15} {row.sha256}  {row.bytes} bytes")


@app.command()
def timestamp() -> None:
    """Anchor the evidence chain head with an RFC 3161 trusted timestamp (only a hash leaves)."""
    from brand_evidence.core.db import session_scope
    from brand_evidence.core.runs import tracked_run
    from brand_evidence.ingest.capture_pipeline import timestamp_chain_head

    be = _app()
    if be.tsa is None:
        typer.secho("BE_TSA_URL is empty; timestamping disabled", fg="yellow", err=True)
        raise typer.Exit(2)
    with tracked_run(be.sessions, "timestamp") as ctx, session_scope(be.sessions) as session:
        row = timestamp_chain_head(be, session)
        if row is None:
            typer.echo("chain is empty; nothing to anchor")
            return
        ctx.stats = {"stamped_seq": row.capture_meta["stamped_seq"], "tsa": be.tsa.url}
    typer.echo(
        f"anchored seq {row.capture_meta['stamped_seq']} at {row.captured_at} via {be.tsa.url}"
    )
    typer.echo(f"  token sha256 {row.sha256}")


@app.command()
def doctor() -> None:
    """Check that this installation is actually recording: paths, schema,
    triggers, chain, sources, schedule, recent runs and backlog."""
    from brand_evidence import doctor as checks

    be = _app()
    results = checks.run_checks(be, project=_state["project"])
    for check in results:
        typer.echo(str(check))
    verdict = checks.worst(results)
    typer.echo("")
    if verdict == checks.FAIL:
        typer.echo("Something is wrong: the lines marked FAIL above.")
        raise typer.Exit(1)
    if verdict == checks.WARN:
        typer.echo("Working, with the warnings above.")
        return
    typer.echo("Everything checks out.")


@app.command()
def verify() -> None:
    """Walk the evidence hash chain and report the first break."""
    from brand_evidence.core import evidence_log

    be = _app()
    with be.sessions() as session:
        result = evidence_log.verify(session)
    missing = [digest for digest in _artifact_hashes(be) if not be.store.verify(digest)]
    typer.echo(result.describe())
    bad_stamps = _check_timestamps(be)
    if bad_stamps:
        typer.echo(f"timestamps: {len(bad_stamps)} token(s) do not match their chain entry")
        for note in bad_stamps[:20]:
            typer.echo(f"  {note}")
    if missing:
        typer.echo(f"artifact store: {len(missing)} artifact(s) missing or corrupted")
        for d in missing[:20]:
            typer.echo(f"  {d}")
    if not result.ok or missing or bad_stamps:
        raise typer.Exit(1)
    typer.echo(f"artifact store: all {len(list(_artifact_hashes(be)))} artifacts intact")


def _check_timestamps(be) -> list[str]:  # type: ignore[no-untyped-def]
    """Each token's digest must equal sha256 of the seq:entry_hash it claims to anchor."""
    import hashlib

    from brand_evidence.core.models import Artifact, EvidenceEntry

    problems: list[str] = []
    with be.sessions() as session:
        stamps = session.scalars(select(Artifact).where(Artifact.kind == "timestamp")).all()
        for stamp in stamps:
            meta = stamp.capture_meta
            entry = session.get(EvidenceEntry, meta.get("stamped_seq"))
            material = f"{meta.get('stamped_seq')}:{entry.entry_hash if entry else ''}".encode()
            if entry is None or hashlib.sha256(material).hexdigest() != meta.get("digest_sha256"):
                problems.append(f"token {stamp.sha256[:12]} for seq {meta.get('stamped_seq')}")
    return problems


def _artifact_hashes(be) -> list[str]:  # type: ignore[no-untyped-def]
    from brand_evidence.core.models import Artifact

    with be.sessions() as session:
        return list(session.scalars(select(Artifact.sha256).distinct()))


@app.command()
def export(
    out: Annotated[Path, typer.Option(help="Output zip path")],
    fmt: Annotated[str, typer.Option("--format")] = "zip",
) -> None:
    """Produce the evidence package for a third party."""
    from brand_evidence.export import export_zip

    if fmt != "zip":
        typer.secho("only --format zip is supported", fg="red", err=True)
        raise typer.Exit(2)
    be = _app()
    path = export_zip(be, out)
    typer.echo(f"written {path} ({path.stat().st_size} bytes)")


@mentions_app.command("list")
def mentions_list(
    status: Annotated[str | None, typer.Option(help="new | reviewed | actioned | ignored")] = None,
    limit: int = 50,
) -> None:
    from brand_evidence.core.models import Mention

    be = _app()
    with be.sessions() as session:
        stmt = select(Mention).order_by(Mention.discovered_at.desc()).limit(limit)
        if status:
            stmt = stmt.where(Mention.status == status)
        rows = session.scalars(stmt).all()
    if not rows:
        typer.echo("0 mentions")
        return
    for m in rows:
        typer.echo(f"{m.id}  {m.status:8} {m.discovered_at[:10]} {m.source:17} {m.url}")
        if m.title:
            typer.echo(f"    {m.title[:120]}")


@mentions_app.command("set-status")
def mentions_set_status(
    mention_id: str,
    status: str,
    note: Annotated[str | None, typer.Option()] = None,
) -> None:
    from brand_evidence.core import evidence_log
    from brand_evidence.core.db import session_scope
    from brand_evidence.core.models import Mention

    if status not in ("new", "reviewed", "actioned", "ignored"):
        typer.secho("status must be new | reviewed | actioned | ignored", fg="red", err=True)
        raise typer.Exit(2)
    be = _app()
    with session_scope(be.sessions) as session:
        m = session.get(Mention, mention_id)
        if m is None:
            typer.secho("mention not found", fg="red", err=True)
            raise typer.Exit(1)
        m.status = status
        if note:
            m.notes = f"{m.notes}\n{note}" if m.notes else note
        evidence_log.append(
            session, "mention.status_changed", {"id": m.id, "status": status, "note": note}
        )
    typer.echo(f"{mention_id} -> {status}")


@projects_app.command("list")
def projects_list() -> None:
    from brand_evidence.config.projects import DEFAULT_REGISTRY, load_registry

    registry = load_registry()
    if not registry.projects:
        typer.echo(f"no projects registered ({DEFAULT_REGISTRY})")
        return
    for name, entry in sorted(registry.projects.items()):
        typer.echo(
            f"{name:20} {entry.env_file}{'  cwd=' + str(entry.workdir) if entry.workdir else ''}"
        )


@projects_app.command("add")
def projects_add(
    name: str,
    env_file: Annotated[Path, typer.Option(exists=True, readable=True, resolve_path=True)],
    workdir: Annotated[Path | None, typer.Option(exists=True, resolve_path=True)] = None,
) -> None:
    """Register a project. Its .env decides the database, store and brand terms."""
    from brand_evidence.config.projects import Project, load_registry, save_registry

    registry = load_registry()
    registry.projects[name] = Project(env_file=env_file, workdir=workdir or env_file.parent)
    typer.echo(f"registered {name} -> {save_registry(registry)}")


@projects_app.command("remove")
def projects_remove(name: str) -> None:
    from brand_evidence.config.projects import load_registry, save_registry

    registry = load_registry()
    if registry.projects.pop(name, None) is None:
        typer.secho("unknown project", fg="red", err=True)
        raise typer.Exit(1)
    save_registry(registry)
    typer.echo(f"removed {name}")


@schedule_app.command("install")
def schedule_install(dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    """Write and load launchd jobs (crawl 07:00, archive-poll every 2h, digest 07:30).

    With a registry, one set of jobs per project; otherwise one set for the current directory.
    """
    from brand_evidence import schedule
    from brand_evidence.config.projects import load_registry

    if sys.platform != "darwin":
        typer.secho("launchd scheduling is macOS only; see README for GitHub Actions.", fg="red")
        raise typer.Exit(2)
    registry = load_registry()
    targets = (
        [(name, p.workdir or p.env_file.parent) for name, p in sorted(registry.projects.items())]
        if registry.projects and not _state["project"]
        else [(_state["project"], Path.cwd())]
    )
    for project, workdir in targets:
        if project:
            _state["project"], _state["env_file"] = project, str(registry.get(project).env_file)
        _app()  # fail fast on config before scheduling anything
        for path in schedule.install(workdir, project=project, dry_run=dry_run):
            typer.echo(f"{'would write' if dry_run else 'loaded'} {path}")


@schedule_app.command("uninstall")
def schedule_uninstall() -> None:
    from brand_evidence import schedule

    for path in schedule.uninstall():
        typer.echo(f"removed {path}")


@app.command()
def backfill(
    file: Annotated[Path, typer.Option(exists=True, readable=True)],
    no_capture: Annotated[bool, typer.Option("--no-capture")] = False,
    no_history: Annotated[bool, typer.Option("--no-history")] = False,
) -> None:
    """Import historical posts from CSV (platform,url,body,published_at[,external_id,language])."""
    from brand_evidence.ingest.backfill import run_backfill

    be = _app()
    counts = run_backfill(be, file, capture=not no_capture, history=not no_history)
    typer.echo(
        f"rows {counts['rows']}  imported {counts['imported']}  "
        f"duplicates {counts['duplicates']}  prior snapshots {counts['historical_snapshots']}"
    )


@app.command()
def init() -> None:
    """Create or upgrade the database schema (runs `alembic upgrade head`)."""
    from alembic.config import Config

    from alembic import command
    from brand_evidence.app import repo_root
    from brand_evidence.core.db import install_triggers

    be = _app(require_schema=False)
    cfg = Config(str(repo_root() / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", be.settings.sqlalchemy_url)
    command.upgrade(cfg, "head")
    install_triggers(be.engine)
    typer.echo(f"schema ready at {be.settings.db_path}")


if __name__ == "__main__":
    app()
