"""The browser may only reach what the guard allowed, on every request.

A local HTTP server plays both the public site and the internal service. A fake
resolver decides which names are "public", and the proxy's dialer sends the
fake public address to the local server, so nothing leaves the machine.
"""

from __future__ import annotations

import ipaddress
import socket
import struct
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from brand_evidence.capture.pinning_proxy import PinningProxy
from brand_evidence.capture.screenshot import Capturer, RequestGuard
from brand_evidence.core.urlguard import Address, UnsafeUrlError

PUBLIC = ipaddress.ip_address("93.184.216.34")
_REAL_CONNECT = socket.socket.connect
_REAL_CREATE_CONNECTION = socket.create_connection
METADATA = "http://169.254.169.254/latest/meta-data/"


class Site:
    def __init__(self) -> None:
        self.hits: list[str] = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                site.hits.append(self.path)
                port = site.port
                pages = {
                    "/start": (302, {"Location": f"http://127.0.0.1:{port}/secret"}, ""),
                    "/to-internal": (302, {"Location": f"http://internal.test:{port}/secret"}, ""),
                    "/frames": (200, {}, f'<p>public page</p><iframe src="{METADATA}"></iframe>'),
                    "/rebind": (
                        200,
                        {},
                        f'<p>public page</p><iframe src="http://rebind.test:{port}/secret">',
                    ),
                    "/secret": (200, {}, "<p>internal secret</p>"),
                }
                status, headers, body = pages.get(self.path, (404, {}, "missing"))
                data = body.encode()
                self.send_response(status)
                for name, value in {**headers, "Content-Type": "text/html"}.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def dial(self, address: Address, port: int, timeout: float) -> socket.socket:
        # Only the fake public address is routable, and it routes here.
        if address != PUBLIC:
            raise OSError("unroutable in tests")
        return socket.create_connection(("127.0.0.1", self.port), timeout=timeout)


class Resolver:
    """public.test is public; internal.test is private; rebind.test flips."""

    def __init__(self) -> None:
        self.rebind_calls = 0

    def __call__(self, host: str) -> list[Address]:
        if host == "rebind.test":
            self.rebind_calls += 1
            first = self.rebind_calls == 1
            return [PUBLIC if first else ipaddress.ip_address("127.0.0.1")]
        if host == "internal.test":
            return [ipaddress.ip_address("10.0.0.5")]
        return [PUBLIC]


