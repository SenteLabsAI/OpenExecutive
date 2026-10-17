"""Chat ids that hold a slash travel as one path segment (api/path_ids.py)."""
from __future__ import annotations

import pytest

from openexecutive.api.path_ids import decode_path_id, encode_path_id


@pytest.mark.parametrize(
    "sid",
    [
        "google_chat:spaces/6U3LSqAAAAE~6",
        "google_chat:spaces/AAQAU-9BHIY:spaces/AAQAU-9BHIY/threads/-C-x6xYbH6U",
        "a/é/b",
    ],
)
def test_slashed_ids_round_trip(sid: str) -> None:
    segment = encode_path_id(sid)
    assert segment.startswith("~")
    assert "/" not in segment and "=" not in segment
    assert decode_path_id(segment) == sid


@pytest.mark.parametrize("sid", ["4b43cebb-8031-4187-a0d1-43801fb9b22b", "telegram:42", "slack:dm:U1"])
def test_other_ids_pass_through(sid: str) -> None:
    assert encode_path_id(sid) == sid
    assert decode_path_id(sid) == sid


def test_matches_the_web_proxy() -> None:
    # packages/ui/src/lib/proxySegments.ts: "~" + Buffer.toString("base64url").
    assert encode_path_id("google_chat:spaces/AAQA") == "~Z29vZ2xlX2NoYXQ6c3BhY2VzL0FBUUE"


def test_a_bad_encoded_segment_stays_as_is() -> None:
    assert decode_path_id("~not base64!") == "~not base64!"
    assert decode_path_id("~/w") == "~/w"
    assert decode_path_id("~a!b") == "~a!b"
