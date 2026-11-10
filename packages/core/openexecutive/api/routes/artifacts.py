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
- POST   /artifacts/{composite_id}/share     Share by link (answers the link's path)
- DELETE /artifacts/{composite_id}/share     Stop sharing: the link stops at once
- GET    /shared-documents/{key}             What a share link opens (public page)

`GET /artifacts?q=` searches titles, sources and the documents' text within
what the caller may see, in the view asked for (`utils.text_search`).

Share by link (`orchestrator/artifact_shares.py`): the owner of a document,
or the principal for the team's, gets an unguessable link to a read-only
page of that one version. `GET /shared-documents/{key}` is what the UI's
public `/shared/<key>` page reads, server to server: the title, the kind
and the text, nothing else, and not found for anything but a live link.
"""
from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel

from openexecutive.orchestrator import artifact_shares
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
from openexecutive.utils.text_search import clean_search

router = APIRouter()
logger = logging.getLogger(__name__)

# Max artifacts returned by the list endpoint (mirrors alerts.list_alerts' cap).
_DEFAULT_LIMIT = 200
# The Executive's own working notes: kept in the run log and recall, left
# off the Documents page (its owner never asked for them).
_OFF_THE_PAGE: frozenset[str] = frozenset({"executive_reflection", "executive_research"})
# Daily write-ups that pile up: one closed group at the bottom of the page.
_BRIEFS: frozenset[str] = frozenset({"morning_brief", "end_of_day_digest"})
_BRIEFS_LABEL = "Briefs and wrap-ups"
# What a run's title says about how it started (workflows/, research_tools,
# workflow_run_tools, onboarding).
_HOW_IT_STARTED: tuple[str, ...] = (
    "(periodic)", "(post-onboarding auto-fire)", "(chat-tool fire)",
)
_CHATS_LABEL = "From chats"

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
    # Where the Documents page files it: "chats" (drafted in a chat),
    # "briefs" (morning briefs and wrap-ups, one closed group), "c:<name>"
    # (an extension's collection) or "wf:<workflow name>"; and that group's
    # plain name.
    group: str = "chats"
    group_label: str = "From chats"
    # Share by link: whether the caller may share it, and the link's path
    # (on the app's own address) while it is shared.
    can_share: bool = False
    share_path: str | None = None


class ArtifactDetail(ArtifactSummary):
    # What the page renders: raw (sanitized) HTML for 'html', Markdown for
    # every other format (a spreadsheet arrives as Markdown tables).
    body: str
    rationale: str | None = None


@router.get("/artifacts")
async def list_artifacts(
    request: Request,
    limit: int = _DEFAULT_LIMIT,
    archived: bool = False,
    q: str | None = None,
) -> dict[str, list[ArtifactSummary]]:
    """Unified, newest-first list of the artifacts the caller may see.

    Defaults to active artifacts; `?archived=true` returns only archived ones
    (the gallery's Active / Archived views are clean swaps, not supersets).
    The Executive's own working notes (`_OFF_THE_PAGE`) are left out of the
    active list; they still open by id, and one archived earlier still shows
    under Archived so it can be restored or deleted.

    `?q=` keeps only those whose title, source or text holds every word of
    it, in the same view.
    """
    viewer = _viewer(request)
    records = list_artifact_records(
        limit,
        viewer=viewer,
        archived=archived,
        exclude_workflows=() if archived else _OFF_THE_PAGE,
        search=clean_search(q),
    )
    labels: dict[str, str] = {}
    shared = artifact_shares.share_keys(r.id for r in records)
    return {"artifacts": [_summary(r, labels, viewer, shared) for r in records]}


@router.get("/artifacts/{composite_id}")
async def get_artifact(composite_id: str, request: Request) -> ArtifactDetail:
    """One artifact with its displayable body, addressed by composite id."""
    viewer = _viewer(request)
    rec = _load(composite_id, request)
    summary = _summary(rec, {}, viewer, artifact_shares.share_keys([rec.id]))
    return ArtifactDetail(**summary.model_dump(), body=_body(rec), rationale=rec.rationale)


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
    artifact_shares.forget(rec.id)
    return {"status": "deleted", "id": composite_id}


@router.post("/artifacts/{composite_id}/share")
async def share_artifact(composite_id: str, request: Request) -> dict[str, str]:
    """Share a document by link. Answers the link's path on the app's own
    address; asking again answers the same link."""
    viewer = _viewer(request)
    try:
        key = artifact_shares.share(composite_id, viewer=viewer)
    except artifact_shares.ShareRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MalformedArtifactId as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ArtifactNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    logger.info("artifact %s shared by link by person %s", composite_id, viewer.person_id)
    return {"status": "shared", "id": composite_id, "share_path": artifact_shares.public_path(key)}


@router.delete("/artifacts/{composite_id}/share")
async def stop_sharing_artifact(composite_id: str, request: Request) -> dict[str, str]:
    """Stop sharing a document by link. The link stops working at once."""
    viewer = _viewer(request)
    _mutate(lambda: _stop_share(composite_id, viewer))
    logger.info("artifact %s link stopped by person %s", composite_id, viewer.person_id)
    return {"status": "stopped", "id": composite_id}


class SharedDocument(BaseModel):
    """All a share link shows: no ids, no names, no source."""

    title: str
    format: str
    format_label: str
    created_at: str
    body: str


@router.get("/shared-documents/{key}")
async def shared_document(key: str, response: Response) -> SharedDocument:
    """The document a share link opens, for the UI's public page. Not found
    for anything but a live link (`artifact_shares.load_shared`)."""
    rec = artifact_shares.load_shared(key)
    if rec is None:
        raise HTTPException(status_code=404, detail="Not found")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    fmt = get_format(rec.format)
    return SharedDocument(
        title=_title(rec),
        format=fmt.name,
        format_label=fmt.label,
        created_at=rec.created_at,
        body=_body(rec),
    )


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
    # Archiving what is archived, or restoring what is active, changes
    # nothing, so the collection isn't told.
    if (change == "archived") == (rec.archived_at is not None) and change != "deleted":
        return
    try:
        await notify_collection(rec.collection, rec.id, change)
    except Exception as exc:
        logger.exception("artifact %s: collection %s refused %s", rec.id, rec.collection, change)
        raise HTTPException(
            status_code=502, detail="That couldn't be done right now. Try again in a moment."
        ) from exc


def _stop_share(composite_id: str, viewer: Viewer) -> ArtifactRecord:
    artifact_shares.stop_share(composite_id, viewer=viewer)
    return load_artifact(composite_id, viewer=viewer)


def _body(rec: ArtifactRecord) -> str:
    """What the page renders: raw (sanitized) HTML for 'html', Markdown
    for every other format."""
    stored = rec.stored or ""
    return stored if rec.format == "html" else get_format(rec.format).display(stored)


def _mutate(action: Callable[[], ArtifactRecord]) -> ArtifactRecord:
    try:
        return action()
    except MalformedArtifactId as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ArtifactNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc



def workflow_label(name: str) -> str:
    """A workflow's plain name, also for one switched off or deleted since
    its runs were written: its title where one is known, else its key in
    words ("weekly_laptop_restock" -> "Weekly laptop restock")."""
    from openexecutive.workflows import get_workflow
    from openexecutive.workflows.dynamic_store import get_definition

    with contextlib.suppress(Exception):
        return get_workflow(name).title
    with contextlib.suppress(Exception):
        defn = get_definition(name)
        if defn is not None and defn.title.strip():
            return defn.title.strip()
    words = name.removeprefix("quick_").replace("_", " ").strip()
    return words[:1].upper() + words[1:] if words else "Workflow"


def _group(rec: ArtifactRecord, collection_label: str | None, labels: dict[str, str]) -> tuple[str, str]:
    """The Documents page group a record files under, and its name.
    `labels` caches workflow names across one list."""
    if rec.collection:
        return f"c:{rec.collection}", collection_label or rec.collection
    if rec.kind == "draft":
        return "chats", _CHATS_LABEL
    if rec.source_label in _BRIEFS:
        return "briefs", _BRIEFS_LABEL
    if rec.source_label not in labels:
        labels[rec.source_label] = workflow_label(rec.source_label)
    return f"wf:{rec.source_label}", labels[rec.source_label]


def _title(rec: ArtifactRecord) -> str:
    """A run's title without the note on how it started."""
    if rec.kind != "workflow":
        return rec.title
    # The note on how a run started that the code appends to its name. Only
    # these: "Report (Q1)" keeps its "(Q1)".
    title = rec.title.rstrip()
    for note in _HOW_IT_STARTED:
        if title.endswith(note):
            title = title[: -len(note)].rstrip()
            break
    return title or rec.title


def _summary(
    rec: ArtifactRecord,
    labels: dict[str, str],
    viewer: Viewer | None = None,
    shared: dict[str, str] | None = None,
) -> ArtifactSummary:
    fmt = get_format(rec.format)
    collection = get_collection(rec.collection)
    preview = fmt.text(rec.stored)[:_PREVIEW_CHARS] if rec.stored else ""
    collection_label = collection.label if collection is not None else None
    group, group_label = _group(rec, collection_label, labels)
    can_share = viewer is not None and artifact_shares.may_share(rec, viewer)
    key = (shared or {}).get(rec.id)
    return ArtifactSummary(
        id=rec.id,
        kind=rec.kind,
        title=_title(rec),
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
        collection_label=collection_label,
        group=group,
        group_label=group_label,
        can_share=can_share,
        share_path=artifact_shares.public_path(key) if can_share and key else None,
    )
