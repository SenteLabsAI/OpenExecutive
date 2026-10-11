import asyncio
import time
from typing import Any

from fastapi import APIRouter, Request

from openexecutive.api.models import HealthResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health_check(request: Request) -> HealthResponse:
    from openexecutive.config import get_settings
    from openexecutive.knowledge.skills_index import count_skills
    from openexecutive.knowledge.store import ChromaDBStore
    from openexecutive.onboarding.profile_builder import load_or_create_profile

    settings = get_settings()

    builtin_skills_count = 0
    company_skills_count = 0
    try:
        store = ChromaDBStore(persist_directory=settings.vector_store_path)
        chunk_count = store.get_collection_count(ChromaDBStore.BUILTIN_COLLECTION)
        builtin_skills_count = count_skills(store, source="builtin")
        company_skills_count = count_skills(store, source="company")
    except Exception:
        chunk_count = 0

    profile = load_or_create_profile()

    # This app's gateway, not the module-level active one: shutdown clears the
    # singleton while leaving app.state populated, and the MCP-disabled path
    # nulls app.state without clearing the singleton, so the two diverge.
    # getattr both ways: an app assembled without the MCP lifespan has no
    # attribute, and a gateway built past __init__ (tests) has no probe.
    gateway = getattr(request.app.state, "mcp_gateway", None)
    probe = getattr(gateway, "pinned_tools", None)
    missing = probe.missing if probe is not None else None

    return HealthResponse(
        status="ok",
        builtin_knowledge_chunks=chunk_count,
        company_profile_loaded=not profile.is_empty(),
        company_name=profile.name if not profile.is_empty() else None,
        builtin_skills=builtin_skills_count,
        company_skills=company_skills_count,
        google_tools_missing=list(missing) if missing is not None else None,
    )


_HONCHO_PROBE_TIMEOUT_S = 3.0


@router.get("/health/honcho")
async def honcho_health() -> dict[str, Any]:
    """Probe the configured Honcho workspace with an authed SDK call.

    Returns one of three shapes:
    - ``{"status": "disabled", ...}`` — HONCHO_ENABLED is false; nothing to probe.
    - ``{"status": "ok", "latency_ms": N, ...}`` — workspace reachable + auth valid.
    - ``{"status": "error", "error_type": "...", "error_msg": "...", ...}`` — failure.

    Uses ``client.aio.get_metadata()`` — a read-only call against the
    active workspace that proves connectivity, auth, AND that the workspace
    exists. Intentionally avoids dialectic endpoints (an LLM call per probe).
    """
    from openexecutive.config import get_settings
    from openexecutive.memory.honcho_client import (
        _get_client,
        get_active_workspace_id,
    )

    settings = get_settings()
    workspace_id = get_active_workspace_id() if settings.honcho_enabled else settings.honcho_workspace_id
    base = {
        "base_url": settings.honcho_base_url,
        "workspace_id": workspace_id,
    }
    if not settings.honcho_enabled:
        return {"status": "disabled", **base}

    t0 = time.monotonic()
    try:
        client = await _get_client()
        if client is None:
            return {
                "status": "error",
                "error_type": "ClientConstructionFailed",
                "error_msg": "honcho client could not be constructed (missing key, import failure, or no event loop)",
                "latency_ms": int((time.monotonic() - t0) * 1000),
                **base,
            }
        await asyncio.wait_for(client.aio.get_metadata(), timeout=_HONCHO_PROBE_TIMEOUT_S)
        return {
            "status": "ok",
            "latency_ms": int((time.monotonic() - t0) * 1000),
            **base,
        }
    except TimeoutError:
        return {
            "status": "error",
            "error_type": "TimeoutError",
            "error_msg": f"get_metadata exceeded {_HONCHO_PROBE_TIMEOUT_S}s",
            "latency_ms": int((time.monotonic() - t0) * 1000),
            **base,
        }
    except Exception as exc:  # pragma: no cover - any honcho/network failure
        return {
            "status": "error",
            "error_type": type(exc).__name__,
            "error_msg": str(exc)[:200],
            "latency_ms": int((time.monotonic() - t0) * 1000),
            **base,
        }
