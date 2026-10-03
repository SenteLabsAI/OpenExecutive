"""One read model over every artifact, shared by the HTTP routes and the tools.

Artifacts live in two existing tables — drafted documents in `alerts`
(`source='artifact'`) and workflow output in `workflow_runs.artifact` — and
are addressed by a composite id `"{kind}:{native_id}"` (`alert:<int>` /
`run:<hex>`). This module parses those ids, loads either kind into one
`ArtifactRecord`, and applies archive / delete, so `api/routes/artifacts.py`
and the Executive's `list_artifacts` / `get_artifact` tools can never drift
apart on what counts as an artifact.

Every read and change is on behalf of a `Viewer`, and an artifact the viewer
may not see is `ArtifactNotFound`, exactly like one that does not exist:

- A drafted artifact is its owner's alone (`alerts.owner_person_id`, the
  person whose conversation published it). One with no owner is the
  principal's. Not even the principal sees a teammate's.
- A workflow output follows its run (`persistence.run_visible_to`): a run
  someone started by hand is theirs; a scheduled or system run is the team's.

Errors are plain exceptions (`MalformedArtifactId`, `ArtifactNotFound`); the
route maps them to 400 / 404.
"""
from __future__ import annotations

import contextlib
import contextvars
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal

from openexecutive.alerts.models import Alert, artifact_visible_to
from openexecutive.alerts.store import (
    delete_alert,
    get_alert,
    list_artifact_alerts,
    set_alert_archived,
)
from openexecutive.alerts.store import (
    initialize_db as initialize_alerts_db,
)
from openexecutive.orchestrator.artifact_formats import (
    ARTIFACT_FORMATS,
    EXPORT_TARGETS,
    get_format,
)
from openexecutive.workflows.persistence import (
    delete_run,
    get_run,
    initialize_runs_db,
    list_artifact_runs,
    run_visible_to,
    set_run_archived,
)

DRAFT_SOURCE_LABEL = "Drafted by Executive"


class MalformedArtifactId(ValueError):
    pass


class ArtifactNotFound(LookupError):
    pass


@dataclass(frozen=True)
class Viewer:
    """Who an artifact read or change is for. ``person_id`` None is nobody on
    the roster: they see team workflow outputs and nothing else."""

    person_id: int | None
    is_principal: bool = False


NOBODY = Viewer(person_id=None)


def viewer_for_person(person_id: int | None) -> Viewer:
    """The viewer for a resolved caller (None: nobody). Fails closed: a
    roster that can't be read makes them no principal."""
    if person_id is None:
        return NOBODY
    from openexecutive.people.store import get_person

    try:
        person = get_person(person_id)
    except Exception:
        return Viewer(person_id=person_id)
    is_principal = bool(person is not None and person.is_principal and not person.archived)
    return Viewer(person_id=person_id, is_principal=is_principal)


def principal_viewer() -> Viewer:
    """The principal, for work the server does on its own (the scheduler, a
    workflow with nobody's conversation behind it, the CLI)."""
    from openexecutive.people.store import find_principal_person

    try:
        principal = find_principal_person()
    except Exception:
        return NOBODY
    if principal is None or principal.id is None:
        return NOBODY
    return Viewer(person_id=principal.id, is_principal=True)


# Whose documents work running outside any chat turn is for, when it is
# someone's (`pinned_viewer`): a run started on the Jobs page, a resumed run.
_PINNED: contextvars.ContextVar[Viewer | None] = contextvars.ContextVar(
    "artifact_viewer_pin", default=None
)


@contextlib.contextmanager
def pinned_viewer(viewer: Viewer) -> Iterator[None]:
    """Run the block as ``viewer``'s work: what it reads and drafts, and the
    runs it starts, are theirs (``current_viewer``, ``turn_owner``)."""
    token = _PINNED.set(viewer)
    try:
        yield
    finally:
        _PINNED.reset(token)


def current_viewer() -> Viewer:
    """Whose artifacts the work in progress may read and publish.

    In order: the viewer the session is pinned to
    (``Session.documents_viewer``: a workflow's own loop inside someone's
    turn); the speaker (nobody when they are not on the roster, whatever
    the work is pinned to); the principal on their own CLI; the viewer the
    work is pinned to (``pinned_viewer``: a Jobs-page or resumed run, or
    the alert review pinning the principal). Anything else is nobody — no session at all (a
    scheduled run, retrieval before a turn binds its session), an unattended
    run, an unrostered speaker — so no one's documents leak into work
    someone else may read.
    """
    from openexecutive.orchestrator.schedule_tools import current_session

    session = current_session.get()
    pinned = getattr(session, "documents_viewer", None)
    if isinstance(pinned, Viewer):
        return pinned
    person_id = getattr(session, "caller_person_id", None)
    if isinstance(person_id, int):
        return viewer_for_person(person_id)
    if _someone_speaking(session):
        # A speaker who is not on the roster never inherits the work's pin.
        return NOBODY
    if session is not None and getattr(session, "from_cli", False) is True:
        return principal_viewer()
    outer = _PINNED.get()
    if outer is not None:
        return outer
    return NOBODY


