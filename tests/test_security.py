"""The safety rules that protect the machine and the credentials."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from brand_evidence.core.logging import _mask
from brand_evidence.core.urlguard import UnsafeUrlError, check_url, is_safe
from brand_evidence.sources.base import (
    MAX_RESPONSE_BYTES,
    ResponseTooLargeError,
    SourceUnavailableError,
    fetch,
    http_kwargs,
)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "data:text/html,<h1>hi</h1>",
        "ftp://example.com/x",
        "javascript:alert(1)",
        "http://",
    ],
)
def test_unfetchable_schemes_are_refused(url: str) -> None:
    """Crawled URLs come from strangers. file:// would read this machine into
    the evidence store and then into the export handed to a third party."""
    with pytest.raises(UnsafeUrlError):
        check_url(url, resolve=False)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/admin",
        "http://localhost/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/",
        "http://[::1]/",
    ],
)
def test_private_and_loopback_addresses_are_refused(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        check_url(url)


def test_ordinary_public_urls_pass() -> None:
    assert is_safe("https://example.com/post/1", resolve=False)


def test_a_capability_url_is_never_logged_in_full() -> None:
    """A Google Alerts feed URL is the credential: whoever holds it can read
    the alert. It is a GitHub Actions secret, and an httpx error message used
    to put it in the run log and in runs.error."""
    feed = "https://www.google.com/alerts/feeds/12345678901234567890/9876543210987654321"
    out = _mask(None, "info", {"event": f"fetch failed for url '{feed}'"})
    assert "12345678901234567890" not in str(out)
    assert "www.google.com" in str(out)


def test_credentials_inside_a_url_are_masked() -> None:
    out = _mask(None, "info", {"smtp": "smtps://user:hunter2@example.com:465"})
    assert "hunter2" not in str(out)


def test_secrets_nested_in_a_dict_are_masked() -> None:
    """Run stats are nested dicts, and the masker used to look at top-level
    keys only."""
    out = _mask(None, "info", {"stats": {"api_key": "sk-secret", "ok": 1}})
    assert "sk-secret" not in str(out)
    assert out["stats"]["ok"] == 1


def test_access_key_is_recognised_as_a_secret() -> None:
    out = _mask(None, "info", {"wayback_access_key": "not-a-real-key-value"})
    assert "not-a-real-key-value" not in str(out)


async def test_a_response_that_declares_too_much_is_refused_before_it_is_read() -> None:
    """A feed serving gigabytes used to OOM-kill the scheduled job, leaving
    its run row "running" forever."""
    read = {"bytes": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = b"x" * 1024
        read["bytes"] += len(body)
        return httpx.Response(
            200, content=body, headers={"content-length": str(MAX_RESPONSE_BYTES + 1)}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), **http_kwargs()) as c:  # type: ignore[arg-type]
        with pytest.raises(ResponseTooLargeError):
            await fetch(c, "GET", "https://feed.example/atom")


async def test_a_response_that_declares_nothing_is_still_bounded() -> None:
    """The hostile shape: chunked, no content-length. The old guard read only
    the header, so this one was unbounded."""

    async def body() -> AsyncIterator[bytes]:
        for _ in range((MAX_RESPONSE_BYTES // 1024) + 2):
            yield b"x" * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), **http_kwargs()) as c:  # type: ignore[arg-type]
        with pytest.raises(ResponseTooLargeError):
            await fetch(c, "GET", "https://feed.example/atom")


async def test_a_malformed_content_length_is_refused_not_crashed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{}", headers={"content-length": "not-a-number"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), **http_kwargs()) as c:  # type: ignore[arg-type]
        with pytest.raises(ResponseTooLargeError):
            await fetch(c, "GET", "https://feed.example/atom")


async def test_an_ordinary_response_comes_back_whole() -> None:
    """And through the real client: the previous guard was a plain function in
    an async event hook, which raised TypeError on every single request."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"hits": [{"id": 1}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), **http_kwargs()) as c:  # type: ignore[arg-type]
        response = await fetch(c, "GET", "https://api.example/search")
    response.raise_for_status()
    assert response.json() == {"hits": [{"id": 1}]}


def test_too_large_is_reported_as_an_unavailable_source() -> None:
    """So one greedy feed makes the run partial, not crashed."""
    assert issubclass(ResponseTooLargeError, SourceUnavailableError)
