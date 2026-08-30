"""Client-slot machinery: save/restore the full company context by name.

A slot directory (``company/_client_slots/<slug>/``) is a faithful save file
of one client company:

- ``state.db`` — transactionally-consistent copy of the shared SQLite DB
  (``VACUUM INTO`` on save, online backup API on restore). This carries chat
  history, scheduled actions, onboarding plans, talent pipeline, departments,
  people, watchlist — everything the stores keep.
- ``profile.yaml`` / ``docs/`` / ``skills/`` — the company directory artifacts.
- ``mcp_servers.json`` — per-client external MCP tools (e.g. one client's
  Crayon credentials). The MCP gateway reads this at process startup, so a
  changed config takes effect on the next restart.
- ``meta.json`` — display name + timestamps.

Slots deliberately reuse the fixture switcher's primitives and its
destructive-op lock (``_FIXTURE_OP_LOCK``): fixture loads, snapshots, resets,
and slot switches all mutate the same live state, so they must serialize
against each other.

Invariants:

- At most one slot is *active*; the ``.active_client`` sentinel records it.
- Only the active client is "live" — its scheduled actions fire, its docs are
  indexed. Parked clients sleep in their slot dirs.
- A fixture and a client slot are never active simultaneously: slot operations
  refuse while a demo fixture is loaded, and loading a fixture saves the
  active client back to its slot first (see ``cli/fixture_loader.py``).
- The ``generated_fixtures`` table is operator-level (the demo/fixture
  library), not client data — it is preserved verbatim across slot switches.
"""
from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openexecutive.cli.fixture_loader import (
    _FIXTURE_OP_LOCK,
    _SAFE_NAME_RE,
    get_fixture_status,
    snapshot_user_state,
)

logger = logging.getLogger(__name__)

# Honcho workspaces for client slots are stable (no uuid suffix) so a client's
# peer memory survives parking/reactivating. The prefix lets the fixture
# loader recognise them and never tear them down the way it does the
# sacrificial per-fixture demo workspaces.
CLIENT_WORKSPACE_PREFIX = "openexec-client-"

# Tables preserved verbatim across slot restores — operator-level state that
# does not belong to any one client company.
_GLOBAL_TABLES = ("generated_fixtures",)

# Engagement metadata lives in meta.json — deliberately OUTSIDE the swapped
# client state, so the practice layer (cockpit, renewal awareness) can see
# every engagement regardless of which client is active. Renewal reminders
# must never be modeled as scheduled_actions: those swap with the client and
# would vanish the moment the client is parked.
ENGAGEMENT_META_FIELDS: frozenset[str] = frozenset(
    {
        "role",              # what you are for this client (e.g. "Fractional CFO")
        "status",            # engagement stage — see ENGAGEMENT_STATUSES
        "engagement_start",  # ISO date
        "renewal_date",      # ISO date — next renewal/review checkpoint
        "retainer",          # free text, display only (billing stays external)
        "hours_per_week",    # number, display only
        "primary_contact",   # name of the client-side contact
        "notes",             # free-form engagement notes
    }
)

ENGAGEMENT_STATUSES = ("active", "paused", "winding_down", "completed")

# Per-client tables wiped when activating a *blank* slot (no state.db yet).
# Ordered children-before-parents for PRAGMA foreign_keys=ON. Existence-guarded
# at delete time, so stores that haven't initialized on this box are skipped.
_BLANK_WIPE_TABLES = (
    "chat_messages",
    "sessions",
    "decisions",
    "initiatives",
    "advice_given",
    "scheduled_actions",
    "voice_personas",
    "alerts",
    "mute_topics",
    "user_preferences",
    "workflow_runs",
    "audit_log",
    "eval_runs",
    "external_signals",
    "watchlist",
    "page_watch_state",
    "outbound_context",
    "onboarding_tasks",
    "onboarding_plans",
    "onboarding_templates",
    "offers",
    "candidates",
    "engagements",
    "decision_instances",
    "decision_class_state",
    "agent_override_history",
    "agent_overrides",
    "review_annotations",
    "review_items",
    "person_authority_scope",
    "person_availability",
    "people",
    "department_goals",
    "departments",
    "departments_meta",
)


