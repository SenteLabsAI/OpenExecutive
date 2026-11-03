"""One way to write a time of day for the reader: "2:05 PM".

Briefing context hands times to the model, and the model copies them into
what the reader sees, so a 24-hour "14:05" in the context came out as
"14:05" on Home. Every time-of-day the briefs and Right now are given goes
through here.
"""
from __future__ import annotations

from datetime import datetime


def clock(dt: datetime) -> str:
    """"2:05 PM" for ``dt``'s own wall time (convert to the reader's zone first)."""
    hour = dt.hour % 12 or 12
    return f"{hour}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"
