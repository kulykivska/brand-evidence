import pytest

from brand_evidence.core.urlnorm import normalize_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "https://WWW.Example.com/Path/?utm_source=x&b=2&a=1#frag",
            "https://example.com/Path?a=1&b=2",
        ),
        ("http://example.com:80/", "http://example.com/"),
        ("https://example.com/post/?fbclid=abc", "https://example.com/post"),
        ("https://example.com/a?ref=tw&keep=1", "https://example.com/a?keep=1"),
        ("https://example.com", "https://example.com/"),
    ],
)
def test_normalize(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


def test_idempotent() -> None:
    once = normalize_url("https://Example.com/x/?utm_medium=a&q=1")
    assert normalize_url(once) == once