def _someone_speaking(session: Any) -> bool:
    return session is not None and bool(
        getattr(session, "from_web_chat", False) or getattr(session, "origin_channel", "")
    )


def turn_owner() -> int | None:
    """Who a run started now belongs to (``persistence.create_run``): the
    person whose work starts it by hand (``current_viewer``). None when it
    is nobody's: a team run."""
    return current_viewer().person_id


def runs_refused_for_nobody() -> bool:
    """Whether a run started now must be refused: someone is speaking but is
    nobody on the roster, so the run would have no owner and land in the
    team's history."""
    from openexecutive.orchestrator.schedule_tools import current_session

    return _someone_speaking(current_session.get()) and current_viewer().person_id is None


def draft_visible_to(owner_person_id: int | None, viewer: Viewer) -> bool:
    """Whether ``viewer`` may see a drafted artifact owned by ``owner_person_id``."""
    return artifact_visible_to(
        owner_person_id, viewer.person_id, is_principal=viewer.is_principal
    )


@dataclass(frozen=True)
class ArtifactRecord:
    id: str
    kind: Literal["draft", "workflow"]
    title: str
    source_label: str
    created_at: str
    status: str
    severity: str | None
    archived_at: str | None
    format: str
    # The stored text for `format`; None on list rows that skip the body
    # (workflow runs — the list query excludes it).
    stored: str | None
    rationale: str | None = None
    url: str | None = None
    link_label: str | None = None
    supersedes_id: str | None = None
    # Whose it is (module docstring); None on a draft = the principal's, on
    # a workflow output = the team's.
    owner_person_id: int | None = None


@dataclass(frozen=True)
class ArtifactFile:
    """A rendered download: what `/download` serves and email attaches."""

    content: bytes
    filename: str
    mime: str


_FILENAME_MAX = 60
_FILENAME_BAD = re.compile(r"[^a-z0-9]+")


def artifact_downloads(rec: ArtifactRecord) -> list[str]:
    """Download targets for an artifact; the first is its own format.

    Empty for formats with no file (links)."""
    own = get_format(rec.format)
    if own.render_file is None:
        return []
    return [own.name, *EXPORT_TARGETS.get(own.name, ())]


def render_artifact_file(rec: ArtifactRecord, as_: str | None = None) -> ArtifactFile:
    """Render `rec` as a file in its own format, or as export target `as_`.

    Raises `ArtifactNotFound` when the artifact has no such download (a link,
    or a target outside `artifact_downloads`). Rendering errors propagate.
    """
    targets = artifact_downloads(rec)
    target = (as_ or (targets[0] if targets else "")).strip().lower()
    if not targets or target not in targets:
        raise ArtifactNotFound(
            f"Artifact {rec.id!r} has no {target or 'file'} download"
        )
    fmt = ARTIFACT_FORMATS[target]
    assert fmt.render_file is not None and fmt.extension is not None
    return ArtifactFile(
        content=fmt.render_file(rec.stored or ""),
        filename=f"{_filename_stem(rec)}.{fmt.extension}",
        mime=fmt.mime,
    )


def _filename_stem(rec: ArtifactRecord) -> str:
    stem = _FILENAME_BAD.sub("-", rec.title.lower()).strip("-")[:_FILENAME_MAX].strip("-")
    if stem:
        return stem
    kind, native_id = parse_artifact_id(rec.id)
    return f"{kind}-{native_id[:12]}"


def parse_artifact_id(composite_id: str) -> tuple[str, str]:
    """Split `"{kind}:{native_id}"`, or raise `MalformedArtifactId`."""
    kind, sep, native_id = (composite_id or "").strip().partition(":")
    if not sep or kind not in ("alert", "run") or not native_id:
        raise MalformedArtifactId(f"Malformed artifact id: {composite_id!r}")
    return kind, native_id


