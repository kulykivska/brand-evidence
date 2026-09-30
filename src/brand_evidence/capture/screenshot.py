"""Playwright capture: full-page PNG, PDF and raw HTML with a deterministic viewport.

The push path is exempt from robots.txt (owner's own content); the crawler checks it before
calling capture_url. No cookies or storage state are ever loaded here (§6).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.metadata import version as pkg_version
from typing import Any
from urllib.parse import urlsplit

from brand_evidence.capture.pinning_proxy import Dial, PinningProxy
from brand_evidence.core.clock import now_iso
from brand_evidence.core.logging import get_logger
from brand_evidence.core.urlguard import (
    Resolver,
    UnsafeUrlError,
    addresses_for,
    check_url,
    resolve_host,
)

log = get_logger(__name__)

# Schemes a page may load without touching the network.
LOCAL_SCHEMES = frozenset({"data", "blob", "about"})
# WebRTC can open UDP to any address, around the proxy; keep it on the proxy.
CHROMIUM_ARGS = ["--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]

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


class RequestGuard:
    """Checks every request a page routes: navigations, frames, fetch, XHR.

    Playwright does not route redirect hops, and a name can re-resolve after the
    check; the pinning proxy covers both, since every connection goes through it.
    """

    def __init__(self, resolver: Resolver = addresses_for) -> None:
        self._resolver = resolver
        self._public_hosts: set[str] = set()
        self.blocked: list[str] = []
        self.blocked_navigation: str | None = None

    def check(self, url: str) -> None:
        check_url(url, resolve=False)
        host = (urlsplit(url).hostname or "").lower()
        if host not in self._public_hosts:
            resolve_host(host, self._resolver)
            self._public_hosts.add(host)

    def handle(self, route: Any, request: Any) -> None:
        url = request.url
        if urlsplit(url).scheme.lower() in LOCAL_SCHEMES:
            route.continue_()
            return
        try:
            self.check(url)
        except UnsafeUrlError as why:
            self.blocked.append(url)
            if _is_main_frame_navigation(request):
                self.blocked_navigation = f"{url}: {why}"
            log.warning("capture_request_refused", url=url, reason=str(why))
            route.abort("blockedbyclient")
            return
        route.continue_()


def _is_main_frame_navigation(request: Any) -> bool:
    try:
        return bool(request.is_navigation_request() and request.frame.parent_frame is None)
    except Exception:  # noqa: BLE001 - service worker requests have no frame
        return False


class Capturer:
    def __init__(
        self,
        viewport_width: int = 1440,
        viewport_height: int = 900,
        device_scale_factor: int = 2,
        timeout_ms: int = 45_000,
        *,
        resolver: Resolver = addresses_for,
        dial: Dial | None = None,
    ) -> None:
        self.viewport = {"width": viewport_width, "height": viewport_height}
        self.dsf = device_scale_factor
        self.timeout_ms = timeout_ms
        self._resolver = resolver
        self._dial = dial
        self._pw: Any = None
        self._browser: Any = None
        self._proxy: PinningProxy | None = None

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
            self._browser = self._pw.chromium.launch(headless=True, args=CHROMIUM_ARGS)
        return self._browser

    def _pinning_proxy(self) -> PinningProxy:
        if self._proxy is None:
            if self._dial is None:
                self._proxy = PinningProxy(self._resolver)
            else:
                self._proxy = PinningProxy(self._resolver, self._dial)
        return self._proxy

    def close(self) -> None:
        """Idempotent: the capturer is usable again afterwards."""
        try:
            if self._browser is not None:
                self._browser.close()
        finally:
            self._browser = None
            if self._proxy is not None:
                self._proxy.close()
                self._proxy = None
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
        guard = None if allow_local else RequestGuard(self._resolver)
        if guard is not None:
            guard.check(url)

        browser = self._browser_instance()
        options: dict[str, Any] = {}
        proxy_blocked_before = 0
        if guard is not None:
            proxy = self._pinning_proxy()
            proxy_blocked_before = len(proxy.blocked)
            # "<-loopback>" stops Chromium sending localhost around the proxy.
            options["proxy"] = {"server": proxy.server, "bypass": "<-loopback>"}
            # Service worker fetches skip page routes, so there are none.
            options["service_workers"] = "block"
        # A fresh context per page: no cookies or storage carry from the page
        # captured before this one.
        context = browser.new_context(
            viewport=self.viewport,
            device_scale_factor=self.dsf,
            locale="en-US",
            timezone_id="UTC",
            **options,
        )
        try:
            if guard is not None:
                context.route("**/*", guard.handle)
            page = context.new_page()
            try:
                response = page.goto(url, wait_until="networkidle", timeout=self.timeout_ms)
            except Exception as exc:
                if guard is not None and guard.blocked_navigation:
                    raise UnsafeUrlError(f"navigation refused: {guard.blocked_navigation}") from exc
                # Routes never see redirect hops; the proxy does, and refused one.
                refused = self._proxy.blocked[proxy_blocked_before:] if self._proxy else []
                if guard is not None and refused:
                    raise UnsafeUrlError(f"connection refused: {', '.join(refused)}") from exc
                raise
            page.wait_for_timeout(1500)
            final_url = page.url
            if guard is not None:
                # A late redirect or script navigation lands somewhere else.
                if guard.blocked_navigation:
                    raise UnsafeUrlError(f"navigation refused: {guard.blocked_navigation}")
                guard.check(final_url)
            captured_at = now_iso()
            html = page.content().encode("utf-8")
            png = page.screenshot(full_page=True, type="png")
            page.emulate_media(media="screen")
            pdf = page.pdf(print_background=True, prefer_css_page_size=False, format="A4")
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
            if guard is not None and self._proxy is not None:
                meta["blocked_requests"] = guard.blocked + [
                    f"connect {target}" for target in self._proxy.blocked[proxy_blocked_before:]
                ]
        finally:
            if guard is not None:
                # Handlers still in flight would otherwise error on the closed context.
                context.unroute_all(behavior="ignoreErrors")
            context.close()
        log.info("captured", url=url, final_url=final_url, status=meta["http_status"])
        return CaptureResult(url, final_url, png, pdf, html, captured_at, meta)


def _looks_like_login_wall(url: str, final_url: str, status: int | None) -> bool:
    if status is not None and status in (401, 403, 999):
        return True
    if final_url != url and any(marker in final_url.lower() for marker in LOGIN_WALL_MARKERS):
        return True
    return False