@pytest.fixture(autouse=True)
def _loopback_only(_no_network: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Still no network: the local server and the proxy, nothing else."""

    def connect(sock: socket.socket, address: Any) -> None:
        if not (isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1")):
            raise RuntimeError("network access attempted during tests")
        _REAL_CONNECT(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "create_connection", _REAL_CREATE_CONNECTION)


@pytest.fixture
def site() -> Iterator[Site]:
    s = Site()
    yield s
    s.server.shutdown()


# --- the proxy, no browser needed ------------------------------------------


def _socks_connect(proxy: PinningProxy, host: str, port: int) -> tuple[int, socket.socket]:
    sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
    sock.sendall(b"\x05\x01\x00")
    assert sock.recv(2) == b"\x05\x00"
    name = host.encode()
    sock.sendall(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + struct.pack("!H", port))
    reply = sock.recv(10)
    return reply[1], sock


def test_the_proxy_dials_the_address_it_checked(site: Site) -> None:
    proxy = PinningProxy(Resolver(), site.dial)
    try:
        code, sock = _socks_connect(proxy, "public.test", site.port)
        assert code == 0
        sock.sendall(b"GET /secret HTTP/1.0\r\nHost: public.test\r\n\r\n")
        received = b""
        while chunk := sock.recv(4096):
            received += chunk
        sock.close()
        assert b"internal secret" in received
    finally:
        proxy.close()


@pytest.mark.parametrize("host", ["internal.test", "localhost", "169.254.169.254"])
def test_the_proxy_refuses_internal_destinations(site: Site, host: str) -> None:
    proxy = PinningProxy(Resolver(), site.dial)
    try:
        code, sock = _socks_connect(proxy, host, 80)
        sock.close()
        assert code == 2  # connection not allowed by ruleset
        assert proxy.blocked == [f"{host}:80"]
    finally:
        proxy.close()


def test_the_proxy_resolves_again_and_refuses_a_rebound_name(site: Site) -> None:
    """The route check saw the first answer; the proxy sees the second."""
    resolver = Resolver()
    guard = RequestGuard(resolver)
    guard.check(f"http://rebind.test:{site.port}/secret")
    proxy = PinningProxy(resolver, site.dial)
    try:
        code, sock = _socks_connect(proxy, "rebind.test", site.port)
        sock.close()
        assert code == 2
    finally:
        proxy.close()
    assert "/secret" not in site.hits


# --- the route guard, no browser needed ------------------------------------


class FakeRoute:
    def __init__(self) -> None:
        self.outcome = ""

    def continue_(self) -> None:
        self.outcome = "continue"

    def abort(self, code: str) -> None:
        self.outcome = f"abort:{code}"


class FakeRequest:
    def __init__(self, url: str, navigation: bool = False) -> None:
        self.url = url
        self._navigation = navigation
        self.frame = type("F", (), {"parent_frame": None})()

    def is_navigation_request(self) -> bool:
        return self._navigation


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/",
        METADATA,
        "http://10.1.2.3/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://internal.test/",
        "file:///etc/passwd",
    ],
)
def test_the_route_guard_aborts_internal_requests(url: str) -> None:
    guard, route = RequestGuard(Resolver()), FakeRoute()
    guard.handle(route, FakeRequest(url, navigation=True))
    assert route.outcome == "abort:blockedbyclient"
    assert guard.blocked == [url]
    assert guard.blocked_navigation is not None


def test_the_route_guard_lets_public_and_inline_requests_through() -> None:
    guard = RequestGuard(Resolver())
    for url in ("https://public.test/app.js", "data:image/png;base64,AAAA"):
        route = FakeRoute()
        guard.handle(route, FakeRequest(url))
        assert route.outcome == "continue"
    assert guard.blocked == []


# --- through a real Chromium -------------------------------------------------


def _chromium_available() -> bool:
    try:
        from pathlib import Path

        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            return Path(pw.chromium.executable_path).exists()
    except Exception:  # noqa: BLE001 - any failure means no browser
        return False


needs_chromium = pytest.mark.skipif(
    not _chromium_available(), reason="Playwright Chromium not installed"
)


@pytest.fixture
def capturer(site: Site) -> Iterator[Capturer]:
    with Capturer(timeout_ms=10_000, resolver=Resolver(), dial=site.dial) as c:
        yield c


@needs_chromium
def test_a_redirect_to_loopback_is_not_captured(site: Site, capturer: Capturer) -> None:
    with pytest.raises(UnsafeUrlError, match=r"127\.0\.0\.1"):
        capturer.capture_url(f"http://public.test:{site.port}/start")
    assert site.hits == ["/start"]


@needs_chromium
def test_a_redirect_to_an_internal_name_is_not_captured(site: Site, capturer: Capturer) -> None:
    with pytest.raises(UnsafeUrlError, match=r"internal\.test"):
        capturer.capture_url(f"http://public.test:{site.port}/to-internal")
    assert "/secret" not in site.hits


@needs_chromium
def test_an_iframe_at_the_metadata_address_is_blocked(site: Site, capturer: Capturer) -> None:
    result = capturer.capture_url(f"http://public.test:{site.port}/frames")
    assert b"public page" in result.html
    assert METADATA in result.meta["blocked_requests"]


@needs_chromium
def test_a_rebound_iframe_never_reaches_the_internal_page(
    site: Site, capturer: Capturer
) -> None:
    """Public when the route checked it, loopback when Chromium connected: the
    proxy's own resolution is the one that counts."""
    result = capturer.capture_url(f"http://public.test:{site.port}/rebind")
    assert b"internal secret" not in result.html
    assert "/secret" not in site.hits
    assert f"connect rebind.test:{site.port}" in result.meta["blocked_requests"]