class ClientSlotError(ValueError):
    """Base error for slot operations (maps to HTTP 400)."""


class ClientSlotNotFoundError(ClientSlotError):
    """The named slot does not exist (maps to HTTP 404)."""


class ClientSlotConflictError(ClientSlotError):
    """The operation conflicts with current state (maps to HTTP 409)."""


def _clients_root(settings: Any) -> Path:
    """Slot storage lives inside the gitignored company dir, as a sibling of
    ``_user_backup`` — client data must never be committable."""
    return settings.company_profile_path.parent / "_client_slots"


def _active_client_sentinel(settings: Any) -> Path:
    return _clients_root(settings) / ".active_client"


def _slot_dir(settings: Any, slug: str) -> Path:
    return _clients_root(settings) / slug


def _episodic_db_path() -> Path:
    # Resolved lazily so tests can monkeypatch ``memory.episodic.DB_PATH``.
    from openexecutive.memory.episodic import DB_PATH

    return Path(str(DB_PATH))


def get_active_client(settings: Any) -> str | None:
    """The active slot slug, or None when running single-company / fixture mode."""
    sentinel = _active_client_sentinel(settings)
    if not sentinel.exists():
        return None
    try:
        content = sentinel.read_text().strip()
    except Exception:
        return None
    # Same defence-in-depth as the fixture sentinel: garbage never reaches the UI.
    if content and _SAFE_NAME_RE.match(content):
        return content
    return None


def list_client_slots(settings: Any) -> list[dict[str, Any]]:
    """Summaries of all slots, newest-saved first."""
    root = _clients_root(settings)
    if not root.exists():
        return []

    out: list[dict[str, Any]] = []
    for slot in sorted(root.iterdir()):
        if not slot.is_dir() or not _SAFE_NAME_RE.match(slot.name):
            continue
        meta = _read_meta(slot)
        summary: dict[str, Any] = {
            "slug": slot.name,
            "display_name": meta.get("display_name") or slot.name,
            "created_at": meta.get("created_at"),
            "saved_at": meta.get("saved_at"),
            "origin": meta.get("origin"),
            **{field: meta.get(field) for field in ENGAGEMENT_META_FIELDS},
            "has_state": (slot / "state.db").exists(),
            "has_mcp_config": (slot / "mcp_servers.json").exists(),
            "doc_count": len(list((slot / "docs").glob("*")))
            if (slot / "docs").exists()
            else 0,
        }
        profile_path = slot / "profile.yaml"
        if profile_path.exists():
            try:
                from openexecutive.memory.company_profile import CompanyProfile

                profile = CompanyProfile.load_from_yaml(profile_path)
                summary["industry"] = profile.industry
                summary["stage"] = profile.stage
            except Exception:
                pass
        out.append(summary)

    out.sort(key=lambda s: s.get("saved_at") or "", reverse=True)
    return out


