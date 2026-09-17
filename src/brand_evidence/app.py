"""Composition root: settings, engine, store, provider. Everything else receives these."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from brand_evidence.capture.archiver import ArchiveProvider, make_provider
from brand_evidence.capture.timestamp import Rfc3161Authority, TimestampAuthority
from brand_evidence.config.settings import Settings, load_settings
from brand_evidence.config.sources_config import SourcesConfig, load_sources_config
from brand_evidence.core.db import install_triggers, make_engine, make_session_factory
from brand_evidence.core.logging import configure_logging
from brand_evidence.core.models import Base
from brand_evidence.core.store import ArtifactStore
from brand_evidence.sources.base import set_contact_url


@dataclass
class App:
    settings: Settings
    engine: Engine
    sessions: sessionmaker[Session]
    store: ArtifactStore
    archive: ArchiveProvider
    sources_config: SourcesConfig
    tsa: TimestampAuthority | None = None

    @property
    def terms(self) -> list[str]:
        return self.sources_config.terms or self.settings.brand_terms


def build_app(
    settings: Settings | None = None, *, create_schema: bool = False, env_file: Path | None = None
) -> App:
    settings = settings or load_settings(env_file)
    configure_logging(settings.log_level)
    set_contact_url(settings.contact_url)
    engine = make_engine(settings.db_path)
    if create_schema:
        # Tests and first-run convenience; production databases use `alembic upgrade head`.
        Base.metadata.create_all(engine)
    install_triggers(engine)
    return App(
        settings=settings,
        engine=engine,
        sessions=make_session_factory(engine),
        store=ArtifactStore(settings.store_path),
        archive=make_provider(
            settings.archive_provider, settings.wayback_access_key, settings.wayback_secret_key
        ),
        sources_config=load_sources_config(),
        tsa=Rfc3161Authority(settings.tsa_url) if settings.tsa_enabled else None,
    )


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]
