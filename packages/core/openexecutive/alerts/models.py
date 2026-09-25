from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class AlertSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


SEVERITY_RANK: dict[str, int] = {
    AlertSeverity.LOW.value: 0,
    AlertSeverity.MEDIUM.value: 1,
    AlertSeverity.HIGH.value: 2,
    AlertSeverity.URGENT.value: 3,
}


class AlertChannel(StrEnum):
    WEB = "web"
    SLACK_DM = "slack_dm"
    EMAIL = "email"
    PERSISTED = "persisted"
    # Shift 3 — broadcast channels. The triage agent picks these when the
    # alert is dept-scoped (no single human owner) or company-wide. The
    # dispatcher reads the accompanying TriageDecision.department_slug /
    # broadcast_integration fields to know which room to post to.
    DEPARTMENT_CHANNEL = "department_channel"
    COMPANY_BROADCAST = "company_broadcast"


class AlertEvent(BaseModel):
    """An external event that may or may not become an alert."""

    model_config = ConfigDict(extra="ignore")

    source: str  # "email" | "slack" | "document"
    external_id: str  # source-system id for idempotency
    subject: str = ""
    body: str = ""
    from_: str | None = Field(default=None, alias="from")
    channel: str | None = None
    user: str | None = None
    title: str | None = None  # for documents
    # Producer-supplied dedup key that overrides the triage model's own
    # (monitoring sets ``watch:<slug>`` so one watch = one open alert).
    dedup_hint: str = ""
    # Person the alert should be routed to (assigned_to_person_id from the
    # Executive's create_alert tool). Lands on the row so the "principal
    # owns unrouted" rule stops sweeping every triage-born alert to them.
    routed_to_person_id: int | None = None
    # Department the alert belongs to (slug). Becomes the row's
    # ``department:<slug>`` topic tag — a routing hint only, unlike
    # ``channel``, which triage reads as the room a message came from and may
    # answer with a team-room broadcast.
    department: str = ""


class TriageDecision(BaseModel):
    """Structured output of the Triage agent."""

    model_config = ConfigDict(extra="ignore")

    alert: bool
    severity: AlertSeverity = AlertSeverity.LOW
    channels: list[AlertChannel] = Field(default_factory=list)
    headline: str = ""
    body: str = ""
    suggested_action: str = ""
    topic_tags: list[str] = Field(default_factory=list)
    dedup_key: str = ""
    reason_if_suppressed: str = ""
    # Shift 3 broadcast routing. Set when `channels` contains
    # DEPARTMENT_CHANNEL or COMPANY_BROADCAST so the dispatcher knows
    # WHERE to post. Empty strings when the alert is per-person or
    # web-only — those channels never read these fields.
    department_slug: str = ""
    broadcast_integration: str = ""


class Alert(BaseModel):
    """A persisted alert row."""

    model_config = ConfigDict(extra="ignore")

    id: int | None = None
    external_id: str = ""
    source: str
    severity: str
    headline: str
    body: str
    suggested_action: str = ""
    topic_tags: list[str] = Field(default_factory=list)
    channels_attempted: list[str] = Field(default_factory=list)
    channels_delivered: list[str] = Field(default_factory=list)
    dedup_key: str = ""
    # unread | read | ack | dismissed | resolved | expired. `ack` means the
    # user approved (the Executive executes the suggested action);
    # `resolved` means the Executive's review closed it with evidence;
    # `expired` means the lifecycle sweep aged it out (alerts/lifecycle.py).
    status: str = "unread"
    created_at: str
    # Phase 4: person the alert is routed to for approval. NULL means the
    # alert is general (not routed to a specific approver).
    routed_to_person_id: int | None = None
    # Soft-delete for the Executive Artifacts gallery. NULL = active; an ISO
    # timestamp means the artifact was archived (hidden from the default list
    # but restorable). Only meaningful for source='artifact' rows.
    archived_at: str | None = None
    # Lifecycle (alerts/lifecycle.py + alerts/review.py). All additive with
    # defaults so rows written by older builds read back unchanged.
    # Coalescing: a repeat of an open alert bumps these instead of stacking.
    last_seen_at: str | None = None
    occurrence_count: int = 1
    # Executive review verdict: '' | relevant | changed | likely_stale |
    # drafted | routed | merged, plus resolved | stale on rows the review
    # closed; `review_note` is the one-line "what changed since
    # you last looked"; `recommended_move` the next move the card leads with;
    # `why_now` a short urgency note; `due_at` an ISO deadline when one exists.
    last_reviewed_at: str | None = None
    review_verdict: str = ""
    review_note: str = ""
    recommended_move: str = ""
    why_now: str = ""
    due_at: str | None = None
    # Set on a row folded into another (merge): the surviving alert's id.
    superseded_by_alert_id: int | None = None
    # Hidden from the live queue until this ISO timestamp (snooze).
    snoozed_until: str | None = None
    # Registry workflow the review suggested as the next step ('' = none).
    suggested_workflow: str = ""
    # Artifact format + link metadata (source='artifact' rows only; see
    # orchestrator/artifact_formats.py). Legacy rows read back as Markdown.
    artifact_format: str = "markdown"
    artifact_url: str | None = None
    artifact_link_label: str | None = None
    # Composite id ('alert:<n>' / 'run:<hex>') of the version this revised.
    supersedes_id: str | None = None


class UserPreferences(BaseModel):
    model_config = ConfigDict(extra="ignore")

    severity_threshold: AlertSeverity = AlertSeverity.MEDIUM
    quiet_hours_start: str = ""  # "22:00"
    quiet_hours_end: str = ""  # "07:00"
    # IANA zone the quiet hours are read in; empty = the user's zone
    # (memory.workspace_settings.get_user_timezone).
    quiet_hours_tz: str = ""
    channels_enabled: list[AlertChannel] = Field(
        default_factory=lambda: [
            AlertChannel.WEB,
            AlertChannel.SLACK_DM,
            AlertChannel.EMAIL,
            AlertChannel.PERSISTED,
        ]
    )


class MuteTopic(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int | None = None
    pattern: str
    created_at: str


