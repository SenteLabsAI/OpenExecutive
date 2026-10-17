"""Chat ids that hold a slash, in a URL path.

A Google Chat chat's id carries the space's name (``google_chat:spaces/AAQA…``,
``…:spaces/AAQA…/threads/…``). Percent-encoded, that slash is decoded before
routing, so ``/sessions/{session_id}`` never matches and the chat can't be
opened, renamed or deleted. Such an id travels as one segment instead: ``~``
and its base64url (the web proxy writes it, ``lib/proxySegments.ts``). A
route reads it back with ``{session_id:session}``. Every other id passes
through unchanged; none starts with ``~``.
"""

from __future__ import annotations

import base64
import binascii
import re

from starlette.convertors import Convertor, register_url_convertor

ENCODED_PREFIX = "~"
_BASE64URL = re.compile(r"[A-Za-z0-9_-]+")


def encode_path_id(value: str) -> str:
    """The segment for ``value``: itself, or ``~`` + base64url when it holds a slash."""
    if "/" not in value:
        return value
    raw = base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")
    return ENCODED_PREFIX + raw


def decode_path_id(segment: str) -> str:
    """The id a path segment names. A ``~`` segment that isn't valid base64url
    stays as it is, so it simply matches no chat."""
    if not segment.startswith(ENCODED_PREFIX):
        return segment
    raw = segment[len(ENCODED_PREFIX):]
    if not _BASE64URL.fullmatch(raw):
        return segment
    try:
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return segment


class SessionIdConvertor(Convertor[str]):
    regex = "[^/]+"

    def convert(self, value: str) -> str:
        return decode_path_id(value)

    def to_string(self, value: str) -> str:
        return encode_path_id(value)


register_url_convertor("session", SessionIdConvertor())
