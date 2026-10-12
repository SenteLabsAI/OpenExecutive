"""Stripe revenue snapshot (read-only).

Needs a Stripe *restricted* key with read access to Balance, Subscriptions and
Invoices only (``STRIPE_READONLY_KEY``). The key is sent only as a bearer
token to ``api.stripe.com`` and is never logged or rendered.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

import httpx

from openexecutive.feeds.base import Feed

_API = "https://api.stripe.com"
_PAGE = 100

# Days per interval unit, used to normalise recurring prices to a month.
_MONTHS_PER_INTERVAL = {"day": 1 / 30, "week": 12 / 52, "month": 1.0, "year": 12.0}


def _money(minor_units: int | float, currency: str) -> str:
    # Zero-decimal currencies are rare for this company; treat all as /100 and
    # label the currency so a misread is visible rather than silent.
    return f"{minor_units / 100:,.2f} {currency.upper()}"


class StripeFeed(Feed):
    name = "Stripe"
    gap_hint = "Do not estimate revenue, MRR or balances; flag the gap and ask for the number."

    def _key(self) -> str:
        from openexecutive.config import get_settings

        return (get_settings().stripe_readonly_key or "").strip()

    def configured(self) -> bool:
        return bool(self._key())

    def required_config(self) -> str:
        return "STRIPE_READONLY_KEY"

    async def _get(self, client: httpx.AsyncClient, path: str, params: dict[str, Any]) -> dict[str, Any]:
        resp = await client.get(
            f"{_API}{path}",
            params=params,
            headers={"Authorization": f"Bearer {self._key()}"},
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}

    async def _fetch(self, client: httpx.AsyncClient) -> str:
        balance = await self._get(client, "/v1/balance", {})
        subs = await self._get(client, "/v1/subscriptions", {"status": "active", "limit": _PAGE})
        open_invoices = await self._get(client, "/v1/invoices", {"status": "open", "limit": _PAGE})

        lines: list[str] = []

        available = {b.get("currency", "?"): b.get("amount", 0) for b in balance.get("available", [])}
        pending = {b.get("currency", "?"): b.get("amount", 0) for b in balance.get("pending", [])}
        for cur in sorted(set(available) | set(pending)):
            lines.append(
                f"Balance {cur.upper()}: available {_money(available.get(cur, 0), cur)}, "
                f"pending {_money(pending.get(cur, 0), cur)}"
            )

        sub_rows = subs.get("data", [])
        mrr: dict[str, float] = defaultdict(float)
        for sub in sub_rows:
            for item in (sub.get("items") or {}).get("data", []):
                price = item.get("price") or {}
                recurring = price.get("recurring") or {}
                unit = price.get("unit_amount")
                if unit is None:
                    continue  # tiered/metered prices: not computable from this view
                months = _MONTHS_PER_INTERVAL.get(recurring.get("interval", "month"), 1.0)
                months *= recurring.get("interval_count", 1) or 1
                mrr[price.get("currency", "?")] += unit * (item.get("quantity") or 1) / months
        more = "+" if subs.get("has_more") else ""
        lines.append(f"Active subscriptions: {len(sub_rows)}{more}")
        for cur in sorted(mrr):
            lines.append(f"Estimated MRR {cur.upper()}: {_money(mrr[cur], cur)} (flat-priced items only)")
        if not sub_rows:
            lines.append("No active subscriptions.")

        inv_rows = open_invoices.get("data", [])
        due: dict[str, int] = defaultdict(int)
        for inv in inv_rows:
            due[inv.get("currency", "?")] += int(inv.get("amount_remaining", 0) or 0)
        more_inv = "+" if open_invoices.get("has_more") else ""
        lines.append(f"Open invoices: {len(inv_rows)}{more_inv}")
        for cur in sorted(due):
            lines.append(f"Open invoice amount {cur.upper()}: {_money(due[cur], cur)}")
        return "\n".join(lines)
