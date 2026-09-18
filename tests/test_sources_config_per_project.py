"""Two projects on one machine must not share one sources.yaml.

The README promises a project is its own .env, database and store, but the
source toggles, the query terms and the skip list came from one file inside the
installed package: registering a second brand made both crawls search for the
first one's terms.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from brand_evidence.app import build_app
from brand_evidence.config.sources_config import DEFAULT_PATH, resolve_sources_path
from tests.conftest import make_settings

SOURCES = """
sources:
  hn_algolia:
    enabled: true
terms: ["Acme Robotics"]
skip_capture_hosts: ["acme.example"]
"""


def test_sources_yaml_beside_the_env_file_wins(tmp_path: Path) -> None:
    (tmp_path / "sources.yaml").write_text(SOURCES, encoding="utf-8")
    assert resolve_sources_path(None, tmp_path / ".env") == tmp_path / "sources.yaml"


def test_the_packaged_defaults_are_the_fallback(tmp_path: Path) -> None:
    assert resolve_sources_path(None, tmp_path / ".env") == DEFAULT_PATH
    assert resolve_sources_path(None, None) == DEFAULT_PATH


def test_a_configured_file_that_is_missing_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="BE_SOURCES_FILE"):
        resolve_sources_path(tmp_path / "gone.yaml", None)


def test_each_project_crawls_its_own_terms(tmp_path: Path) -> None:
    project = tmp_path / "acme"
    project.mkdir()
    (project / "sources.yaml").write_text(SOURCES, encoding="utf-8")
    settings = make_settings(tmp_path, brand_terms=["Northwind"])
    app = build_app(settings, create_schema=True, env_file=project / ".env")
    try:
        assert app.terms == ["Acme Robotics"]
        assert app.sources_config.skip_capture_hosts == ["acme.example"]
        assert not app.sources_config.is_enabled("reddit")
    finally:
        app.engine.dispose()


def test_an_explicit_file_overrides_the_one_beside_the_env(tmp_path: Path) -> None:
    (tmp_path / "sources.yaml").write_text(SOURCES, encoding="utf-8")
    elsewhere = tmp_path / "shared.yaml"
    elsewhere.write_text('terms: ["Northwind"]\n', encoding="utf-8")
    settings = make_settings(tmp_path, sources_file=elsewhere)
    app = build_app(settings, create_schema=True, env_file=tmp_path / ".env")
    try:
        assert app.terms == ["Northwind"]
    finally:
        app.engine.dispose()