def _read_meta(slot: Path) -> dict[str, Any]:
    meta_path = slot / "meta.json"
    if not meta_path.exists():
        return {}
    try:
        data = json.loads(meta_path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_meta(slot: Path, **updates: Any) -> None:
    meta = _read_meta(slot)
    meta.update(updates)
    (slot / "meta.json").write_text(json.dumps(meta, indent=2, default=str))


async def update_client_meta(
    settings: Any, slug: str, patch: dict[str, Any]
) -> dict[str, Any]:
    """Update a slot's engagement metadata (allowlisted fields only).

    Metadata edits are valid for any slot — active or parked — because
    meta.json is practice-level state, never swapped with the client. Takes
    the shared lock only to serialize the read-modify-write against a
    concurrent save-back touching the same meta.json.
    """
    unknown = set(patch) - ENGAGEMENT_META_FIELDS
    if unknown:
        raise ClientSlotError(
            f"Unknown metadata fields: {', '.join(sorted(unknown))}. "
            f"Allowed: {', '.join(sorted(ENGAGEMENT_META_FIELDS))}"
        )
    status = patch.get("status")
    if status is not None and status not in ENGAGEMENT_STATUSES:
        raise ClientSlotError(
            f"status must be one of: {', '.join(ENGAGEMENT_STATUSES)}"
        )
    if "display_name" in patch:  # defence in depth — not in the allowlist
        raise ClientSlotError("display_name cannot be changed here")

    async with _FIXTURE_OP_LOCK:
        slot = _require_slot(settings, slug)
        _write_meta(slot, **patch)
        meta = _read_meta(slot)
        return {
            "slug": slug,
            **{field: meta.get(field) for field in ENGAGEMENT_META_FIELDS},
        }


def derive_client_slug(display_name: str, settings: Any) -> str:
    """Filesystem-safe unique slug for a new slot (reuses the fixture slugifier)."""
    from openexecutive.fixtures.generator import derive_slug

    return derive_slug(display_name, lambda s: _slot_dir(settings, s).exists())


# ── Save: live state → slot dir ─────────────────────────────────────────────


def _save_slot_state(settings: Any, slot: Path) -> dict[str, Any]:
    """Write the full live company context into ``slot``. Caller holds the lock."""
    slot.mkdir(parents=True, exist_ok=True)
    company_dir: Path = settings.company_profile_path.parent

    # 1. profile.yaml — always write one so the slot stays loadable.
    from openexecutive.memory.company_profile import CompanyProfile

    if settings.company_profile_path.exists():
        shutil.copy2(settings.company_profile_path, slot / "profile.yaml")
    else:
        CompanyProfile().save_to_yaml(slot / "profile.yaml")

    # 2. docs/ + skills/ — full directory copies (all file types, unlike the
    #    fixture snapshot's *.md filter: slots are save files, not demo data).
    docs_copied = _replace_dir_copy(company_dir / "docs", slot / "docs")
    _replace_dir_copy(company_dir / "skills", slot / "skills")

    # 3. mcp_servers.json — per-client external tool config.
    mcp_src = Path(settings.mcp_servers_config_path)
    mcp_dst = slot / "mcp_servers.json"
    if mcp_src.exists():
        shutil.copy2(mcp_src, mcp_dst)
    else:
        mcp_dst.unlink(missing_ok=True)

    # 4. state.db — VACUUM INTO produces a transactionally-consistent copy
    #    even while other connections hold the live DB open.
    db_path = _episodic_db_path()
    state_dst = slot / "state.db"
    state_dst.unlink(missing_ok=True)
    db_saved = False
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("VACUUM INTO ?", (str(state_dst),))
            db_saved = True
        finally:
            conn.close()

    _write_meta(slot, saved_at=datetime.now(UTC).isoformat())
    return {"docs_saved": docs_copied, "db_saved": db_saved}


def _replace_dir_copy(src: Path, dst: Path) -> int:
    """Replace ``dst`` with a copy of ``src``. Returns files copied (0 if no src)."""
    if dst.exists():
        shutil.rmtree(dst)
    if not src.exists():
        return 0
    shutil.copytree(src, dst)
    return sum(1 for p in dst.rglob("*") if p.is_file())


# ── Restore: slot dir → live state ───────────────────────────────────────────


async def _restore_slot_state(
    settings: Any, slot: Path, *, app_state: Any | None = None
) -> dict[str, Any]:
    """Make ``slot`` the live company context. Caller holds the lock."""
    company_dir: Path = settings.company_profile_path.parent
    # "Blank" here means "no state.db yet" — true for both empty blank slots
    # and generated seed slots (which carry YAML/JSON seed files instead).
    # Once any slot has been saved back, state.db exists and wins.
    is_blank = not (slot / "state.db").exists()

    # 1. SQLite state — whole-DB restore (or factory wipe for blank slots),
    #    with operator-level tables carried across.
    preserved = _dump_global_tables()
    if is_blank:
        _wipe_per_client_tables()
    else:
        _restore_db_from_file(slot / "state.db")
    _ensure_schemas()
    _restore_global_tables(preserved)
    seeded: dict[str, Any] = {}
    if is_blank:
        # Generated (seed) slots carry people.yaml / departments.yaml /
        # memory.json from the intake draft — apply them with the fixture
        # loader's seeders (people first so memory + department heads can
        # resolve person ids by name), then layer the boot-time defaults on
        # top, skipping the default org when the draft supplied one.
        seeded = _seed_from_slot_files(settings, slot)
        _reseed_blank_defaults(
            seed_departments=not bool(seeded.get("departments_seeded"))
        )

    # 2. Company directory artifacts.
    from openexecutive.memory.company_profile import CompanyProfile

    profile_path = slot / "profile.yaml"
    profile = (
        CompanyProfile.load_from_yaml(profile_path)
        if profile_path.exists()
        else CompanyProfile()
    )
    profile.save_to_yaml(settings.company_profile_path)
    _replace_dir_copy(slot / "docs", company_dir / "docs")
    (company_dir / "docs").mkdir(parents=True, exist_ok=True)
    _replace_dir_copy(slot / "skills", company_dir / "skills")

    mcp_src = slot / "mcp_servers.json"
    mcp_live = Path(settings.mcp_servers_config_path)
    mcp_changed = mcp_src.exists() or mcp_live.exists()
    if mcp_src.exists():
        mcp_live.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(mcp_src, mcp_live)
    else:
        mcp_live.unlink(missing_ok=True)

    # 3. Vector state — rebuild the company collections from the restored dirs.
    docs_indexed = await _rebuild_vector_state(settings, app_state)

    return {
        "display_name": profile.name,
        "profile": profile.model_dump(),
        "docs_indexed": docs_indexed,
        "blank": is_blank,
        # The gateway reads mcp_servers.json at process startup; flag so the
        # caller/UI can tell the operator a restart applies the new config.
        "mcp_config_changed": mcp_changed,
        **seeded,
    }


def _seed_from_slot_files(settings: Any, slot: Path) -> dict[str, Any]:
    """Apply a seed slot's people/memory/departments files to the live DB.

    A slot created from an engagement-intake bundle has no ``state.db`` yet —
    its org and history live in the same YAML/JSON artifacts curated fixtures
    use, so the fixture loader's seeders apply them verbatim. Plain blank
    slots have none of these files and this is a no-op. The files remain in
    the slot afterward as the engagement's birth record; once the first
    save-back writes ``state.db`` they are no longer consulted.
    """
    from openexecutive.cli.fixture_loader import (
        _seed_departments,
        _seed_episodic_memory,
        _seed_people,
    )

    out: dict[str, Any] = {}
    if (slot / "people.yaml").exists():
        out["people_seeded"] = _seed_people(slot / "people.yaml")
    if (slot / "memory.json").exists():
        out["memory_seeded"] = _seed_episodic_memory(slot / "memory.json", settings)
    if (slot / "departments.yaml").exists():
        out["departments_seeded"] = _seed_departments(slot / "departments.yaml")
    return out


def _restore_db_from_file(state_src: Path) -> None:
    """Replace the live DB's content with the slot copy via the backup API.

    The backup API writes through a destination *connection*, so the live
    file's inode never changes — connections other code holds stay valid
    (they just see the new content), unlike a file swap which would strand
    them on an orphaned inode.
    """
    db_path = _episodic_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(state_src))
    dst = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _wipe_per_client_tables() -> None:
    """Factory-wipe per-client tables (blank-slot activation). Existence-guarded."""
    db_path = _episodic_db_path()
    if not db_path.exists():
        return
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        existing = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for table in _BLANK_WIPE_TABLES:
            if table in existing:
                conn.execute(f"DELETE FROM {table}")  # noqa: S608 — fixed allowlist
        conn.commit()
    finally:
        conn.close()


