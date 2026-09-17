from __future__ import annotations

import httpx
import pytest
import respx

from brand_evidence.capture.archiver import WaybackProvider, next_attempt_due


def test_backoff_schedule() -> None:
    t0 = "2026-09-14T00:00:00+00:00"
    assert next_attempt_due(t0, 0) == "2026-09-14T00:00:00.000000+00:00"
    assert next_attempt_due(t0, 1) == "2026-09-14T00:01:00.000000+00:00"
    assert next_attempt_due(t0, 2) == "2026-09-14T00:06:00.000000+00:00"
    assert next_attempt_due(t0, 3) == "2026-09-14T00:36:00.000000+00:00"
    assert next_attempt_due(t0, 4) == "2026-09-14T02:36:00.000000+00:00"
    assert next_attempt_due(t0, 5) == "2026-09-14T14:36:00.000000+00:00"
    assert next_attempt_due(t0, 6) == next_attempt_due(t0, 5)


AUTH = {"Authorization": "LOW ak:sk"}


@respx.mock
def test_spn2_submit_returns_job_id() -> None:
    route = respx.post("https://web.archive.org/save").mock(
        return_value=httpx.Response(
            200, json={"url": "https://example.com/p", "job_id": "spn2-abc"}
        )
    )
    provider = WaybackProvider("ak", "sk", httpx.Client())
    result = provider.submit("https://example.com/p")
    assert result.provider_ref == "spn2-abc" and result.snapshot_url is None
    assert route.calls[0].request.headers["Authorization"] == "LOW ak:sk"


@respx.mock
def test_spn2_submit_without_login_raises() -> None:
    respx.post("https://web.archive.org/save").mock(
        return_value=httpx.Response(401, json={"message": "You need to be logged in"})
    )
    with pytest.raises(httpx.HTTPStatusError):
        WaybackProvider("ak", "sk", httpx.Client()).submit("https://example.com/p")


@respx.mock
def test_spn2_status_pending_then_success() -> None:
    route = respx.get("https://web.archive.org/save/status/spn2-abc")
    route.side_effect = [
        httpx.Response(200, json={"status": "pending"}),
        httpx.Response(
            200,
            json={
                "status": "success",
                "timestamp": "20260914120000",
                "original_url": "https://example.com/p",
            },
        ),
    ]
    provider = WaybackProvider("ak", "sk", httpx.Client())
    assert provider.check("https://example.com/p", "2026-09-14T00:00:00+00:00", "spn2-abc") is None
    assert provider.check("https://example.com/p", "2026-09-14T00:00:00+00:00", "spn2-abc") == (
        "https://web.archive.org/web/20260914120000/https://example.com/p"
    )


@respx.mock
def test_availability_fallback_rejects_snapshots_older_than_submission() -> None:
    respx.get("https://archive.org/wayback/available").mock(
        return_value=httpx.Response(
            200,
            json={
                "archived_snapshots": {
                    "closest": {
                        "available": True,
                        "timestamp": "20200101000000",
                        "url": "https://web.archive.org/web/20200101000000/https://example.com/p",
                    }
                }
            },
        )
    )
    provider = WaybackProvider("ak", "sk", httpx.Client())
    assert provider.check("https://example.com/p", "2026-09-14T00:00:00+00:00", None) is None


@respx.mock
def test_availability_fallback_accepts_new_snapshot() -> None:
    respx.get("https://archive.org/wayback/available").mock(
        return_value=httpx.Response(
            200,
            json={
                "archived_snapshots": {
                    "closest": {
                        "available": True,
                        "timestamp": "20260914120000",
                        "url": "https://web.archive.org/web/20260914120000/https://example.com/p",
                    }
                }
            },
        )
    )
    provider = WaybackProvider("ak", "sk", httpx.Client())
    assert provider.check("https://example.com/p", "2026-09-14T00:00:00+00:00", None) is not None
