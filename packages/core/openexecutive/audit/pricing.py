"""List prices, for estimating what the Executive's model calls cost.

The usage log (``audit.usage``) records every call's tokens, but only
OpenRouter reports what a call was charged. On the standard setup — the
Anthropic API directly — nothing does, so spend is estimated here from the
tokens at Anthropic's published list prices
(https://platform.claude.com/docs/en/about-claude/pricing, September 2026):

- input and output per million tokens, per model;
- cache writes by how long they are kept: 1.25x input for 5 minutes, 2x for
  1 hour; cache reads 0.1x input (0.025x on Fable 5.1 / Mythos 5.1, 0.05x on
  Opus 5.5 — so reads are listed per model);
- web search at $10 per 1,000 searches (web fetch is free);
- no long-context surcharge on the 4.6 and later models. Sonnet 4 and 4.5
  bill a prompt over 200K tokens (the 1M-context beta) at a higher rate;
  this table prices it at the base rate, so it falls short there.

It is a list-price estimate: it knows nothing of a negotiated discount,
Bedrock or Vertex pricing, fast mode or US-only inference (1.1x). A model
this table does not know (a local model, another vendor through OpenRouter
that reported no charge, a Claude model newer than the table) has no price,
and its calls are counted as unpriced, never as free.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 5-minute and 1-hour cache writes, as multiples of the input price.
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0
WEB_SEARCH_USD = 10.0 / 1000


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens."""

    input: float
    output: float
    cache_read: float

    @property
    def cache_write_5m(self) -> float:
        return self.input * CACHE_WRITE_5M_MULTIPLIER

    @property
    def cache_write_1h(self) -> float:
        return self.input * CACHE_WRITE_1H_MULTIPLIER


# Keyed by the Claude API model id without a date. Matched exactly after
# normalize_model(), so a model this table does not list is unpriced rather
# than priced as a neighbour ("claude-opus-5-5" is not "claude-opus-5").
PRICES: dict[str, ModelPrice] = {
    "claude-fable-5-1": ModelPrice(10.0, 50.0, 0.25),
    "claude-mythos-5-1": ModelPrice(10.0, 50.0, 0.25),
    "claude-fable-5": ModelPrice(10.0, 50.0, 1.0),
    "claude-mythos-5": ModelPrice(10.0, 50.0, 1.0),
    "claude-opus-5-5": ModelPrice(4.0, 20.0, 0.20),
    "claude-opus-5": ModelPrice(5.0, 25.0, 0.50),
    "claude-opus-4-8": ModelPrice(5.0, 25.0, 0.50),
    "claude-opus-4-7": ModelPrice(5.0, 25.0, 0.50),
    "claude-opus-4-6": ModelPrice(5.0, 25.0, 0.50),
    "claude-opus-4-5": ModelPrice(5.0, 25.0, 0.50),
    "claude-opus-4-1": ModelPrice(15.0, 75.0, 1.50),
    "claude-opus-4": ModelPrice(15.0, 75.0, 1.50),
    "claude-sonnet-5": ModelPrice(2.0, 10.0, 0.20),
    "claude-sonnet-4-6": ModelPrice(3.0, 15.0, 0.30),
    "claude-sonnet-4-5": ModelPrice(3.0, 15.0, 0.30),
    "claude-sonnet-4": ModelPrice(3.0, 15.0, 0.30),
    "claude-haiku-4-5": ModelPrice(1.0, 5.0, 0.10),
    "claude-3-5-haiku": ModelPrice(0.80, 4.0, 0.08),
}

# Bedrock ids: an optional cross-region profile, then "anthropic.".
_BEDROCK_PREFIX = re.compile(r"^(?:[a-z]{2,6}\.)?anthropic\.")
# Bedrock's "-v1:0" version, then an 8-digit release date.
_BEDROCK_VERSION = re.compile(r"-v\d+(?::\d+)?$")
_DATE_SUFFIX = re.compile(r"-\d{8}$")


def normalize_model(model: str) -> str:
    """The Claude API id a recorded model name refers to, as a PRICES key:
    lowercased, without a context-window tag ("[1m]"), a Vertex version
    ("@20251101"), an OpenRouter ("anthropic/", dotted versions) or Bedrock
    ("us.anthropic.", "-v1:0") wrapping, or a release date."""
    name = (model or "").strip().lower()
    name = name.split("[", 1)[0].split("@", 1)[0]
    if name.startswith("anthropic/"):
        name = name[len("anthropic/"):].replace(".", "-")
    name = _BEDROCK_PREFIX.sub("", name)
    name = _BEDROCK_VERSION.sub("", name)
    return _DATE_SUFFIX.sub("", name)


def price_for(model: str) -> ModelPrice | None:
    """The list price of ``model``, or None when this table does not know it."""
    return PRICES.get(normalize_model(model))


def estimate_cost(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_5m_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
    web_searches: int = 0,
) -> float | None:
    """What these counts cost at list price, in USD, or None for an unknown
    model. Callers price a cache write whose lifetime was not recorded as a
    1-hour write, the dearer one, so an estimate never falls short on it."""
    price = price_for(model)
    if price is None:
        return None
    per_million = (
        input_tokens * price.input
        + output_tokens * price.output
        + cache_read_tokens * price.cache_read
        + cache_write_5m_tokens * price.cache_write_5m
        + cache_write_1h_tokens * price.cache_write_1h
    )
    return per_million / 1_000_000 + web_searches * WEB_SEARCH_USD


def format_usd(amount: float) -> str:
    """"$1,234.56" — how every backend message shows an amount (the UI's
    ``lib/spending.ts`` ``formatUsd`` says it the same way)."""
    return f"${amount:,.2f}"


__all__ = [
    "PRICES",
    "WEB_SEARCH_USD",
    "ModelPrice",
    "estimate_cost",
    "format_usd",
    "normalize_model",
    "price_for",
]
