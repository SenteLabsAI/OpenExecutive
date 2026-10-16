"""Shared HTTP + URL safety helpers for monitoring source adapters.

Every adapter that hits an external URL goes through these helpers so
the SSRF guard, body-size cap, redirect cap, and query-string strip are
applied once and consistently. New adapters MUST NOT use ``httpx``
directly — call :func:`fetch_bounded` instead.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

import httpx

# Schemes a watchlist target is allowed to use. Excludes file://, ftp://,
# data:, javascript:, etc. so a misconfigured row can't read local files
# or invoke unexpected protocol handlers.
ALLOWED_SCHEMES = frozenset({"http", "https"})

# Tight redirect cap — a malicious feed shouldn't be able to pull
# megabytes of data via a long 30x chain. Three is enough for any real
# CDN setup without giving the attacker enough rope.
MAX_REDIRECTS = 3

HTTP_TIMEOUT_SECONDS = 15.0

# Neutral identifier — some Statuspage / news / stock APIs 403 the
# default httpx UA. Including a project URL would let upstream contact
# us about rate-limiting; intentionally omitted to avoid tenant leak.
USER_AGENT = "OpenExecutive-Monitor/1.0"


class FetchOverflowError(Exception):
    """Raised when a fetched body exceeded the configured byte ceiling."""


class BlockedTargetError(httpx.HTTPError):
    """Raised when a fetch hop (the first URL or a redirect) is refused.

    Subclasses :class:`httpx.HTTPError` so every adapter's existing
    ``except httpx.HTTPError`` logs it and skips the tick.
    """


def _resolve_public(url: str) -> tuple[list[str], str]:
    """Resolve ``url``'s host and return its addresses if all are public.

    Returns ``(ips, "")`` on success or ``([], reason)`` when the URL is
    refused: a scheme outside :data:`ALLOWED_SCHEMES`, no host, a DNS
    failure, or any address that is loopback / link-local / private /
    reserved / multicast / unspecified.
    """
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        return [], f"unparseable URL: {exc}"
    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        return [], f"scheme {scheme!r} not in {sorted(ALLOWED_SCHEMES)}"
    host = parsed.hostname or ""
    if not host:
        return [], "URL has no host"
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        return [], f"DNS resolution failed: {exc}"
    ips: list[str] = []
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (
            ip.is_loopback
            or ip.is_link_local
            or ip.is_private
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return [], f"host {host!r} resolves to non-public IP {ip}"
        if str(ip) not in ips:
            ips.append(str(ip))
    if not ips:
        return [], f"host {host!r} has no usable address"
    return ips, ""


def validate_target_url(url: str) -> tuple[bool, str]:
    """SSRF guard: only allow public http(s) URLs.

    Rejects non-http(s) schemes and any host that resolves to a
    loopback / link-local / private / reserved IP — the classic SSRF
    pivots into IMDS (169.254.169.254), local services (127.*), or
    private cloud meshes (RFC1918, IPv6 ULA). Returns
    ``(False, reason)`` to be logged at the call site.

    This is the check callers run before queueing a fetch.
    :func:`fetch_bounded` repeats it on every hop, redirects included,
    and connects to the address it vetted, so a redirect or a DNS answer
    that changes after this check can't reach a private address.
    """
    ips, reason = _resolve_public(url)
    return bool(ips), reason


def strip_url_query(url: str) -> str:
    """Drop the query string + fragment from a URL.

    Feeds occasionally embed analytics tokens (``?utm_source=…``) or
    session identifiers on links; a malicious feed could push arbitrary
    ``?token=<value>`` query strings into our audit log and DM bodies.
    Strip on the way in — the canonical path stays clickable.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    return parsed._replace(query="", fragment="").geturl()


def _transport() -> httpx.AsyncBaseTransport:
    return httpx.AsyncHTTPTransport(retries=0)


async def fetch_bounded(
    url: str, max_bytes: int, *, user_agent: str | None = None
) -> bytes:
    """GET ``url`` with a body-size ceiling and tight redirect cap.

    Streams the response and stops once the cumulative read exceeds
    ``max_bytes`` so a malicious feed cannot exhaust memory. Raises
    :class:`FetchOverflowError` rather than truncating-and-pretending-
    it-worked so the caller can log + skip the tick honestly.

    Redirects are followed by hand, at most :data:`MAX_REDIRECTS`. Every
    hop, the first included, is resolved and checked like
    :func:`validate_target_url`, and the connection goes to the address
    that passed the check (the hostname stays in the ``Host`` header and
    the TLS server name), so neither a redirect nor a second DNS answer
    can point the fetch at a private address. A refused hop raises
    :class:`BlockedTargetError`; too many hops raise
    :class:`httpx.TooManyRedirects`.

    ``user_agent`` overrides the default UA for hosts that require a
    specific identifier — e.g. SEC EDGAR rejects/ratelimits the generic
    UA and asks callers to identify with a contact. Falls back to the
    module ``USER_AGENT`` when None.
    """
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        ips, reason = await asyncio.to_thread(_resolve_public, current)
        if not ips:
            raise BlockedTargetError(f"refused {strip_url_query(current)}: {reason}")
        try:
            target = httpx.URL(current)
        except httpx.InvalidURL as exc:
            raise BlockedTargetError(f"refused {strip_url_query(current)}: {exc}") from exc
        pinned = target.copy_with(host=ips[0])
        extensions = {"sni_hostname": target.host} if target.scheme == "https" else {}
        # A fresh client per hop: a pooled TLS connection to the same IP
        # would otherwise carry the previous hop's server name.
        async with (
            httpx.AsyncClient(
                timeout=HTTP_TIMEOUT_SECONDS,
                follow_redirects=False,
                transport=_transport(),
                headers={"User-Agent": user_agent or USER_AGENT},
            ) as client,
            client.stream(
                "GET",
                pinned,
                headers={"Host": target.netloc.decode("ascii")},
                extensions=extensions,
            ) as resp,
        ):
            if resp.has_redirect_location:
                current = str(target.join(resp.headers["Location"]))
                continue
            resp.raise_for_status()
            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    raise FetchOverflowError()
            return bytes(buf)
    raise httpx.TooManyRedirects(f"more than {MAX_REDIRECTS} redirects from {strip_url_query(url)}")


__all__ = [
    "ALLOWED_SCHEMES",
    "BlockedTargetError",
    "FetchOverflowError",
    "HTTP_TIMEOUT_SECONDS",
    "MAX_REDIRECTS",
    "USER_AGENT",
    "fetch_bounded",
    "strip_url_query",
    "validate_target_url",
]
