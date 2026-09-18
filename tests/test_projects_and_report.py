from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from brand_evidence.app import App
from brand_evidence.cli import app as cli
from brand_evidence.config.projects import Project, Registry, load_registry, save_registry
from brand_evidence.digest.report import build_report
from brand_evidence.ingest.crawler import run_crawl
from brand_evidence.ingest.hook import ingest_post
from tests.factories import BrokenSource, FakeArchive, FakeCapturer

runner = CliRunner()


def test_registry_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "projects.yaml"
    reg = Registry(projects={"northwind": Project(env_file=tmp_path / ".env")})
    save_registry(reg, path)
    loaded = load_registry(path)
    assert loaded.get("northwind").env_file == tmp_path / ".env"
    with pytest.raises(KeyError, match="known: northwind"):
        loaded.get("nope")
    assert load_registry(tmp_path / "missing.yaml").projects == {}


def test_report_did_found_need(be: App) -> None:
    be.archive = FakeArchive(confirm_on_submit=True)
    ingest_post(
        be,
        platform="threads",
        url="https://threads.net/p/1",
        body="Northwind",
        published_at=None,
        capturer=FakeCapturer(),
    )
    run_crawl(be, sources=[BrokenSource()], capture=False)
    data = build_report(be, project="northwind")
    assert data["agent"] == "brand-evidence" and data["project"] == "northwind"
    assert data["did"]["posts_captured"] == 1 and data["did"]["archives_confirmed"] == 1
    # The hook writes a run of its own now, so a push that dies before
    # capture is visible in the report.
    assert data["did"]["runs"] == {"crawl": "partial", "hook": "ok"}
    assert data["found"]["failed_sources"] == ["broken"]
    assert data["found"]["chain"].startswith("chain valid")
    assert data["need"] == ["source(s) failing: broken"]


def test_cli_unknown_project_exits_2(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("brand_evidence.config.projects.DEFAULT_REGISTRY", tmp_path / "r.yaml")
    result = runner.invoke(cli, ["--project", "ghost", "verify"])
    assert result.exit_code == 2 and "unknown project" in result.output


@pytest.mark.skipif(sys.platform != "darwin", reason="launchd is macOS only")
def test_cli_project_selects_env_file(be: App, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    s = be.settings
    env = tmp_path / "proj.env"
    env.write_text(
        f"BE_DB_PATH={s.db_path}\nBE_STORE_PATH={s.store_path}\nBE_DIGEST_PATH={s.digest_path}\n"
        "BE_BRAND_TERMS=Northwind\nBE_ARCHIVE_PROVIDER=wayback\n"
        "BE_WAYBACK_ACCESS_KEY=a\nBE_WAYBACK_SECRET_KEY=b\nBE_LOG_LEVEL=warning\n"
    )
    registry_path = tmp_path / "r.yaml"
    monkeypatch.setattr("brand_evidence.config.projects.DEFAULT_REGISTRY", registry_path)
    for var in (
        "BE_DB_PATH",
        "BE_STORE_PATH",
        "BE_DIGEST_PATH",
        "BE_BRAND_TERMS",
        "BE_ARCHIVE_PROVIDER",
        "BE_WAYBACK_ACCESS_KEY",
        "BE_WAYBACK_SECRET_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(cli, ["projects", "add", "rm", "--env-file", str(env)]).exit_code == 0
    result = runner.invoke(cli, ["--project", "rm", "report", "--json"])
    assert result.exit_code == 0, result.output
    assert '"project": "rm"' in result.output
    result = runner.invoke(cli, ["schedule", "install", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "brand-evidence.rm.crawl.plist" in result.output