def _dump_global_tables() -> dict[str, tuple[list[str], list[tuple[Any, ...]]]]:
    """Read operator-level table rows from the live DB before a restore."""
    db_path = _episodic_db_path()
    out: dict[str, tuple[list[str], list[tuple[Any, ...]]]] = {}
    if not db_path.exists():
        return out
    conn = sqlite3.connect(str(db_path))
    try:
        for table in _GLOBAL_TABLES:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not exists:
                continue
            cursor = conn.execute(f"SELECT * FROM {table}")  # noqa: S608 — fixed allowlist
            cols = [d[0] for d in cursor.description]
            out[table] = (cols, cursor.fetchall())
    finally:
        conn.close()
    return out


def _restore_global_tables(
    preserved: dict[str, tuple[list[str], list[tuple[Any, ...]]]],
) -> None:
    """Write the operator-level rows back after the restore replaced the DB."""
    if not preserved:
        return
    db_path = _episodic_db_path()
    conn = sqlite3.connect(str(db_path))
    try:
        for table, (cols, rows) in preserved.items():
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not exists:
                continue
            conn.execute(f"DELETE FROM {table}")  # noqa: S608 — fixed allowlist
            if rows:
                placeholders = ",".join("?" for _ in cols)
                col_list = ",".join(cols)
                conn.executemany(
                    f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})",  # noqa: S608
                    rows,
                )
        conn.commit()
    finally:
        conn.close()


