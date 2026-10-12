"""GCP spend snapshot from the Cloud Billing BigQuery export (read-only).

Needs ``BILLING_EXPORT_TABLE`` (the standard-usage export table) and Application
Default Credentials whose identity can run query jobs and read that table
(``roles/bigquery.jobUser`` on the query project, ``roles/bigquery.dataViewer``
on the billing dataset). On the app VM that is the app service account.

Costs are net of credits. The table name is validated against a strict pattern
before being placed in SQL (identifiers cannot be bound as parameters).
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

from openexecutive.feeds.base import Feed

_TABLE_RE = re.compile(r"^[A-Za-z0-9_\-]+(\.[A-Za-z0-9_\-]+){1,2}$")
_SCOPE = "https://www.googleapis.com/auth/bigquery.readonly"
_NET = "cost + IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) AS c), 0)"

_BY_SERVICE_SQL = """
SELECT
  service.description AS service,
  MAX(currency) AS currency,
  SUM(IF(usage_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY), {net}, 0)) AS last_7d,
  SUM(IF(usage_start_time < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY), {net}, 0)) AS prior_7d
FROM `{table}`
WHERE usage_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 14 DAY)
GROUP BY service
ORDER BY last_7d DESC
LIMIT 8
"""

_MTD_SQL = """
SELECT MAX(currency) AS currency, SUM({net}) AS mtd
FROM `{table}`
WHERE usage_start_time >= TIMESTAMP_TRUNC(CURRENT_TIMESTAMP(), MONTH)
"""


def _adc_token() -> tuple[str, str | None]:
    """Blocking: return (access_token, default_project)."""
    import google.auth
    from google.auth.transport.requests import Request

    creds, project = google.auth.default(scopes=[_SCOPE])
    creds.refresh(Request())
    return str(creds.token), project


class BillingFeed(Feed):
    name = "GCP billing"
    gap_hint = "Do not estimate cloud spend or burn; flag the gap and ask for the number."

    def _table(self) -> str:
        from openexecutive.config import get_settings

        return (get_settings().billing_export_table or "").strip()

    def configured(self) -> bool:
        return bool(self._table())

    def required_config(self) -> str:
        return "BILLING_EXPORT_TABLE"

    async def _query(
        self, client: httpx.AsyncClient, token: str, project: str, sql: str
    ) -> list[dict[str, Any]]:
        resp = await client.post(
            f"https://bigquery.googleapis.com/bigquery/v2/projects/{project}/queries",
            json={"query": sql, "useLegacySql": False, "timeoutMs": 8000, "maxResults": 50},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        body = resp.json()
        if not body.get("jobComplete", False):
            raise RuntimeError("billing query did not complete in time")
        fields = [f["name"] for f in (body.get("schema") or {}).get("fields", [])]
        rows: list[dict[str, Any]] = []
        for row in body.get("rows", []) or []:
            values = [cell.get("v") for cell in row.get("f", [])]
            rows.append(dict(zip(fields, values, strict=False)))
        return rows

    async def _fetch(self, client: httpx.AsyncClient) -> str:
        from openexecutive.config import get_settings

        table = self._table()
        if not _TABLE_RE.match(table):
            raise ValueError("BILLING_EXPORT_TABLE is not a valid table reference")

        token, adc_project = await asyncio.to_thread(_adc_token)
        project = (get_settings().billing_query_project or adc_project or "").strip()
        if not project:
            raise ValueError("no query project (set BILLING_QUERY_PROJECT)")

        by_service = await self._query(
            client, token, project, _BY_SERVICE_SQL.format(table=table, net=_NET)
        )
        mtd = await self._query(client, token, project, _MTD_SQL.format(table=table, net=_NET))

        lines: list[str] = []
        if mtd and mtd[0].get("mtd") is not None:
            lines.append(f"Month-to-date net spend: {float(mtd[0]['mtd']):,.2f} {mtd[0].get('currency', '')}".rstrip())
        total_7 = sum(float(r.get("last_7d") or 0) for r in by_service)
        total_prior = sum(float(r.get("prior_7d") or 0) for r in by_service)
        if by_service:
            lines.append(
                f"Top services, last 7 days vs prior 7 days (total {total_7:,.2f} vs {total_prior:,.2f}):"
            )
            for r in by_service:
                cur = float(r.get("last_7d") or 0)
                prev = float(r.get("prior_7d") or 0)
                delta = f"{(cur - prev) / prev * 100:+.0f}%" if prev else "new"
                lines.append(f"- {r.get('service', '?')}: {cur:,.2f} ({delta})")
        else:
            lines.append("No billing rows in the last 14 days (export may be new or delayed).")
        return "\n".join(lines)
