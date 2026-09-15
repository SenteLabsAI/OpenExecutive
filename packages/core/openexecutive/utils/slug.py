"""One slug implementation, shared by everything that derives or matches slugs.

Extracted because there were three near-copies of the same regex —
``departments.store``, and the onboarding interview/commit modules that have to
predict what the store will assign. They were not quite identical: the store
falls back to a placeholder for a title that slugifies to nothing, the copies
returned "". A draft titled "###" therefore created a department under one slug
and was looked up under another on the next run. Callers that need to match the
store MUST pass the same ``fallback`` the store uses.
"""
from __future__ import annotations

import re

_NON_SLUG = re.compile(r"[^a-z0-9]+")

# What departments.store falls back to when a title slugifies to nothing.
DEPARTMENT_SLUG_FALLBACK = "department"


def slugify(title: str, *, fallback: str = "") -> str:
    """Lowercase, collapse non-alphanumerics to hyphens, trim hyphens."""
    slug = _NON_SLUG.sub("-", title.strip().lower()).strip("-")
    return slug or fallback