def _record_from_alert(alert: Alert) -> ArtifactRecord:
    return ArtifactRecord(
        id=f"alert:{alert.id}",
        kind="draft",
        title=alert.headline,
        source_label=DRAFT_SOURCE_LABEL,
        created_at=alert.created_at,
        status=alert.status,
        severity=alert.severity,
        archived_at=alert.archived_at,
        format=get_format(alert.artifact_format).name,
        stored=alert.body,
        rationale=alert.suggested_action or None,
        url=alert.artifact_url,
        link_label=alert.artifact_link_label,
        supersedes_id=alert.supersedes_id,
        owner_person_id=alert.owner_person_id,
    )


def _record_from_run(run: dict[str, Any], *, with_body: bool) -> ArtifactRecord:
    return ArtifactRecord(
        id=f"run:{run['run_id']}",
        kind="workflow",
        title=run["title"],
        source_label=run["workflow_name"],
        created_at=run["created_at"],
        status="done",
        severity=None,
        archived_at=run.get("archived_at"),
        # Workflows always emit Markdown (workflows/base.py contract).
        format="markdown",
        stored=run.get("artifact") if with_body else None,
        owner_person_id=run.get("owner_person_id"),
    )


def _require_alert(native_id: str, composite_id: str, viewer: Viewer) -> Alert:
    try:
        alert_id = int(native_id)
    except ValueError as exc:
        raise MalformedArtifactId(f"Malformed artifact id: {composite_id!r}") from exc
    alert = get_alert(alert_id)
    # A non-artifact alert id must never resolve here, so neither the routes
    # nor the tools can become general alert readers / mutators. Someone
    # else's draft answers the same as a missing one.
    if (
        alert is None
        or alert.source != "artifact"
        or not draft_visible_to(alert.owner_person_id, viewer)
    ):
        raise ArtifactNotFound(f"Artifact {composite_id!r} not found")
    return alert


def _require_run(native_id: str, composite_id: str, viewer: Viewer) -> dict[str, Any]:
    run = get_run(native_id)
    # An empty body counts as no artifact.
    if run is None or not run.get("artifact") or not run_visible_to(run, viewer.person_id):
        raise ArtifactNotFound(f"Artifact {composite_id!r} not found")
    return run


def load_artifact(composite_id: str, *, viewer: Viewer) -> ArtifactRecord:
    kind, native_id = parse_artifact_id(composite_id)
    if kind == "alert":
        return _record_from_alert(_require_alert(native_id, composite_id, viewer))
    return _record_from_run(_require_run(native_id, composite_id, viewer), with_body=True)


def list_artifacts(
    limit: int, *, viewer: Viewer, archived: bool = False
) -> list[ArtifactRecord]:
    """Both sources merged, newest first, capped at `limit`: only what
    `viewer` may see."""
    initialize_alerts_db()
    initialize_runs_db()
    items = [
        _record_from_alert(a)
        for a in list_artifact_alerts(
            limit=limit,
            archived=archived,
            owner_person_id=viewer.person_id,
            include_unowned=viewer.is_principal,
        )
    ]
    items.extend(
        _record_from_run(r, with_body=False)
        for r in list_artifact_runs(
            limit=limit, archived=archived, visible_to=viewer.person_id
        )
    )
    items.sort(key=lambda a: a.created_at, reverse=True)
    return items[:limit]


def set_archived(composite_id: str, *, archived: bool, viewer: Viewer) -> ArtifactRecord:
    record = load_artifact(composite_id, viewer=viewer)
    kind, native_id = parse_artifact_id(composite_id)
    if kind == "alert":
        set_alert_archived(int(native_id), archived)
    else:
        set_run_archived(native_id, archived)
    return record


def delete_artifact(composite_id: str, *, viewer: Viewer) -> ArtifactRecord:
    record = load_artifact(composite_id, viewer=viewer)
    kind, native_id = parse_artifact_id(composite_id)
    if kind == "alert":
        delete_alert(int(native_id))
    else:
        delete_run(native_id)
    return record


__all__ = [
    "DRAFT_SOURCE_LABEL",
    "NOBODY",
    "ArtifactFile",
    "ArtifactNotFound",
    "ArtifactRecord",
    "MalformedArtifactId",
    "Viewer",
    "artifact_downloads",
    "current_viewer",
    "delete_artifact",
    "draft_visible_to",
    "list_artifacts",
    "load_artifact",
    "parse_artifact_id",
    "principal_viewer",
    "render_artifact_file",
    "set_archived",
    "turn_owner",
    "pinned_viewer",
    "runs_refused_for_nobody",
    "viewer_for_person",
]