def _ensure_schemas() -> None:
    """Re-run idempotent schema inits after a restore.

    A slot saved before a schema-adding deploy lacks the new tables; the
    lifespan inits only run at boot, so re-run them here (same set, same
    order — people before departments for FK ordering).
    """
    from openexecutive.agents.overrides import initialize_overrides_db
    from openexecutive.alerts.store import initialize_db as init_alerts
    from openexecutive.departments.store import initialize_db as init_departments
    from openexecutive.fixtures.store import initialize_db as init_fixtures
    from openexecutive.knowledge.review_store import ReviewStore
    from openexecutive.memory.episodic import initialize_db as init_episodic
    from openexecutive.monitoring.store import initialize_db as init_monitoring
    from openexecutive.people.store import initialize_db as init_people
    from openexecutive.staff_onboarding.store import initialize_db as init_onboarding
    from openexecutive.talent.store import initialize_db as init_talent

    # Pass the path explicitly everywhere: some initializers bind their
    # DB_PATH default at import time, which would ignore a runtime override.
    db_path = _episodic_db_path()
    init_episodic(db_path)
    init_alerts(db_path)
    init_fixtures(db_path)
    initialize_overrides_db(db_path)
    init_people(db_path)
    init_talent(db_path)
    init_onboarding(db_path)
    init_departments(db_path)
    init_monitoring(db_path)
    ReviewStore.initialize_db(db_path)


def _reseed_blank_defaults(*, seed_departments: bool = True) -> None:
    """Blank slot = factory state: default org + the boot-time scheduled rows.

    Mirrors ``reset_all_state`` step 5/5a — without these the new client's
    Today page stays blank until the next process restart. Every call is
    idempotent and individually guarded. ``seed_departments=False`` skips the
    default 8-department org (used when a generated seed slot supplied its
    own departments — the cadence/brief bootstraps still run and pick those
    up from the table).
    """
    from openexecutive.config import get_settings
    from openexecutive.departments.store import seed_default_departments

    if seed_departments:
        try:
            seed_default_departments()
        except Exception:
            logger.exception("client-slots: seed_default_departments failed")
    try:
        from openexecutive.staff_onboarding.seed import seed_default_templates

        seed_default_templates()
    except Exception:
        logger.exception("client-slots: seed_default_templates failed")
    try:
        from openexecutive.scheduler.runner import seed_principal_briefs

        seed_principal_briefs()
    except Exception:
        logger.exception("client-slots: seed_principal_briefs failed")
    try:
        from openexecutive.departments.cadence import bootstrap_cadences

        bootstrap_cadences()
    except Exception:
        logger.exception("client-slots: bootstrap_cadences failed")
    settings = get_settings()
    if settings.nudge_scan_enabled:
        try:
            from openexecutive.scheduler.nudge_engine import bootstrap_nudge_scan

            bootstrap_nudge_scan()
        except Exception:
            logger.exception("client-slots: bootstrap_nudge_scan failed")
    if settings.external_monitor_enabled:
        try:
            from openexecutive.monitoring.pipeline import (
                bootstrap_external_monitor_scan,
            )

            bootstrap_external_monitor_scan()
        except Exception:
            logger.exception("client-slots: bootstrap_external_monitor_scan failed")
    if settings.watchlist_research_enabled:
        try:
            from openexecutive.monitoring.research.scheduler import (
                bootstrap_watchlist_research_scan,
            )

            bootstrap_watchlist_research_scan()
        except Exception:
            logger.exception(
                "client-slots: bootstrap_watchlist_research_scan failed"
            )


