"""fetch_bounded checks every redirect hop and connects to the vetted IP."""
from __future__ import annotations

import socket

import httpx
import pytest

from openexecutive.monitoring.sources import _http
from openexecutive.monitoring.sources._http import (
    BlockedTargetError,
    fetch_bounded,
    validate_target_url,
)

# Public addresses for the made-up hostnames below. Anything not listed
# resolves the way an IP literal does: to itself.
DNS = {
    "feed.example.com": "93.184.215.14",
    "cdn.example.net": "151.101.1.69",
    "rebind.example.org": "10.0.0.7",
    "dual.example.com": ["2606:4700::6810:84e5", "104.16.132.229"],
    "xn--fa-hia.example": "93.184.215.14",
}


def _fake_getaddrinfo(host: str, port: object, *args: object, **kwargs: object) -> list:
    found = DNS.get(host, host)
    infos = []
    for addr in [found] if isinstance(found, str) else found:
        family = socket.AF_INET6 if ":" in addr else socket.AF_INET
        sockaddr = (addr, 0, 0, 0) if family == socket.AF_INET6 else (addr, 0)
        infos.append((family, socket.SOCK_STREAM, 6, "", sockaddr))
    return infos


class _Server:
    """Answers by Host header, records every request it was sent."""

    def __init__(self, routes: dict[str, httpx.Response]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = request.headers["host"] + request.url.path
        return self.routes.get(key, httpx.Response(404))


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)

    def install(routes: dict[str, httpx.Response]) -> _Server:
        srv = _Server(routes)
        monkeypatch.setattr(_http, "_transport", lambda: httpx.MockTransport(srv))
        return srv

    return install


def _redirect(location: str) -> httpx.Response:
    return httpx.Response(302, headers={"Location": location})


@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://[fdaa::3]:8080/internal",
        "http://100.100.100.200/latest/meta-data/",
        "http://rebind.example.org/",
        "file:///etc/passwd",
    ],
)
async def test_redirect_to_non_public_address_is_refused(server, location: str) -> None:
    srv = server({"feed.example.com/rss": _redirect(location)})

    with pytest.raises(BlockedTargetError):
        await fetch_bounded("https://feed.example.com/rss", 10_000)

    # Only the first, public hop was ever sent.
    assert len(srv.requests) == 1
    assert srv.requests[0].url.host == "93.184.215.14"


async def test_blocked_redirect_is_an_httpx_error(server) -> None:
    # Adapters catch httpx.HTTPError to log and skip the tick.
    server({"feed.example.com/rss": _redirect("http://127.0.0.1/")})
    with pytest.raises(httpx.HTTPError):
        await fetch_bounded("https://feed.example.com/rss", 10_000)


@pytest.mark.parametrize("location", ["http://[::1", "http://exa mple.com:99999/"])
async def test_malformed_redirect_is_an_httpx_error(server, location: str) -> None:
    # Adapters log and skip httpx.HTTPError; a bad Location must land there.
    srv = server({"feed.example.com/rss": _redirect(location)})
    with pytest.raises(httpx.HTTPError):
        await fetch_bounded("https://feed.example.com/rss", 10_000)
    assert len(srv.requests) == 1


async def test_first_hop_is_checked_too(server) -> None:
    srv = server({})
    with pytest.raises(BlockedTargetError):
        await fetch_bounded("http://169.254.169.254/latest/meta-data/", 10_000)
    assert srv.requests == []


async def test_public_redirect_is_followed_on_the_pinned_ip(server) -> None:
    srv = server({
        "feed.example.com/rss": _redirect("https://cdn.example.net/feed.xml?sig=1"),
        "cdn.example.net/feed.xml": httpx.Response(200, content=b"<rss/>"),
    })

    body = await fetch_bounded("https://feed.example.com/rss", 10_000)

    assert body == b"<rss/>"
    first, second = srv.requests
    assert first.url.host == "93.184.215.14"
    assert first.headers["host"] == "feed.example.com"
    assert first.extensions["sni_hostname"] == "feed.example.com"
    assert second.url.host == "151.101.1.69"
    assert second.headers["host"] == "cdn.example.net"
    assert second.extensions["sni_hostname"] == "cdn.example.net"
    assert second.url.query == b"sig=1"


async def test_relative_redirect_keeps_host_and_port(server) -> None:
    srv = server({
        "feed.example.com:8443/old": _redirect("/new"),
        "feed.example.com:8443/new": httpx.Response(200, content=b"ok"),
    })

    assert await fetch_bounded("https://feed.example.com:8443/old", 100) == b"ok"
    assert srv.requests[1].url.port == 8443
    assert srv.requests[1].headers["host"] == "feed.example.com:8443"


async def test_plain_http_sends_no_server_name(server) -> None:
    srv = server({"feed.example.com/": httpx.Response(200, content=b"ok")})
    await fetch_bounded("http://feed.example.com/", 100)
    assert "sni_hostname" not in srv.requests[0].extensions


async def test_next_vetted_address_is_tried_when_connect_fails(server) -> None:
    srv = server({"dual.example.com/": httpx.Response(200, content=b"ok")})
    inner = srv.__call__

    def no_ipv6_route(request: httpx.Request) -> httpx.Response:
        if ":" in request.url.host:
            srv.requests.append(request)
            raise httpx.ConnectError("unreachable", request=request)
        return inner(request)

    srv.__call__ = no_ipv6_route  # type: ignore[method-assign]
    _http_transport = httpx.MockTransport(no_ipv6_route)
    server_patch = pytest.MonkeyPatch()
    server_patch.setattr(_http, "_transport", lambda: _http_transport)
    try:
        assert await fetch_bounded("https://dual.example.com/", 100) == b"ok"
    finally:
        server_patch.undo()
    assert [r.url.host for r in srv.requests] == ["2606:4700::6810:84e5", "104.16.132.229"]


async def test_idn_host_uses_the_encoded_name(server) -> None:
    srv = server({"xn--fa-hia.example/": httpx.Response(200, content=b"ok")})
    await fetch_bounded("https://faß.example/", 100)
    assert srv.requests[0].headers["host"] == "xn--fa-hia.example"
    assert srv.requests[0].extensions["sni_hostname"] == "xn--fa-hia.example"


async def test_redirect_cap(server) -> None:
    routes = {
        f"feed.example.com/{i}": _redirect(f"/{i + 1}")
        for i in range(_http.MAX_REDIRECTS + 1)
    }
    srv = server(routes)

    with pytest.raises(httpx.TooManyRedirects):
        await fetch_bounded("https://feed.example.com/0", 100)
    assert len(srv.requests) == _http.MAX_REDIRECTS + 1


async def test_body_cap_still_applies(server) -> None:
    server({"feed.example.com/": httpx.Response(200, content=b"x" * 50)})
    with pytest.raises(_http.FetchOverflowError):
        await fetch_bounded("https://feed.example.com/", 10)


async def test_error_status_still_raises(server) -> None:
    server({"feed.example.com/": httpx.Response(503)})
    with pytest.raises(httpx.HTTPStatusError):
        await fetch_bounded("https://feed.example.com/", 10)


def test_validate_target_url_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)
    assert validate_target_url("https://feed.example.com/") == (True, "")
    ok, reason = validate_target_url("http://[fdaa::3]/")
    assert not ok and "non-public" in reason
    ok, reason = validate_target_url("ftp://feed.example.com/")
    assert not ok and "scheme" in reason
