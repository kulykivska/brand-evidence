"""The safety rules that protect the machine and the credentials."""

from __future__ import annotations

import pytest

from brand_evidence.core.logging import _mask
from brand_evidence.core.urlguard import UnsafeUrlError, check_url, is_safe


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
