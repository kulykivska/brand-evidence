"""Builds the list of enabled, credentialed sources. Missing credentials => warning, not crash."""

from __future__ import annotations

from brand_evidence.config.settings import Settings
from brand_evidence.config.sources_config import SourcesConfig
from brand_evidence.core.logging import get_logger
from brand_evidence.sources.base import Source
from brand_evidence.sources.google_alerts_rss import GoogleAlertsRSSSource
from brand_evidence.sources.hn_algolia import HNAlgoliaSource
from brand_evidence.sources.reddit import RedditSource
from brand_evidence.sources.web_search import WebSearchSource

log = get_logger(__name__)


def build_sources(settings: Settings, config: SourcesConfig) -> tuple[list[Source], list[str]]:
    """Return (active sources, names skipped for missing credentials)."""
    active: list[Source] = []
    skipped: list[str] = []

    if config.is_enabled("hn_algolia"):
        active.append(HNAlgoliaSource())
    if config.is_enabled("google_alerts_rss"):
        if settings.google_alerts_feeds:
            active.append(GoogleAlertsRSSSource(settings.google_alerts_feeds))
        else:
            skipped.append("google_alerts_rss")
    if config.is_enabled("reddit"):
        if settings.reddit_enabled:
            active.append(RedditSource(settings.reddit_client_id, settings.reddit_client_secret))
        else:
            skipped.append("reddit")
    if config.is_enabled("web_search"):
        if settings.web_search_enabled:
            active.append(
                WebSearchSource(settings.web_search_provider, settings.web_search_api_key)
            )
        else:
            skipped.append("web_search")

    for name in skipped:
        log.warning("source_disabled_missing_credentials", source=name)
    return active, skipped
