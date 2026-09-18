"""Small builders for rows and fake collaborators."""

from __future__ import annotations

import hashlib
from datetime import datetime

from brand_evidence.capture.archiver import HistoricalSnapshot, SubmitResult
from brand_evidence.capture.screenshot import CaptureResult
from brand_evidence.capture.timestamp import TimestampToken
from brand_evidence.core.clock import now_iso
from brand_evidence.sources.base import RawHit, SourceUnavailableError


class FakeCapturer:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def capture_url(self, url: str, *, allow_local: bool = False) -> CaptureResult:
        self.calls.append(url)
        return CaptureResult(
            url=url,
            final_url=url,
            png=b"\x89PNG fake " + url.encode(),
            pdf=b"%PDF fake " + url.encode(),
            html=f"<html><body>{url}</body></html>".encode(),
            captured_at=now_iso(),
            meta={
                "viewport": {"width": 1440, "height": 900},
                "authenticated": False,
                "playwright_version": "test",
            },
        )


class FakeTSA:
    url = "https://tsa.example/tsr"

    def __init__(self) -> None:
        self.stamped: list[bytes] = []

    def stamp(self, data: bytes) -> TimestampToken:
        self.stamped.append(data)
        return TimestampToken(
            tsr=b"TSR:" + hashlib.sha256(data).digest(),
            gen_time="2026-09-15T04:00:00.000000+00:00",
            digest_hex=hashlib.sha256(data).hexdigest(),
            tsa_url=self.url,
            serial="42",
            policy="1.2.3",
        )


class FakeArchive:
    name = "wayback"

    def __init__(self, *, confirm_on_submit: bool = False, fail: bool = False) -> None:
        self.confirm_on_submit = confirm_on_submit
        self.fail = fail
        self.submitted: list[str] = []
        self.checks = 0
        self.check_result: str | None = None
        self.historical: list[HistoricalSnapshot] = []

    def submit(self, url: str) -> SubmitResult:
        if self.fail:
            raise ConnectionError("archive unreachable")
        self.submitted.append(url)
        snap = (
            f"https://web.archive.org/web/20260914000000/{url}" if self.confirm_on_submit else None
        )
        return SubmitResult(
            request_url=f"https://web.archive.org/save/{url}",
            snapshot_url=snap,
            provider_ref=f"spn2-{len(self.submitted)}",
        )

    def history(self, url: str) -> list[HistoricalSnapshot]:
        if self.fail:
            raise ConnectionError("archive unreachable")
        return self.historical

    def check(self, url: str, submitted_at: str, provider_ref: str | None) -> str | None:
        self.checks += 1
        self.last_ref = provider_ref
        self.checked_since: list[str] = getattr(self, "checked_since", [])
        self.checked_since.append(submitted_at)
        return self.check_result


class StaticSource:
    def __init__(self, name: str, hits: list[RawHit]) -> None:
        self.name = name
        self.hits = hits

    async def search(self, terms: list[str], since: datetime) -> list[RawHit]:
        return self.hits


class BrokenSource:
    name = "broken"

    async def search(self, terms: list[str], since: datetime) -> list[RawHit]:
        raise SourceUnavailableError("broken: connection refused")


def hit(url: str, title: str = "Northwind mentioned", terms: list[str] | None = None) -> RawHit:
    return RawHit(
        url=url, title=title, excerpt="... Northwind ...", matched_terms=terms or ["Northwind"]
    )