async def _rebuild_vector_state(settings: Any, app_state: Any | None) -> int:
    """Rebuild ChromaDB company collections from the restored live dirs.

    Returns the number of company-doc chunks indexed. Isolated here so tests
    can stub the vector layer without touching the file/DB round-trip logic.
    """
    from openexecutive.knowledge.loader import ingest_file
    from openexecutive.knowledge.skills_index import SKILLS_COLLECTION, index_skill
    from openexecutive.knowledge.skills_repo import list_skills
    from openexecutive.knowledge.store import ChromaDBStore

    store = ChromaDBStore(persist_directory=settings.vector_store_path)
    store.delete_company_docs()
    # Per-company research artifacts never carry across companies.
    store.delete_documents(
        collection=ChromaDBStore.RESEARCH_COLLECTION,
        where={"type": "recent_research"},
    )
    store.delete_notion_docs()
    from openexecutive.knowledge.notion_sync import reset_local_state

    reset_local_state(profile_path=settings.company_profile_path)

    company_docs_dir: Path = settings.company_profile_path.parent / "docs"
    docs_indexed = 0
    for doc in sorted(company_docs_dir.glob("*.md")):
        docs_indexed += await ingest_file(
            path=doc, store=store, collection=ChromaDBStore.COMPANY_COLLECTION
        )

    # Company-authored skills: drop the old client's rows, index the restored set.
    store.delete_documents(collection=SKILLS_COLLECTION, where={"source": "company"})
    for skill in list_skills():
        if skill.source == "company":
            try:
                index_skill(skill, store)
            except Exception:
                logger.exception("client-slots: reindex skill failed")

    if app_state is not None and hasattr(app_state, "store"):
        app_state.store = ChromaDBStore(persist_directory=settings.vector_store_path)
    return docs_indexed


def _set_honcho_client_workspace(slug: str) -> str | None:
    """Point Honcho at the client's stable workspace. Best-effort.

    Stable (no uuid) so the client's peer memory survives parking. The fixture
    loader's teardown paths skip ``CLIENT_WORKSPACE_PREFIX`` workspaces, so
    this memory is durable until the slot is deleted.
    """
    try:
        from openexecutive.memory.honcho_client import set_active_workspace_id

        workspace = f"{CLIENT_WORKSPACE_PREFIX}{slug}"
        set_active_workspace_id(workspace)
        return workspace
    except Exception:
        logger.exception("client-slots: honcho workspace switch failed")
        return None


def _write_generated_slot(
    slot: Path, bundle: Any, intake_description: str
) -> None:
    """Materialize a validated intake bundle into ``slot`` as a seed slot.

    profile.yaml + docs/ are read by the normal restore path;
    people.yaml / departments.yaml / memory.json are applied by
    ``_seed_from_slot_files`` on first activation (no ``state.db`` yet).
    """
    from openexecutive.fixtures.generator import (
        bundle_to_serialized,
        materialize_to_dir,
    )

    serialized = bundle_to_serialized(bundle, intake_description)
    materialize_to_dir(serialized, slot)
    _write_meta(
        slot,
        origin="generated",
        intake_description=intake_description,
        doc_count=serialized.get("doc_count", 0),
    )


def park_active_client(settings: Any) -> str | None:
    """Save the active client to its slot and leave client mode.

    Used by the fixture switcher before it replaces live state (load and
    unload both call this) so demo flows can never destroy client work.
    Returns the parked slug, or None when no client was active. The caller
    MUST already hold ``_FIXTURE_OP_LOCK`` — this helper deliberately takes
    no lock so it can run inside the fixture loader's critical sections.
    """
    active = get_active_client(settings)
    if active is None:
        return None
    slot = _slot_dir(settings, active)
    if slot.is_dir():
        _save_slot_state(settings, slot)
    _active_client_sentinel(settings).unlink(missing_ok=True)
    logger.info("client-slots: parked active client %r to its slot", active)
    return active


# ── Public operations (each holds the shared destructive-op lock) ───────────


def _require_no_fixture(settings: Any) -> None:
    active_fixture = get_fixture_status(settings).get("active_fixture")
    if active_fixture:
        raise ClientSlotConflictError(
            f"Demo fixture {active_fixture!r} is active — live state is fixture "
            "data, not client data. Unload it (POST /fixtures/unload) first."
        )


