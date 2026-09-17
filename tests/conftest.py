from __future__ import annotations

import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from brand_evidence.app import App, build_app
from brand_evidence.config.settings import Settings


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test runs with networking disabled; HTTP is mocked with respx where needed."""

    def guard(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("network access attempted during tests")

    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket, "create_connection", guard)


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    base = {
        "db_path": tmp_path / "be.db",
        "store_path": tmp_path / "store",
        "digest_path": tmp_path / "digests",
        "brand_terms": ["Northwind", "North Wind", "northwind.example"],
        "archive_provider": "wayback",
        "wayback_access_key": "test-access",
        "wayback_secret_key": "test-secret",
        "log_level": "warning",
        "archive_min_interval_seconds": 0.0,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def be(settings: Settings) -> Iterator[App]:
    app = build_app(settings, create_schema=True)
    yield app
    app.engine.dispose()
