"""Optional advisory tagging of new mentions. Runs after the evidence write and never touches
evidence_log; the tag lives in mentions.notes only. Any failure is logged and ignored."""

from __future__ import annotations

from dataclasses import replace

import httpx
from sqlalchemy import select

from brand_evidence.app import App
from brand_evidence.core.db import session_scope
from brand_evidence.core.llm import LlmClient
from brand_evidence.core.logging import get_logger
from brand_evidence.core.models import Mention

log = get_logger(__name__)
TAGS = ("positive", "neutral", "negative", "irrelevant")
TAG_PREFIX = "[auto-tag:"


def classify_new_mentions(app: App, limit: int = 50) -> int:
    """Tag untagged mentions. Reads, then asks, then writes.

    The model is a network call, and holding SQLite's write lock across fifty
    of them locks every other job out for the length of the run.
    """
    if not app.settings.enable_classification:
        return 0
    client = LlmClient(
        provider=app.settings.llm_provider,
        model=app.settings.llm_model,
        api_key=app.settings.classification_key,
        base_url=app.settings.llm_base_url,
    )
    with app.sessions() as session:
        pending = [
            (m.id, m.title, m.excerpt, m.url)
            for m in session.scalars(
                select(Mention)
                .where(Mention.status == "new")
                .order_by(Mention.discovered_at.desc())
                .limit(limit)
            ).all()
            if not (m.notes and TAG_PREFIX in m.notes)
        ]
    if not pending:
        return 0

    tags: dict[str, str] = {}
    # One connection for the batch rather than one per mention.
    with httpx.Client(timeout=client.timeout) as http:
        asked = replace(client, session=http)
        for mention_id, title, excerpt, url in pending:
            try:
                tags[mention_id] = _ask(asked, app.terms, title, excerpt, url)
            except Exception as exc:  # noqa: BLE001 - advisory only
                log.warning("classification_failed", mention=mention_id, error=str(exc))

    tagged = 0
    with session_scope(app.sessions) as session:
        for mention_id, tag in tags.items():
            mention = session.get(Mention, mention_id)
            if mention is None or (mention.notes and TAG_PREFIX in mention.notes):
                continue
            prefix = f"{mention.notes}{chr(10)}" if mention.notes else ""
            mention.notes = f"{prefix}{TAG_PREFIX}{tag}]"
            tagged += 1
    return tagged


def _ask(client: LlmClient, terms: list[str], title: str, excerpt: str, url: str) -> str:
    prompt = (
        f"Brand terms: {', '.join(terms)}.\n"
        f"Title: {title}\nExcerpt: {excerpt}\nURL: {url}\n\n"
        "Reply with exactly one word from: positive, neutral, negative, irrelevant. "
        "Use irrelevant when the text is not about this brand."
    )
    answer = client.ask(prompt).strip().lower().strip(".")
    # Anything that is not one of the tags is not a tag. Neutral is the
    # honest default: the mention is recorded either way, and this line is
    # advisory only.
    return answer if answer in TAGS else "neutral"
