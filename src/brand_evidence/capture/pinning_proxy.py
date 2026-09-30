"""A local SOCKS5 proxy that makes the browser connect only where the guard said.

Route interception checks a URL, then Chromium resolves the name again to connect,
and a hostile DNS server can answer differently the second time. Chromium sends
the host name to a SOCKS5 proxy unresolved, so this proxy resolves it once,
refuses any non-public answer, and dials exactly the address it checked. Plain
HTTP, HTTPS and WebSockets all pass through it; TLS stays end to end.
"""

from __future__ import annotations

import ipaddress
import socket
import struct
import threading
from collections.abc import Callable

from brand_evidence.core.logging import get_logger
from brand_evidence.core.urlguard import (
    Address,
    Resolver,
    UnsafeUrlError,
    addresses_for,
    resolve_host,
)

log = get_logger(__name__)

Dial = Callable[[Address, int, float], socket.socket]

SOCKS_VERSION = 5
CMD_CONNECT = 1
ATYP_IPV4, ATYP_DOMAIN, ATYP_IPV6 = 1, 3, 4
REPLY_OK, REPLY_FAILURE, REPLY_NOT_ALLOWED, REPLY_UNREACHABLE = 0, 1, 2, 4
REPLY_BAD_COMMAND, REPLY_BAD_ADDRESS = 7, 8
CONNECT_TIMEOUT = 15.0
CHUNK = 65536


def _dial(address: Address, port: int, timeout: float) -> socket.socket:
    return socket.create_connection((str(address), port), timeout=timeout)


class PinningProxy:
    """Threaded, loopback-only, no authentication: it can only reach public addresses."""

    def __init__(self, resolver: Resolver = addresses_for, dial: Dial = _dial) -> None:
        self._resolver = resolver
        self._dial = dial
        self._lock = threading.Lock()
        self.blocked: list[str] = []
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(128)
        self._closed = False
        self._thread = threading.Thread(target=self._accept, name="pinning-proxy", daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return int(self._server.getsockname()[1])

    @property
    def server(self) -> str:
        return f"socks5://127.0.0.1:{self.port}"

    def close(self) -> None:
        self._closed = True
        try:
            self._server.close()
        except OSError:  # pragma: no cover - already closed
            pass

    def _accept(self) -> None:
        while not self._closed:
            try:
                client, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        try:
            client.settimeout(CONNECT_TIMEOUT)
            upstream = self._handshake(client)
            if upstream is None:
                return
            client.settimeout(None)
            upstream.settimeout(None)
            _pipe(client, upstream)
        except (OSError, ValueError):
            pass
        finally:
            for sock in (client, upstream):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:  # pragma: no cover
                        pass

    def _handshake(self, client: socket.socket) -> socket.socket | None:
        version, count = _recv_exact(client, 2)
        if version != SOCKS_VERSION:
            return None
        _recv_exact(client, count)
        client.sendall(b"\x05\x00")  # no authentication

        version, command, _, atyp = _recv_exact(client, 4)
        host = _read_host(client, atyp)
        (port,) = struct.unpack("!H", _recv_exact(client, 2))
        if command != CMD_CONNECT:
            _reply(client, REPLY_BAD_COMMAND)
            return None
        if host is None:
            _reply(client, REPLY_BAD_ADDRESS)
            return None

        try:
            addresses = resolve_host(host, self._resolver)
        except UnsafeUrlError as why:
            with self._lock:
                self.blocked.append(f"{host}:{port}")
            log.warning("capture_connection_refused", host=host, port=port, reason=str(why))
            _reply(client, REPLY_NOT_ALLOWED)
            return None
        for address in addresses:
            try:
                upstream = self._dial(address, port, CONNECT_TIMEOUT)
            except OSError:
                continue
            _reply(client, REPLY_OK)
            return upstream
        _reply(client, REPLY_UNREACHABLE if addresses else REPLY_FAILURE)
        return None


def _read_host(client: socket.socket, atyp: int) -> str | None:
    if atyp == ATYP_IPV4:
        return str(ipaddress.IPv4Address(_recv_exact(client, 4)))
    if atyp == ATYP_IPV6:
        return str(ipaddress.IPv6Address(_recv_exact(client, 16)))
    if atyp == ATYP_DOMAIN:
        (length,) = _recv_exact(client, 1)
        return _recv_exact(client, length).decode("idna")
    return None


def _reply(client: socket.socket, code: int) -> None:
    client.sendall(bytes([SOCKS_VERSION, code, 0, ATYP_IPV4]) + b"\x00" * 6)


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ValueError("connection closed mid-handshake")
        data += chunk
    return data


def _pipe(a: socket.socket, b: socket.socket) -> None:
    def forward(src: socket.socket, dst: socket.socket) -> None:
        try:
            while chunk := src.recv(CHUNK):
                dst.sendall(chunk)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    other = threading.Thread(target=forward, args=(b, a), daemon=True)
    other.start()
    forward(a, b)
    other.join()
