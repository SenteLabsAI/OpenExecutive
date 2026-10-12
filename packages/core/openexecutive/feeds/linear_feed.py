"""Linear delivery-rhythm snapshot (read-only).

Needs a Linear personal API key (``LINEAR_API_KEY``). Reports, per team, the
active cycle's progress and issue mix, plus the in-progress issues that have
not been touched in a week (the practical definition of "stuck" when the team
does not use a dedicated Blocked state).
"""
from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from openexecutive.feeds.base import Feed

_API = "https://api.linear.app/graphql"
_STALE_AFTER = timedelta(days=7)

_CYCLE_QUERY = """
query ActiveCycles {
  teams(first: 25) {
    nodes {
      key
      name
      activeCycle {
        number
        startsAt
        endsAt
        issues(first: 100) {
          nodes { state { type } }
        }
      }
    }
  }
}
"""

_STALE_QUERY = """
query StaleInProgress($before: DateTimeOrDuration!) {
  issues(
    first: 25
    filter: { state: { type: { eq: "started" } }, updatedAt: { lt: $before } }
    orderBy: updatedAt
  ) {
    nodes {
      identifier
      title
      updatedAt
      assignee { name }
      team { key }
    }
  }
}
"""

_STATE_ORDER = ("completed", "started", "unstarted", "backlog", "triage", "canceled")


class LinearFeed(Feed):
    name = "Linear"
    gap_hint = "Do not estimate delivery status; flag the gap and ask for the cycle picture."

    def _key(self) -> str:
        from openexecutive.config import get_settings

        return (get_settings().linear_api_key or "").strip()

    def configured(self) -> bool:
        return bool(self._key())

    def required_config(self) -> str:
        return "LINEAR_API_KEY"

    async def _gql(
        self, client: httpx.AsyncClient, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        resp = await client.post(
            _API,
            json={"query": query, "variables": variables or {}},
            headers={"Authorization": self._key(), "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("errors"):
            # Message text only — never the request, which carries the key.
            first = body["errors"][0].get("message", "unknown error")
            raise RuntimeError(f"Linear GraphQL error: {str(first)[:120]}")
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    async def _fetch(self, client: httpx.AsyncClient) -> str:
        cycles = await self._gql(client, _CYCLE_QUERY)
        before = (datetime.now(UTC) - _STALE_AFTER).isoformat()
        stale = await self._gql(client, _STALE_QUERY, {"before": before})

        lines: list[str] = []
        for team in (cycles.get("teams") or {}).get("nodes", []):
            cycle = team.get("activeCycle")
            label = f"{team.get('name', team.get('key', '?'))}"
            if not cycle:
                lines.append(f"{label}: no active cycle.")
                continue
            counts = Counter(
                (n.get("state") or {}).get("type", "unknown")
                for n in (cycle.get("issues") or {}).get("nodes", [])
            )
            total = sum(counts.values())
            done = counts.get("completed", 0)
            mix = ", ".join(f"{counts[k]} {k}" for k in _STATE_ORDER if counts.get(k))
            ends = str(cycle.get("endsAt", ""))[:10]
            pct = f"{100 * done / total:.0f}%" if total else "n/a"
            lines.append(
                f"{label} — cycle {cycle.get('number', '?')} (ends {ends}): "
                f"{done}/{total} done ({pct}); {mix or 'no issues'}"
            )
        if not lines:
            lines.append("No teams returned.")

        stuck = (stale.get("issues") or {}).get("nodes", [])
        if stuck:
            lines.append(f"In progress with no update for 7+ days ({len(stuck)}):")
            for issue in stuck[:10]:
                who = (issue.get("assignee") or {}).get("name", "unassigned")
                lines.append(
                    f"- {issue.get('identifier', '?')} {str(issue.get('title', ''))[:80]} "
                    f"(last updated {str(issue.get('updatedAt', ''))[:10]}, {who})"
                )
        else:
            lines.append("No in-progress issues idle for 7+ days.")
        return "\n".join(lines)
