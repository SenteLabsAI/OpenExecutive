"""One read model over every artifact, shared by the HTTP routes and the tools.

Artifacts live in two existing tables — drafted documents in `alerts`
(`source='artifact'`) and workflow output in `workflow_runs.artifact` — and
are addressed by a composite id `"{kind}:{native_id}"` (`alert:<int>` /
`run:<hex>`). This module parses those ids, loads either kind into one
`ArtifactRecord`, and applies archive / delete, so `api/routes/artifacts.py`
and the Executive's `list_artifacts` / `get_artifact` tools can never drift
apart on what counts as an artifact.

Errors are plain exceptions (`MalformedArtifactId`, `ArtifactNotFound`); the
route maps them to 400 / 404.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from openexecutive.alerts.models import Alert
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
    set_run_archived,
)

DRAFT_SOURCE_LABEL = "Drafted by Executive"


class MalformedArtifactId(ValueError):
    pass


class ArtifactNotFound(LookupError):
    pass


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
    )


def _require_alert(native_id: str, composite_id: str) -> Alert:
    try:
        alert_id = int(native_id)
    except ValueError as exc:
        raise MalformedArtifactId(f"Malformed artifact id: {composite_id!r}") from exc
    alert = get_alert(alert_id)
    # A non-artifact alert id must never resolve here, so neither the routes
    # nor the tools can become general alert readers / mutators.
    if alert is None or alert.source != "artifact":
        raise ArtifactNotFound(f"Artifact {composite_id!r} not found")
    return alert


def _require_run(native_id: str, composite_id: str) -> dict[str, Any]:
    run = get_run(native_id)
    # An empty body counts as no artifact.
    if run is None or not run.get("artifact"):
        raise ArtifactNotFound(f"Artifact {composite_id!r} not found")
    return run


def load_artifact(composite_id: str) -> ArtifactRecord:
    kind, native_id = parse_artifact_id(composite_id)
    if kind == "alert":
        return _record_from_alert(_require_alert(native_id, composite_id))
    return _record_from_run(_require_run(native_id, composite_id), with_body=True)


def list_artifacts(limit: int, *, archived: bool = False) -> list[ArtifactRecord]:
    """Both sources merged, newest first, capped at `limit`."""
    initialize_alerts_db()
    initialize_runs_db()
    items = [_record_from_alert(a) for a in list_artifact_alerts(limit=limit, archived=archived)]
    items.extend(
        _record_from_run(r, with_body=False)
        for r in list_artifact_runs(limit=limit, archived=archived)
    )
    items.sort(key=lambda a: a.created_at, reverse=True)
    return items[:limit]


def set_archived(composite_id: str, *, archived: bool) -> ArtifactRecord:
    record = load_artifact(composite_id)
    kind, native_id = parse_artifact_id(composite_id)
    if kind == "alert":
        set_alert_archived(int(native_id), archived)
    else:
        set_run_archived(native_id, archived)
    return record


def delete_artifact(composite_id: str) -> ArtifactRecord:
    record = load_artifact(composite_id)
    kind, native_id = parse_artifact_id(composite_id)
    if kind == "alert":
        delete_alert(int(native_id))
    else:
        delete_run(native_id)
    return record


__all__ = [
    "DRAFT_SOURCE_LABEL",
    "ArtifactFile",
    "ArtifactNotFound",
    "ArtifactRecord",
    "MalformedArtifactId",
    "artifact_downloads",
    "delete_artifact",
    "list_artifacts",
    "load_artifact",
    "parse_artifact_id",
    "render_artifact_file",
    "set_archived",
]
