"""The `<channel>` block must not claim a capability the channel lacks.

Issue #136 was the assistant asking for an approval it could not process.
A block that tells the model "a sign-off can be answered here" on a channel
whose adapter never calls the inbound resolver is the same overclaim, written
into the prompt on purpose.
"""
from __future__ import annotations

import pytest

from openexecutive.integrations.channel_context import (
    _RESOLVER_CHANNELS,
    build_channel_context_block,
)

_SIGNOFF_CLAIM = "CAN be answered here"


@pytest.mark.parametrize("channel", sorted(_RESOLVER_CHANNELS))
def test_resolver_channels_may_claim_sign_offs_are_answerable(channel: str) -> None:
    block = build_channel_context_block(channel, ui_base_url="https://oe.test")
    assert _SIGNOFF_CLAIM in block


@pytest.mark.parametrize("channel", ["google_chat", "email"])
def test_non_resolver_channels_never_claim_it(channel: str) -> None:
    block = build_channel_context_block(channel, ui_base_url="https://oe.test")
    assert _SIGNOFF_CLAIM not in block
    assert "cannot record a workflow sign-off" in block


def test_every_resolver_channel_has_a_label() -> None:
    """A channel in the allowlist with no label renders an empty block, which
    would silently drop the whole set of approval rules."""
    for channel in _RESOLVER_CHANNELS:
        assert build_channel_context_block(channel)


def test_unknown_channel_asserts_nothing() -> None:
    assert build_channel_context_block("carrier_pigeon") == ""


def test_block_degrades_without_a_base_url() -> None:
    """A settings problem must not raise on the reply path."""
    block = build_channel_context_block("slack", ui_base_url="")
    assert "briefing page" in block
    assert "()" not in block