def _require_slot(settings: Any, slug: str) -> Path:
    if not _SAFE_NAME_RE.match(slug):
        raise ClientSlotError("Invalid client slug")
    slot = _slot_dir(settings, slug)
    if not slot.is_dir() or not (slot / "meta.json").exists():
        raise ClientSlotNotFoundError(f"Client {slug!r} not found")
    return slot


async def create_client_slot(
    settings: Any,
    *,
    display_name: str,
    slug: str | None = None,
    source: str = "current",
    bundle: dict[str, Any] | None = None,
    intake_description: str = "",
) -> dict[str, Any]:
    """Create a slot from the live state, empty, or an intake-generated bundle.

    ``source="current"`` captures the live company into the new slot and marks
    it active — this is how a single-company install enters client mode, and
    it guarantees every later switch has a save-back target. It is refused
    while another client is active (the live state already belongs to that
    slot; use blank + activate instead).

    ``source="blank"`` creates an empty-but-loadable slot to onboard fresh.

    ``source="generated"`` writes a *seed slot* from an engagement-intake
    ``bundle`` (the ``FixtureBundle`` shape from ``/clients/generate``):
    profile + docs land as slot artifacts, and people/departments/memory land
    as the same YAML/JSON seed files curated fixtures use — applied to the
    live DB on first activation. Live state is untouched and the slot is NOT
    activated.
    """
    display_name = (display_name or "").strip()
    if not display_name:
        raise ClientSlotError("display_name is required")
    if source not in ("current", "blank", "generated"):
        raise ClientSlotError("source must be 'current', 'blank', or 'generated'")
    if source == "generated" and not bundle:
        raise ClientSlotError("source='generated' requires a bundle")

    async with _FIXTURE_OP_LOCK:
        _require_no_fixture(settings)

        if slug is not None and not _SAFE_NAME_RE.match(slug):
            raise ClientSlotError("Invalid client slug")
        slug = slug or derive_client_slug(display_name, settings)
        slot = _slot_dir(settings, slug)
        if slot.exists():
            raise ClientSlotConflictError(f"Client {slug!r} already exists")

        if source == "current" and get_active_client(settings) is not None:
            raise ClientSlotConflictError(
                "A client is already active — its live state belongs to that "
                "slot. Create a blank client and activate it instead."
            )

        # Parse + validate a generated bundle BEFORE creating the slot dir so
        # a rejected draft never leaves an empty husk behind.
        parsed_bundle = None
        if source == "generated":
            from openexecutive.fixtures.generator import (
                FixtureBundle,
                validate_bundle,
            )

            try:
                parsed_bundle = FixtureBundle.model_validate(bundle)
            except Exception as exc:
                raise ClientSlotError(f"Invalid bundle: {exc}") from exc
            errors = validate_bundle(parsed_bundle)
            if errors:
                raise ClientSlotError("Invalid bundle: " + "; ".join(errors))

        slot.mkdir(parents=True, exist_ok=True)
        _write_meta(
            slot,
            slug=slug,
            display_name=display_name,
            created_at=datetime.now(UTC).isoformat(),
            saved_at=None,
        )

        if source == "current":
            # Preserve the user's original company in _user_backup before
            # entering client mode, so POST /fixtures/unload remains a working
            # "exit client mode" path (the slot holds the same data, but
            # unload restores from _user_backup specifically).
            if not (
                settings.company_profile_path.parent / "_user_backup" / "profile.yaml"
            ).exists():
                try:
                    snapshot_user_state(settings)
                except Exception:
                    logger.exception(
                        "client-slots: pre-create user snapshot failed"
                    )
            saved = _save_slot_state(settings, slot)
            _active_client_sentinel(settings).write_text(slug)
            _set_honcho_client_workspace(slug)
            return {"slug": slug, "display_name": display_name, "active": True, **saved}

        if source == "generated":
            assert parsed_bundle is not None  # guaranteed by the gate above
            try:
                _write_generated_slot(slot, parsed_bundle, intake_description)
            except Exception as exc:
                # Never leave a half-written husk: the no-husk guarantee must
                # also hold for failures AFTER mkdir (serialization, disk).
                shutil.rmtree(slot, ignore_errors=True)
                raise ClientSlotError(
                    f"Failed to write client slot: {exc}"
                ) from exc
            return {
                "slug": slug,
                "display_name": display_name,
                "active": False,
                "origin": "generated",
                "people": len(parsed_bundle.people),
                "departments": len(parsed_bundle.departments),
                "docs": len(parsed_bundle.docs),
            }

        # Blank: an empty-but-loadable bundle. Live state is untouched.
        from openexecutive.memory.company_profile import CompanyProfile

        CompanyProfile(name=display_name).save_to_yaml(slot / "profile.yaml")
        (slot / "docs").mkdir(exist_ok=True)
        return {"slug": slug, "display_name": display_name, "active": False}


