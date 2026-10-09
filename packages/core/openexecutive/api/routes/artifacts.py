"""HTTP surface for the Executive Artifacts section.

A unified view over the two places the Executive stores deliverables — no
new table, the underlying rows stay canonical:

1. `draft_artifact` output, written to the alerts table
   (`source='artifact'`) in any registered format (Markdown, HTML, Word,
   Excel, or a link into a connected app — `orchestrator/artifact_formats.py`),
2. completed workflow runs, whose Markdown lives in
   `workflow_runs.artifact`.

Both sources are merged, sorted newest-first, and addressed by a composite
id `"{kind}:{native_id}"` (`alert:<int>` / `run:<hex>`). Lookup, archive and
delete go through `orchestrator/artifact_records.py`, which the Executive's
`list_artifacts` / `get_artifact` tools share.

Archive is a reversible soft-hide (a nullable `archived_at` column on each
backing table); delete is a permanent hard-delete of the underlying row.
The default list shows only active artifacts; `?archived=true` shows only
archived ones. Every route refuses non-artifact alert ids — these routes
must not become general alert/run readers or mutators.

Every route answers for the signed-in caller (`chat._resolve_caller_person_id`)
and sees only what they may (`artifact_records` module docstring): their own
drafts and runs, and the team's scheduled ones. Anyone else's answers 404, as
a missing one does.

Downloads are always served as attachments with `nosniff` and a sandbox CSP,
so an HTML artifact never renders on the app's origin (the UI shows it in a
sandboxed iframe instead).

Endpoints:
- GET    /artifacts                          Unified list (summaries, no bodies)
- GET    /artifacts/{composite_id}           One artifact with its displayable body
- GET    /artifacts/{composite_id}/download  The file (optionally ?as=<format>)
- POST   /artifacts/{composite_id}/archive   Soft-hide (reversible)
- POST   /artifacts/{composite_id}/restore   Un-archive
- DELETE /artifacts/{composite_id}           Permanently delete the underlying row
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel

from openexecutive.orchestrator.artifact_formats import get_format
from openexecutive.orchestrator.artifact_records import (
    ArtifactNotFound,
    ArtifactRecord,
    MalformedArtifactId,
    Viewer,
    artifact_downloads,
    load_artifact,
    render_artifact_file,
    viewer_for_person,
)
from openexecutive.orchestrator.artifact_records import (
    delete_artifact as delete_artifact_record,
)
from openexecutive.orchestrator.artifact_records import (
    list_artifacts as list_artifact_records,
)
from openexecutive.orchestrator.artifact_records import (
    set_archived as set_artifact_archived,
)
from openexecutive.orchestrator.extensions import Change, get_collection
from openexecutive.orchestrator.extensions import notify as notify_collection

router = APIRouter()
logger = logging.getLogger(__name__)

# Max artifacts returned by the list endpoint (mirrors alerts.list_alerts' cap).
_DEFAULT_LIMIT = 200
# Chars of body shown as a card preview. Unrelated to _DEFAULT_LIMIT despite
# the shared value — keep them as separate named knobs.
_PREVIEW_CHARS = 200


class ArtifactSummary(BaseModel):
    id: str
    kind: Literal["draft", "workflow"]
    title: str
    source_label: str
    created_at: str
    preview: str | None = None
    status: str
    severity: str | None = None
    # ISO timestamp when archived; None = active. Lets the detail page show
    # Archive vs Restore correctly and keeps the TS interface honest.
    archived_at: str | None = None
    # Registered format name (artifact_formats.ARTIFACT_FORMAT_NAMES).
    format: str = "markdown"
    format_label: str = "Document"
    # Download targets, first = the artifact's own file. Empty for links.
    downloads: list[str] = []
    external_url: str | None = None
    link_label: str | None = None
    supersedes_id: str | None = None
    # An installed extension's kind of document (orchestrator/extensions.py):
    # the Documents page gives each its own filter.
    collection: str | None = None
    collection_label: str | None = None


class ArtifactDetail(ArtifactSummary):
    # What the page renders: raw (sanitized) HTML for 'html', Markdown for
    # every other format (a spreadsheet arrives as Markdown tables).
    body: str
    rationale: str | None = None


@router.get("/artifacts")
async def list_artifacts(
    request: Request, limit: int = _DEFAULT_LIMIT, archived: bool = False
) -> dict[str, list[ArtifactSummary]]:
    """Unified, newest-first list of the artifacts the caller may see.

    Defaults to active artifacts; `?archived=true` returns only archived ones
    (the gallery's Active / Archived views are clean swaps, not supersets).
    """
    records = list_artifact_records(limit, viewer=_viewer(request), archived=archived)
    return {"artifacts": [_summary(r) for r in records]}


@router.get("/artifacts/{composite_id}")
async def get_artifact(composite_id: str, request: Request) -> ArtifactDetail:
    """One artifact with its displayable body, addressed by composite id."""
    rec = _load(composite_id, request)
    stored = rec.stored or ""
    body = stored if rec.format == "html" else get_format(rec.format).display(stored)
    return ArtifactDetail(**_summary(rec).model_dump(), body=body, rationale=rec.rationale)


@router.get("/artifacts/{composite_id}/download")
async def download_artifact(
    composite_id: str,
    request: Request,
    as_: Annotated[str | None, Query(alias="as")] = None,
) -> Response:
    """The artifact as a file. `?as=docx` exports a Markdown artifact to Word."""
    rec = _load(composite_id, request)
    try:
        file = render_artifact_file(rec, as_)
    except ArtifactNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(
            "artifact download render failed for %s as %s", composite_id, as_ or rec.format
        )
        raise HTTPException(status_code=500, detail="Could not render the file") from exc
    return Response(
        content=file.content,
        media_type=file.mime,
        headers={
            "Content-Disposition": f'attachment; filename="{file.filename}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
            "Cache-Control": "no-store",
        },
    )


@router.post("/artifacts/{composite_id}/archive")
async def archive_artifact(composite_id: str, request: Request) -> dict[str, str]:
    """Soft-hide an artifact (reversible). Drops it from the default list and
    from the Executive's recall (knowledge index)."""
    viewer = _viewer(request)
    await _notify_collection(composite_id, request, "archived")
    rec = _mutate(lambda: set_artifact_archived(composite_id, archived=True, viewer=viewer))
    from openexecutive.orchestrator.artifact_tools import unindex_artifact

    await unindex_artifact(rec.id)
    return {"status": "archived", "id": composite_id}


@router.post("/artifacts/{composite_id}/restore")
async def restore_artifact(composite_id: str, request: Request) -> dict[str, str]:
    """Un-archive an artifact, returning it to the active list and, for a
    drafted artifact, to the knowledge index."""
    viewer = _viewer(request)
    await _notify_collection(composite_id, request, "restored")
    rec = _mutate(lambda: set_artifact_archived(composite_id, archived=False, viewer=viewer))
    if rec.kind == "draft" and rec.stored:
        from openexecutive.orchestrator.artifact_tools import index_artifact

        await index_artifact(rec.id, rec.title, rec.format, rec.stored, rec.owner_person_id)
    return {"status": "restored", "id": composite_id}


@router.delete("/artifacts/{composite_id}")
async def delete_artifact(composite_id: str, request: Request) -> dict[str, str]:
    """Permanently delete the underlying alert / workflow-run row."""
    viewer = _viewer(request)
    await _notify_collection(composite_id, request, "deleted")
    rec = _mutate(lambda: delete_artifact_record(composite_id, viewer=viewer))
    from openexecutive.orchestrator.artifact_tools import unindex_artifact

    # The canonical id (`alert:5`, not whatever spelling the path used) is
    # what indexing keyed the chunks on.
    await unindex_artifact(rec.id)
    return {"status": "deleted", "id": composite_id}


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _viewer(request: Request) -> Viewer:
    """The caller, as the artifact read model sees them."""
    from openexecutive.api.routes.chat import _resolve_caller_person_id

    return viewer_for_person(_resolve_caller_person_id(request))


def _load(composite_id: str, request: Request) -> ArtifactRecord:
    try:
        return load_artifact(composite_id, viewer=_viewer(request))
    except MalformedArtifactId as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ArtifactNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


async def _notify_collection(composite_id: str, request: Request, change: Change) -> None:
    """Tell an extension collection one of its documents is about to change
    (orchestrator/extensions.py). If it can't take the change, nothing
    changes: a 502 the page shows as a failed action."""
    rec = _load(composite_id, request)
    if rec.collection is None:
        return
    try:
        await notify_collection(rec.collection, rec.id, change)
    except Exception as exc:
        logger.exception("artifact %s: collection %s refused %s", rec.id, rec.collection, change)
        raise HTTPException(
            status_code=502, detail="That couldn't be done right now. Try again in a moment."
        ) from exc


def _mutate(action: Callable[[], ArtifactRecord]) -> ArtifactRecord:
    try:
        return action()
    except MalformedArtifactId as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ArtifactNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc



def _summary(rec: ArtifactRecord) -> ArtifactSummary:
    fmt = get_format(rec.format)
    collection = get_collection(rec.collection)
    preview = fmt.text(rec.stored)[:_PREVIEW_CHARS] if rec.stored else ""
    return ArtifactSummary(
        id=rec.id,
        kind=rec.kind,
        title=rec.title,
        source_label=rec.source_label,
        created_at=rec.created_at,
        preview=preview or None,
        status=rec.status,
        severity=rec.severity,
        archived_at=rec.archived_at,
        format=fmt.name,
        format_label=rec.link_label if fmt.name == "link" and rec.link_label else fmt.label,
        downloads=artifact_downloads(rec),
        external_url=rec.url,
        link_label=rec.link_label,
        supersedes_id=rec.supersedes_id,
        collection=rec.collection,
        collection_label=collection.label if collection is not None else None,
    )
