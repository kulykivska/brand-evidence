"""Third-party archive submission and polling.

Provider "wayback": Internet Archive Save Page Now (anonymous GET /save/<url>) plus the
Availability API for confirmation. The request is recorded as `pending` immediately; a
separate archive-poll job confirms it with exponential backoff (1m, 5m, 30m, 2h, 12h), max 6.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import quote

import httpx

from brand_evidence.core.clock import now_iso, parse_iso
from brand_evidence.core.logging import get_logger
from brand_evidence.sources.base import user_agent

log = get_logger(__name__)

BACKOFF = [
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=30),
    timedelta(hours=2),
    timedelta(hours=12),
]
MAX_ATTEMPTS = 6


@dataclass(frozen=True)
class SubmitResult:
    request_url: str
    snapshot_url: str | None  # set when the provider confirms synchronously
    provider_ref: str | None = None  # provider job id, used by the poller


@dataclass(frozen=True)
class HistoricalSnapshot:
    snapshot_url: str
    captured_at: str  # ISO-8601 UTC, from the archive's own timestamp
    provider_ref: str  # the archive's timestamp key


class ArchiveProvider(Protocol):
    name: str

    def submit(self, url: str) -> SubmitResult: ...

    def check(self, url: str, submitted_at: str, provider_ref: str | None) -> str | None: ...

    def history(self, url: str) -> list[HistoricalSnapshot]: ...


class WaybackProvider:
    """Save Page Now 2 (authenticated). Anonymous saves stopped working in 2026."""

    name = "wayback"
    save_endpoint = "https://web.archive.org/save"
    availability = "https://archive.org/wayback/available"

    def __init__(
        self, access_key: str, secret_key: str, client: httpx.Client | None = None
    ) -> None:
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(90.0),
            follow_redirects=False,
            headers={"User-Agent": user_agent()},
        )
        self._auth = {
            "Authorization": f"LOW {access_key}:{secret_key}",
            "Accept": "application/json",
        }

    def submit(self, url: str) -> SubmitResult:
        response = self.client.post(
            self.save_endpoint, data={"url": url, "skip_first_archive": "1"}, headers=self._auth
        )
        request_url = f"{self.save_endpoint}/{url}"
        if response.status_code >= 400:
            log.warning("archive_submit_http_error", status=response.status_code, url=url)
            response.raise_for_status()
        data = response.json()
        job_id = data.get("job_id")
        if not job_id:
            raise RuntimeError(f"Save Page Now returned no job_id: {data.get('message', data)}")
        return SubmitResult(request_url=request_url, snapshot_url=None, provider_ref=str(job_id))

    def check(self, url: str, submitted_at: str, provider_ref: str | None) -> str | None:
        """Return the snapshot URL once the SPN2 job succeeded, else None."""
        if provider_ref:
            response = self.client.get(
                f"{self.save_endpoint}/status/{provider_ref}", headers=self._auth
            )
            response.raise_for_status()
            data = response.json()
            if data.get("status") == "success" and data.get("timestamp"):
                original = data.get("original_url", url)
                return f"https://web.archive.org/web/{data['timestamp']}/{original}"
            if data.get("status") == "error":
                log.warning("archive_job_error", job=provider_ref, message=data.get("message"))
            return None
        return self._check_availability(url, submitted_at)

    cdx_endpoint = "https://web.archive.org/cdx/search/cdx"

    def history(self, url: str) -> list[HistoricalSnapshot]:
        """Snapshots the archive already holds for this URL, oldest first. No auth needed."""
        response = self.client.get(
            self.cdx_endpoint,
            params={
                "url": url,
                "output": "json",
                "fl": "timestamp,original,statuscode",
                "filter": "statuscode:200",
                "collapse": "timestamp:8",
                "limit": "500",
            },
            timeout=60.0,
        )
        response.raise_for_status()
        rows = response.json() if response.content else []
        found: list[HistoricalSnapshot] = []
        for row in rows[1:]:  # first row is the header
            stamp, original = row[0], row[1]
            captured = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
            found.append(
                HistoricalSnapshot(
                    snapshot_url=f"https://web.archive.org/web/{stamp}/{original}",
                    captured_at=captured.isoformat(timespec="microseconds"),
                    provider_ref=stamp,
                )
            )
        return found

    def _check_availability(self, url: str, submitted_at: str) -> str | None:
        response = self.client.get(
            self.availability, params={"url": url, "timestamp": _wb_timestamp(submitted_at)}
        )
        response.raise_for_status()
        closest = (response.json().get("archived_snapshots") or {}).get("closest") or {}
        if not closest.get("available"):
            return None
        stamp = closest.get("timestamp", "")
        if stamp and stamp < _wb_timestamp(submitted_at)[: len(stamp)]:
            return None
        return str(closest["url"]) if closest.get("url") else None


def _wb_timestamp(iso: str) -> str:
    return parse_iso(iso).strftime("%Y%m%d%H%M%S")


def make_provider(name: str, access_key: str = "", secret_key: str = "") -> ArchiveProvider:
    if name == "wayback":
        return WaybackProvider(access_key, secret_key)
    raise ValueError(f"unknown archive provider: {name}")


def next_attempt_due(submitted_at: str, attempts: int) -> str:
    """ISO time after which attempt number `attempts + 1` may run."""
    delay = sum(BACKOFF[: min(attempts, len(BACKOFF))], timedelta())
    return (parse_iso(submitted_at) + delay).isoformat(timespec="microseconds")


class RateLimiter:
    """Blocks so that successive calls are at least `interval` seconds apart."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._last = 0.0

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last
        if self._last and elapsed < self.interval:
            time.sleep(self.interval - elapsed)
        self._last = time.monotonic()


def quote_url(url: str) -> str:
    return quote(url, safe=":/?&=%")


__all__ = [
    "ArchiveProvider",
    "WaybackProvider",
    "SubmitResult",
    "make_provider",
    "next_attempt_due",
    "RateLimiter",
    "MAX_ATTEMPTS",
    "BACKOFF",
    "now_iso",
]
