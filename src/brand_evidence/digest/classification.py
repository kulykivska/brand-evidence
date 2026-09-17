"""Optional advisory tagging of new mentions. Runs after the evidence write and never touches
evidence_log; the tag lives in mentions.notes only. Any failure is logged and ignored."""

from __future__ import annotations

from sqlalchemy import select

from brand_evidence.app import App
from brand_evidence.core.db import session_scope
from brand_evidence.core.logging import get_logger
from brand_evidence.core.models import Mention

log = get_logger(__name__)
TAGS = ("positive", "neutral", "negative", "irrelevant")
TAG_PREFIX = "[auto-tag:"


def classify_new_mentions(app: App, limit: int = 50) -> int:
    if not app.settings.enable_classification:
        return 0
    try:
        import anthropic
    except ImportError:
        log.warning("classification_skipped", reason="anthropic package not installed")
        return 0
    client = anthropic.Anthropic(api_key=app.settings.anthropic_api_key)
    tagged = 0
    with session_scope(app.sessions) as session:
        rows = session.scalars(
            select(Mention)
            .where(Mention.status == "new")
            .order_by(Mention.discovered_at.desc())
            .limit(limit)
        ).all()
        for m in rows:
            if m.notes and TAG_PREFIX in m.notes:
                continue
            try:
                tag = _ask(client, app.terms, m)
            except Exception as exc:  # noqa: BLE001 - advisory only
                log.warning("classification_failed", mention=m.id, error=str(exc))
                continue
            m.notes = f"{m.notes + chr(10) if m.notes else ''}{TAG_PREFIX}{tag}]"
            tagged += 1
    return tagged


def _ask(client, terms: list[str], m: Mention) -> str:  # type: ignore[no-untyped-def]
    prompt = (
        f"Brand terms: {', '.join(terms)}.\n"
        f"Title: {m.title}\nExcerpt: {m.excerpt}\nURL: {m.url}\n\n"
        "Reply with exactly one word from: positive, neutral, negative, irrelevant. "
        "Use irrelevant when the text is not about this brand."
    )
    response = client.messages.create(
        model="claude-opus-5",
        max_tokens=16,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}],
    )
    if response.stop_reason == "refusal":
        return "neutral"
    text = "".join(b.text for b in response.content if b.type == "text").strip().lower()
    return text if text in TAGS else "neutral"