async def save_active_client(settings: Any) -> dict[str, Any]:
    """Checkpoint the live state into the active slot without switching."""
    async with _FIXTURE_OP_LOCK:
        _require_no_fixture(settings)
        active = get_active_client(settings)
        if active is None:
            raise ClientSlotConflictError(
                "No client is active — nothing to save. Create one with "
                "source='current' to capture the live company."
            )
        slot = _require_slot(settings, active)
        saved = _save_slot_state(settings, slot)
        return {"slug": active, "saved": True, **saved}


async def activate_client_slot(
    settings: Any, slug: str, *, app_state: Any | None = None
) -> dict[str, Any]:
    """Switch the live company context to ``slug``, saving the current one back.

    With an active client: save it to its slot, then restore the target. With
    no active client (first activation from single-company mode): the live
    state is the user's original company — auto-snapshot it to ``_user_backup``
    (same restore point the fixture switcher uses; ``POST /fixtures/unload``
    brings it back) before it is replaced.
    """
    async with _FIXTURE_OP_LOCK:
        _require_no_fixture(settings)
        slot = _require_slot(settings, slug)

        active = get_active_client(settings)
        if active == slug:
            return {"slug": slug, "already_active": True}

        if active is not None:
            try:
                previous_slot = _require_slot(settings, active)
            except ClientSlotNotFoundError:
                # Sentinel points at a deleted dir — nothing to save into.
                logger.warning(
                    "client-slots: active sentinel %r has no slot dir; skipping save-back",
                    active,
                )
            else:
                _save_slot_state(settings, previous_slot)
        else:
            # Mirror the fixture switcher's first-load behavior: preserve the
            # user's original company before replacing it. Best-effort — an
            # empty environment has nothing worth snapshotting.
            if not (settings.company_profile_path.parent / "_user_backup" / "profile.yaml").exists():
                try:
                    snapshot_user_state(settings)
                except Exception:
                    logger.exception(
                        "client-slots: pre-activation user snapshot failed"
                    )

        summary = await _restore_slot_state(settings, slot, app_state=app_state)
        _active_client_sentinel(settings).parent.mkdir(parents=True, exist_ok=True)
        _active_client_sentinel(settings).write_text(slug)
        workspace = _set_honcho_client_workspace(slug)
        if workspace:
            summary["honcho_workspace"] = workspace

        # Scheduled rows swap with the client, so the nightly rotation row
        # must be re-seeded into whichever DB just went live (idempotent;
        # no-op unless CLIENT_ROTATION_ENABLED).
        try:
            from openexecutive.clients.rotation import seed_client_rotation

            seed_client_rotation()
        except Exception:
            logger.exception("client-slots: rotation re-seed failed")

        summary["slug"] = slug
        summary["previous"] = active
        return summary


async def delete_client_slot(settings: Any, slug: str) -> dict[str, Any]:
    """Delete a parked slot. Refuses the active one (switch away first)."""
    async with _FIXTURE_OP_LOCK:
        from openexecutive.clients.rotation import rotation_in_progress

        if rotation_in_progress(settings):
            # Mid-rotation, "parked" is a moving target — and deleting the
            # rotation's original client would strand its restore.
            raise ClientSlotConflictError(
                "Overnight rotation is in progress — try again when it "
                "finishes (a minute or two)."
            )
        slot = _require_slot(settings, slug)
        if get_active_client(settings) == slug:
            raise ClientSlotConflictError(
                "This client is currently active — activate another client "
                "(or restore your company via POST /fixtures/unload) before deleting."
            )
        shutil.rmtree(slot)
        return {"deleted": True, "slug": slug}
