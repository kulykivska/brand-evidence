"""Playwright capture: full-page PNG, PDF and raw HTML with a deterministic viewport.

The push path is exempt from robots.txt (owner's own content); the crawler checks it before
calling capture_url. No cookies or storage state are ever loaded here (§6).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.metadata import version as pkg_version
from typing import Any

from brand_evidence.core.clock import now_iso
from brand_evidence.core.logging import get_logger
from brand_evidence.core.urlguard import check_url

log = get_logger(__name__)

# Heuristics for "the anonymous visitor got a wall instead of the content".
LOGIN_WALL_MARKERS = ("/login", "/authwall", "/checkpoint", "accounts.google.com", "/signup")


@dataclass
class CaptureResult:
    url: str
    final_url: str
    png: bytes
    pdf: bytes
    html: bytes
    captured_at: str
    meta: dict[str, Any] = field(default_factory=dict)


class Capturer:
    def __init__(
        self,
        viewport_width: int = 1440,
        viewport_height: int = 900,
        device_scale_factor: int = 2,
        timeout_ms: int = 45_000,
    ) -> None:
        self.viewport = {"width": viewport_width, "height": viewport_height}
        self.dsf = device_scale_factor
        self.timeout_ms = timeout_ms
        self._pw: Any = None
        self._browser: Any = None

    def _browser_instance(self) -> Any:
        """One chromium for the life of this capturer.

        A run used to launch and tear down a browser per URL, roughly a second
        of process start each. The sync API is single-threaded, as every caller
        here is; each page still gets its own fresh context.
        """
        if self._browser is not None and not self._browser.is_connected():
            # A chromium killed mid-run (OOM on a heavy page) would otherwise
            # fail every remaining capture and burn their retry budget.
            log.warning("browser_gone_relaunching")
            self.close()
        if self._browser is None:
            from playwright.sync_api import sync_playwright

            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
        return self._browser

    def close(self) -> None:
        """Idempotent: the capturer is usable again afterwards."""
        try:
            if self._browser is not None:
                self._browser.close()
        finally:
            self._browser = None
            if self._pw is not None:
                self._pw.stop()
                self._pw = None

    def __enter__(self) -> Capturer:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def capture_url(self, url: str, *, allow_local: bool = False) -> CaptureResult:
        """Capture one URL. ``allow_local`` opts out of the safety check and
        belongs only to callers supplying a URL they wrote themselves."""
        # Crawled URLs come from search results and feeds, so whoever wrote
        # one chooses what the browser opens: file:// would read this machine
        # into the evidence store, a private address would reach an internal
        # service. Refuse both before launching anything.
        if not allow_local:
            check_url(url)

        browser = self._browser_instance()
        # A fresh context per page: no cookies or storage carry from the page
        # captured before this one.
        context = browser.new_context(
            viewport=self.viewport,
            device_scale_factor=self.dsf,
            locale="en-US",
            timezone_id="UTC",
        )
        try:
            page = context.new_page()
            response = page.goto(url, wait_until="networkidle", timeout=self.timeout_ms)
            page.wait_for_timeout(1500)
            captured_at = now_iso()
            html = page.content().encode("utf-8")
            png = page.screenshot(full_page=True, type="png")
            page.emulate_media(media="screen")
            pdf = page.pdf(print_background=True, prefer_css_page_size=False, format="A4")
            final_url = page.url
            status = response.status if response else None
            ua = page.evaluate("navigator.userAgent")
            meta = {
                "viewport": self.viewport,
                "device_scale_factor": self.dsf,
                "user_agent": ua,
                "playwright_version": pkg_version("playwright"),
                "browser": browser.browser_type.name,
                "browser_version": browser.version,
                "http_status": status,
                "final_url": final_url,
                "authenticated": False,
                "login_wall_suspected": _looks_like_login_wall(url, final_url, status),
            }
        finally:
            context.close()
        log.info("captured", url=url, final_url=final_url, status=meta["http_status"])
        return CaptureResult(url, final_url, png, pdf, html, captured_at, meta)


def _looks_like_login_wall(url: str, final_url: str, status: int | None) -> bool:
    if status is not None and status in (401, 403, 999):
        return True
    if final_url != url and any(marker in final_url.lower() for marker in LOGIN_WALL_MARKERS):
        return True
    return False
