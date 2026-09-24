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
import re
from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel

from openexecutive.orchestrator.artifact_formats import (
    ARTIFACT_FORMATS,
    EXPORT_TARGETS,
    get_format,
)
from openexecutive.orchestrator.artifact_records import (
    ArtifactNotFound,
    ArtifactRecord,
    MalformedArtifactId,
    load_artifact,
    parse_artifact_id,
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

router = APIRouter()
logger = logging.getLogger(__name__)

# Max artifacts returned by the list endpoint (mirrors alerts.list_alerts' cap).
_DEFAULT_LIMIT = 200
# Chars of body shown as a card preview. Unrelated to _DEFAULT_LIMIT despite
# the shared value — keep them as separate named knobs.
_PREVIEW_CHARS = 200
_FILENAME_MAX = 60
_FILENAME_BAD = re.compile(r"[^a-z0-9]+")


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


class ArtifactDetail(ArtifactSummary):
    # What the page renders: raw (sanitized) HTML for 'html', Markdown for
    # every other format (a spreadsheet arrives as Markdown tables).
    body: str
    rationale: str | None = None


@router.get("/artifacts")
async def list_artifacts(
    limit: int = _DEFAULT_LIMIT, archived: bool = False
) -> dict[str, list[ArtifactSummary]]:
    """Unified, newest-first list of every artifact the Executive produced.

    Defaults to active artifacts; `?archived=true` returns only archived ones
    (the gallery's Active / Archived views are clean swaps, not supersets).
    """
    records = list_artifact_records(limit, archived=archived)
    return {"artifacts": [_summary(r) for r in records]}


@router.get("/artifacts/{composite_id}")
async def get_artifact(composite_id: str) -> ArtifactDetail:
    """One artifact with its displayable body, addressed by composite id."""
    rec = _load(composite_id)
    stored = rec.stored or ""
    body = stored if rec.format == "html" else get_format(rec.format).display(stored)
    return ArtifactDetail(**_summary(rec).model_dump(), body=body, rationale=rec.rationale)


@router.get("/artifacts/{composite_id}/download")
async def download_artifact(
    composite_id: str, as_: Annotated[str | None, Query(alias="as")] = None
) -> Response:
    """The artifact as a file. `?as=docx` exports a Markdown artifact to Word."""
    rec = _load(composite_id)
    targets = _downloads(rec)
    target = (as_ or (targets[0] if targets else "")).strip().lower()
    if not targets or target not in targets:
        raise HTTPException(
            status_code=404,
            detail=f"Artifact {composite_id!r} has no {target or 'file'} download",
        )
    fmt = ARTIFACT_FORMATS[target]
    assert fmt.render_file is not None and fmt.extension is not None
    try:
        content = fmt.render_file(rec.stored or "")
    except Exception as exc:
        logger.exception("artifact download render failed for %s as %s", composite_id, target)
        raise HTTPException(status_code=500, detail="Could not render the file") from exc
    filename = f"{_filename_stem(rec)}.{fmt.extension}"
    return Response(
        content=content,
        media_type=fmt.mime,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
            "Cache-Control": "no-store",
        },
    )


@router.post("/artifacts/{composite_id}/archive")
async def archive_artifact(composite_id: str) -> dict[str, str]:
    """Soft-hide an artifact (reversible). Drops it from the default list."""
    _mutate(lambda: set_artifact_archived(composite_id, archived=True))
    return {"status": "archived", "id": composite_id}


@router.post("/artifacts/{composite_id}/restore")
async def restore_artifact(composite_id: str) -> dict[str, str]:
    """Un-archive an artifact, returning it to the active list."""
    _mutate(lambda: set_artifact_archived(composite_id, archived=False))
    return {"status": "restored", "id": composite_id}


@router.delete("/artifacts/{composite_id}")
async def delete_artifact(composite_id: str) -> dict[str, str]:
    """Permanently delete the underlying alert / workflow-run row."""
    _mutate(lambda: delete_artifact_record(composite_id))
    from openexecutive.orchestrator.artifact_tools import unindex_artifact

    await unindex_artifact(composite_id)
    return {"status": "deleted", "id": composite_id}


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _load(composite_id: str) -> ArtifactRecord:
    try:
        return load_artifact(composite_id)
    except MalformedArtifactId as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ArtifactNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _mutate(action: Callable[[], object]) -> None:
    try:
        action()
    except MalformedArtifactId as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ArtifactNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _downloads(rec: ArtifactRecord) -> list[str]:
    own = get_format(rec.format)
    if own.render_file is None:
        return []
    return [own.name, *EXPORT_TARGETS.get(own.name, ())]


def _summary(rec: ArtifactRecord) -> ArtifactSummary:
    fmt = get_format(rec.format)
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
        downloads=_downloads(rec),
        external_url=rec.url,
        link_label=rec.link_label,
        supersedes_id=rec.supersedes_id,
    )


def _filename_stem(rec: ArtifactRecord) -> str:
    stem = _FILENAME_BAD.sub("-", rec.title.lower()).strip("-")[:_FILENAME_MAX].strip("-")
    if stem:
        return stem
    kind, native_id = parse_artifact_id(rec.id)
    return f"{kind}-{native_id[:12]}"
