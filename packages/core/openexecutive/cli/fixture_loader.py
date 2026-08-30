"""Fixture loading logic for demo / eval company switching.

Callable from both the REST API and a CLI entrypoint. Replaces the full
active company context: profile.yaml, company/docs/, ChromaDB company_docs
collection, and episodic memory rows.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Matches a relative run_at sentinel like "+30s", "+5m", or "+1h" used by
# fixture-staged scheduled actions. Resolved against load time so reloads
# stay "live."
_RELATIVE_RUN_AT_RE = re.compile(r"^([+-])(\d+)([smhd])$")


def _resolve_run_at(value: str, now: datetime) -> str | None:
    """Convert a fixture ``run_at`` value into an ISO timestamp.

    Accepts a relative sentinel resolved against ``now`` — a leading ``+``
    schedules in the future (``+30s``, ``+5m``, ``+1h``, ``+2d``) and a
    leading ``-`` in the past (``-45m``, ``-30h``), which demo data uses to
    stage *overdue* in-flight follow-ups and awaiting replies
    deterministically. An absolute ISO string is passed through unchanged.
    Returns ``None`` for unparseable input so the loader can skip the row.
    """
    if not value:
        return None
    m = _RELATIVE_RUN_AT_RE.match(value.strip())
    if m:
        sign, n, unit = m.group(1), int(m.group(2)), m.group(3)
        delta = {
            "s": timedelta(seconds=n),
            "m": timedelta(minutes=n),
            "h": timedelta(hours=n),
            "d": timedelta(days=n),
        }[unit]
        if sign == "-":
            delta = -delta
        return (now + delta).isoformat()
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return None
    return value

logger = logging.getLogger(__name__)

# Serialise destructive operations — load_fixture, unload_fixture, and
# snapshot_user_state all mutate the same on-disk state and SQLite tables.
# Concurrent calls would race the rmtree/mkdir/reindex sequence and the
# auto-snapshot guard, potentially overwriting the only restore point.
_FIXTURE_OP_LOCK = asyncio.Lock()

# Same allowlist as the /fixtures/{name}/load route — applied when reading the
# sentinel file so a tampered/garbage value cannot reach the UI.
_SAFE_NAME_RE = re.compile(r"^[a-z0-9_-]+$")

# Walk up from this file to find the repo root (contains evals/, fixtures/, etc.)
# Match on ``fixtures/companies`` specifically — NOT a bare ``fixtures`` dir —
# so the in-package ``openexecutive/fixtures/`` module (generated-fixture store
# + generator) does not get mistaken for the repo-root curated-fixtures tree.
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE
while _REPO_ROOT.parent != _REPO_ROOT:
    if (_REPO_ROOT / "fixtures" / "companies").exists() or (_REPO_ROOT / ".git").exists():
        break
    _REPO_ROOT = _REPO_ROOT.parent

FIXTURES_ROOT = _REPO_ROOT / "fixtures" / "companies"


class FixtureNotFoundError(ValueError):
    pass


def get_fixtures_root() -> Path:
    return FIXTURES_ROOT


def list_fixtures() -> list[dict[str, Any]]:
    if not FIXTURES_ROOT.exists():
        return []

    results: list[dict[str, Any]] = []
    for fixture_dir in sorted(FIXTURES_ROOT.iterdir()):
        profile_path = fixture_dir / "profile.yaml"
        if not fixture_dir.is_dir() or not profile_path.exists():
            continue

        from openexecutive.memory.company_profile import CompanyProfile

        try:
            profile = CompanyProfile.load_from_yaml(profile_path)
        except Exception:
            continue

        docs_dir = fixture_dir / "docs"
        doc_count = len(list(docs_dir.glob("*.md"))) if docs_dir.exists() else 0

        scenarios_dir = fixture_dir / "scenarios"
        scenario_count = len(list(scenarios_dir.glob("*.yaml"))) if scenarios_dir.exists() else 0

        # Compact department + people summaries so the demo cards can show the
        # org shape without a second round-trip. Parsed defensively — a missing
        # or malformed file just yields an empty list rather than dropping the
        # whole fixture.
        departments = _summarize_departments(fixture_dir / "departments.yaml")
        people = _summarize_people(fixture_dir / "people.yaml")

        results.append(
            {
                "name": fixture_dir.name,
                "display_name": profile.name,
                "industry": profile.industry,
                "stage": profile.stage,
                "arr": profile.annual_revenue_arr,
                "headcount": profile.headcount,
                "founding_year": profile.founding_year,
                "mission": profile.mission,
                "doc_count": doc_count,
                "scenario_count": scenario_count,
                "departments": departments,
                "people": people,
            }
        )
    return results


def _summarize_departments(path: Path) -> list[dict[str, Any]]:
    """Compact ``[{title, head}]`` list for a fixture's departments.yaml."""
    if not path.exists():
        return []
    import yaml

    try:
        data = yaml.safe_load(path.read_text()) or {}
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for dept in data.get("departments", []) or []:
        title = dept.get("title") or dept.get("slug")
        if not title:
            continue
        out.append({"title": title, "head": dept.get("head_person_name")})
    return out


def _summarize_people(path: Path) -> list[dict[str, Any]]:
    """Compact ``[{name, role, is_principal}]`` list for a fixture's people.yaml."""
    if not path.exists():
        return []
    import yaml

    try:
        data = yaml.safe_load(path.read_text()) or {}
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for person in data.get("people", []) or []:
        name = person.get("full_name")
        if not name:
            continue
        out.append(
            {
                "name": name,
                "role": person.get("role") or "",
                "is_principal": bool(person.get("is_principal")),
            }
        )
    return out


def _user_backup_dir(settings: Any) -> Path:
    """Where the user's original (pre-fixture) company state is stashed."""
    return settings.company_profile_path.parent / "_user_backup"


def _fixture_active_sentinel(settings: Any) -> Path:
    return _user_backup_dir(settings) / ".fixture_active"


def reconcile_honcho_workspace_on_startup(settings: Any) -> None:
    """Clear a stale per-fixture Honcho override on app startup.

    If a previous process crashed between ``load_fixture`` and
    ``unload_fixture``, the active-workspace state file persists and
    the next boot would route Honcho traffic to the demo workspace
    forever. We reconcile by checking: if the override exists but the
    fixture-active sentinel doesn't, the previous load is orphaned —
    clear the override so the env-default workspace takes over again.
    The orphaned demo workspace lingers in Honcho until a manual
    ``/fixtures/reset`` (or it ages out via Honcho's own retention).

    Best-effort — failures are logged but never block startup.
    """
    try:
        from openexecutive.clients.slots import get_active_client
        from openexecutive.memory.honcho_client import (
            _active_workspace_state_path,
            clear_active_workspace_id,
        )
        state_path = _active_workspace_state_path()
        sentinel = _fixture_active_sentinel(settings)
        # An active client slot legitimately holds a (stable, durable)
        # workspace override across restarts — never treat it as orphaned.
        if (
            state_path.exists()
            and not sentinel.exists()
            and get_active_client(settings) is None
        ):
            logger.warning(
                "honcho: clearing stale per-fixture workspace override "
                "(no .fixture_active sentinel) — likely from a crashed previous load"
            )
            clear_active_workspace_id()
    except Exception:
        logger.exception("honcho: workspace reconcile on startup failed")


async def load_fixture(fixture_name: str, settings: Any) -> dict[str, Any]:
    """Replace the active company context with the named fixture.

    Before the FIRST fixture load on an environment, automatically snapshots
    the current company state to ``_user_backup/`` so it can be restored via
    ``unload_fixture()``. Subsequent fixture-to-fixture switches do NOT
    overwrite the backup (the live state at that point is a previous fixture,
    not the user's company).

    Returns a summary dict with counts of what was loaded.
    """
    fixture_dir = FIXTURES_ROOT / fixture_name
    if not fixture_dir.exists() or not fixture_dir.is_dir():
        raise FixtureNotFoundError(f"Fixture {fixture_name!r} not found in {FIXTURES_ROOT}")

    profile_path = fixture_dir / "profile.yaml"
    if not profile_path.exists():
        raise FixtureNotFoundError(f"Fixture {fixture_name!r} has no profile.yaml")

    return await _load_from_dir(fixture_name, fixture_dir, settings)


async def _load_from_dir(
    fixture_name: str, fixture_dir: Path, settings: Any
) -> dict[str, Any]:
    """Apply a fixture from an on-disk directory and record it as active.

    Shared by ``load_fixture`` (curated fixtures, source = repo ``fixtures/``)
    and ``load_fixture_any`` (generated fixtures, source = a temp dir
    materialized from the DB). Holds the destructive-op lock, auto-snapshots
    the user's state on the first ever load, swaps the Honcho workspace, and
    writes the active-fixture sentinel — all behavior is identical regardless
    of where ``fixture_dir`` came from.
    """
    async with _FIXTURE_OP_LOCK:
        # If a client slot is active, the live state belongs to that client —
        # save it back to its slot and leave client mode before the fixture
        # replaces everything. Without this, loading a demo would silently
        # destroy the active client's unsaved work.
        try:
            from openexecutive.clients.slots import park_active_client

            park_active_client(settings)
        except Exception:
            logger.exception("fixture load: client save-back failed (continuing)")

        # Auto-snapshot user state on first ever fixture load. Use the same
        # condition as `has_snapshot` so a partial/failed snapshot directory
        # (mkdir succeeded but profile.yaml never landed) does not permanently
        # disable auto-snapshot. The sentinel guards the inverse: if a previous
        # fixture is already active, current state is NOT the user's company.
        backup_dir = _user_backup_dir(settings)
        sentinel = _fixture_active_sentinel(settings)
        auto_snapshot_taken = False
        if not (backup_dir / "profile.yaml").exists() and not sentinel.exists():
            try:
                snapshot_user_state(settings)
                auto_snapshot_taken = True
            except Exception:
                # Best-effort: do not block the fixture load if snapshot fails
                # on a truly empty environment. User can call snapshot manually.
                pass

        summary = await _apply_state_from_source(fixture_dir, settings)

        # Record which fixture is active so the UI can show it and so a
        # subsequent load() knows not to take a fresh snapshot.
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text(fixture_name)

        # Honcho workspace isolation: switch to a per-fixture workspace
        # so demo turns sync to a sacrificial store rather than polluting
        # the operator's real peer cards. Best-effort — the fixture must
        # still load if Honcho can't be reached.
        try:
            # If a previous fixture's workspace is still active (sequential
            # load without unload), tear it down first so it doesn't
            # become an orphan. set_active_workspace_id below overwrites
            # the state file in place, so no explicit clear is needed.
            # Client-slot workspaces are durable peer memory, not demo
            # sacrifices — never tear those down here.
            from openexecutive.clients.slots import CLIENT_WORKSPACE_PREFIX
            from openexecutive.memory.honcho_client import (
                delete_workspace_and_reset_client,
                get_active_workspace_id,
                set_active_workspace_id,
            )

            current_ws = get_active_workspace_id()
            default_ws = settings.honcho_workspace_id
            if (
                current_ws
                and current_ws != default_ws
                and not current_ws.startswith(CLIENT_WORKSPACE_PREFIX)
            ):
                await delete_workspace_and_reset_client(current_ws)

            demo_ws = f"openexec-fixture-{fixture_name}-{uuid.uuid4().hex[:8]}"
            set_active_workspace_id(demo_ws, fixture_name=fixture_name)
            summary["honcho_workspace"] = demo_ws
        except Exception:
            logger.exception(
                "load_fixture: honcho workspace switch failed; demo will share "
                "the operator's default workspace (cleanup may need a manual "
                "/fixtures/reset)"
            )

        summary["fixture"] = fixture_name
        summary["auto_snapshot_taken"] = auto_snapshot_taken
        return summary


async def load_fixture_any(fixture_name: str, settings: Any) -> dict[str, Any]:
    """Load a curated (disk) OR generated (DB) fixture by name.

    Curated fixtures resolve to their repo ``fixtures/companies/<name>/`` dir.
    Generated fixtures are materialized from the ``generated_fixtures`` table
    into a temp directory and loaded through the identical ``_load_from_dir``
    path, so they get the same auto-snapshot / sentinel / Honcho behavior.
    """
    fixture_dir = FIXTURES_ROOT / fixture_name
    if fixture_dir.exists() and (fixture_dir / "profile.yaml").exists():
        return await _load_from_dir(fixture_name, fixture_dir, settings)

    # Not a curated fixture — try the generated-fixtures table.
    from openexecutive.fixtures import generator as fixtures_generator
    from openexecutive.fixtures import store as fixtures_store

    record = fixtures_store.get_fixture(fixture_name)
    if record is None:
        raise FixtureNotFoundError(
            f"Fixture {fixture_name!r} not found (no curated dir and no generated row)"
        )

    import tempfile

    with tempfile.TemporaryDirectory(prefix="oe-fixture-") as tmp:
        tmp_dir = Path(tmp)
        fixtures_generator.materialize_to_dir(record, tmp_dir)
        return await _load_from_dir(fixture_name, tmp_dir, settings)


def list_all_fixtures() -> list[dict[str, Any]]:
    """Curated (disk) + generated (DB) fixtures, each tagged with ``source``.

    Curated rows are tagged ``source="curated"``; generated rows come from the
    store already tagged ``source="generated"``. Generated fixtures sort first
    (most-recently-updated) so freshly created ones surface at the top.
    """
    curated = [{**row, "source": "curated"} for row in list_fixtures()]
    try:
        from openexecutive.fixtures import store as fixtures_store

        generated = fixtures_store.list_fixtures()
    except Exception:
        # A missing/locked generated_fixtures table must never take down the
        # curated list — degrade to curated-only.
        logger.exception("list_all_fixtures: failed to read generated fixtures")
        generated = []
    return generated + curated


async def _apply_state_from_source(source_dir: Path, settings: Any) -> dict[str, Any]:
    """Apply profile.yaml + docs/ + memory.json + people.yaml + departments.yaml
    from ``source_dir`` to the live company state.

    Shared by ``load_fixture()`` (source = fixture dir) and ``unload_fixture()``
    (source = ``_user_backup/`` dir). Wipes the company-side state before
    writing, so the source dir is fully authoritative.
    """
    from openexecutive.knowledge.loader import ingest_file
    from openexecutive.knowledge.store import ChromaDBStore
    from openexecutive.memory.company_profile import CompanyProfile

    profile_path = source_dir / "profile.yaml"
    profile = (
        CompanyProfile.load_from_yaml(profile_path)
        if profile_path.exists()
        else CompanyProfile()
    )

    # ── 1. Write profile.yaml (empty profile is fine — produces empty company)
    profile.save_to_yaml(settings.company_profile_path)

    # ── 2. Replace company/docs/ ───────────────────────────────────────────
    company_docs_dir: Path = settings.company_profile_path.parent / "docs"
    if company_docs_dir.exists():
        shutil.rmtree(company_docs_dir)
    company_docs_dir.mkdir(parents=True, exist_ok=True)

    src_docs_dir = source_dir / "docs"
    if src_docs_dir.exists():
        for src in src_docs_dir.glob("*.md"):
            shutil.copy2(src, company_docs_dir / src.name)

    # ── 3. Clear + re-index ChromaDB company collection ───────────────────
    store = ChromaDBStore(persist_directory=settings.vector_store_path)
    store.delete_company_docs()
    # Recent-research artifacts are per-company; never let a new company
    # inherit the prior company's research.
    store.delete_documents(
        collection=ChromaDBStore.RESEARCH_COLLECTION,
        where={"type": "recent_research"},
    )
    store.delete_notion_docs()
    from openexecutive.knowledge.notion_sync import reset_local_state

    reset_local_state(profile_path=settings.company_profile_path)

    docs_indexed = 0
    for dest_doc in sorted(company_docs_dir.glob("*.md")):
        docs_indexed += await ingest_file(
            path=dest_doc,
            store=store,
            collection=ChromaDBStore.COMPANY_COLLECTION,
        )

    # ── 4. Seed people first so episodic seeding can resolve
    #       assigned_to_person on scheduled_actions, and departments seeding
    #       can resolve head_person_id by name.
    people_seeded = _seed_people(source_dir / "people.yaml")
    memory_seeded = _seed_episodic_memory(source_dir / "memory.json", settings)
    departments_seeded = _seed_departments(source_dir / "departments.yaml")

    # ── 5. Wipe the external-monitoring layer ──────────────────────────────
    # The watchlist + external_signals tables are NOT part of memory.json or
    # the snapshot round-trip — they're regenerated by the executive_research
    # scan and the external monitor. Without clearing them here the previous
    # company's monitors (or a prior research scan's AI/competitor entries)
    # leak into every subsequent fixture load AND into the restored user
    # state on unload. Delete external_signals BEFORE watchlist to respect
    # the FK (external_signals.watchlist_id → watchlist.id, enforced under
    # PRAGMA foreign_keys=ON in _delete_all_rows). Ensure the monitoring
    # schema exists first — load/unload can run against a DB that only has
    # the episodic schema (tests, scripts), and a missing table would raise
    # mid-wipe. initialize_db is idempotent (CREATE TABLE IF NOT EXISTS).
    from openexecutive.memory.episodic import DB_PATH as EPISODIC_DB_PATH
    from openexecutive.monitoring import store as monitoring_store

    # Guarded on the DB already existing so we never materialise a DB the
    # caller never created (which would defeat _delete_all_rows' exists()
    # short circuit). Idempotent CREATE TABLE IF NOT EXISTS otherwise.
    if EPISODIC_DB_PATH.exists():
        monitoring_store.initialize_db(EPISODIC_DB_PATH)
    monitoring_cleared = _delete_all_rows(
        EPISODIC_DB_PATH, ("external_signals", "watchlist")
    )

    # ── 6. Reset the periodic research skip-if-unchanged gate ───────────────
    # The skip gate reads the last run's state_hash from audit_log; load/unload
    # wipes the profile/initiatives/watchlist that feed that fingerprint but
    # (by design) preserves audit_log. Stale research run-history rows would
    # otherwise make the freshly-seeded watchlist_research_scan skip itself —
    # the "no findings on a seeded run" failure. Surgical delete of just the
    # three research event types; the rest of /audit history is untouched.
    research_history_cleared = 0
    try:
        from openexecutive.monitoring.research.scheduler import (
            clear_research_run_history,
        )

        research_history_cleared = clear_research_run_history(EPISODIC_DB_PATH)
    except Exception:
        logger.exception(
            "fixture: clear_research_run_history failed (non-fatal)"
        )

    return {
        "display_name": profile.name,
        "profile": profile.model_dump(),
        "docs_indexed": docs_indexed,
        "memory_seeded": memory_seeded,
        "people_seeded": people_seeded,
        "departments_seeded": departments_seeded,
        "monitoring_cleared": monitoring_cleared,
        "research_history_cleared": research_history_cleared,
    }


async def unload_fixture(settings: Any) -> dict[str, Any]:
    """Restore the user's original state from ``_user_backup/``.

    Raises ``FixtureNotFoundError`` if no snapshot has been taken yet — there
    is no original state to restore to.
    """
    async with _FIXTURE_OP_LOCK:
        backup = _user_backup_dir(settings)
        if not (backup / "profile.yaml").exists():
            raise FixtureNotFoundError(
                "No snapshot exists yet. Load a fixture first (auto-snapshots), "
                "or click 'Snapshot current as my company' to capture state."
            )

        # Unload is also the "exit client mode" path: if a client slot is
        # active (no fixture in play), save its live state back to the slot
        # before the backup restore replaces it.
        try:
            from openexecutive.clients.slots import park_active_client

            summary_client = park_active_client(settings)
        except Exception:
            logger.exception("unload: client save-back failed (continuing)")
            summary_client = None

        summary = await _apply_state_from_source(backup, settings)
        if summary_client is not None:
            summary["client_saved"] = summary_client

        # Clear the active-fixture sentinel — current state is the user's own.
        sentinel = _fixture_active_sentinel(settings)
        sentinel.unlink(missing_ok=True)

        # Honcho cleanup: delete the per-fixture workspace and clear the
        # override so the operator's env-default workspace takes over
        # again. Best-effort — the local restore must succeed regardless.
        try:
            from openexecutive.clients.slots import CLIENT_WORKSPACE_PREFIX
            from openexecutive.memory.honcho_client import (
                clear_active_workspace_id,
                delete_workspace_and_reset_client,
                get_active_workspace_id,
            )

            demo_ws = get_active_workspace_id()
            default_ws = settings.honcho_workspace_id
            # Clear the override BEFORE attempting the delete. If the
            # delete fails (network, ConflictError), an override file
            # still pointing at a half-deleted workspace would make the
            # next ``_get_client()`` build against a dead workspace and
            # emit a NotFoundError on every Honcho call until restart.
            clear_active_workspace_id()
            if (
                demo_ws
                and demo_ws != default_ws
                # Client-slot workspaces are durable peer memory — exiting
                # client mode parks them, never deletes them.
                and not demo_ws.startswith(CLIENT_WORKSPACE_PREFIX)
            ):
                await delete_workspace_and_reset_client(demo_ws)
                summary["honcho_workspace_deleted"] = demo_ws
        except Exception:
            logger.exception(
                "unload_fixture: honcho workspace cleanup failed; the "
                "operator's env-default workspace is back in use, but a "
                "demo workspace may remain in Honcho"
            )

        summary["restored_from_backup"] = True
        return summary


async def reset_all_state(
    settings: Any, *, app_state: Any | None = None
) -> dict[str, Any]:
    """Wipe live state AND the snapshot — return to factory-default.

    Intentionally destructive. Unlike unload, there is no path back from
    this. Re-seeds the 8 default specialist departments after wiping so the
    user sees a sensible starting org instead of a blank departments page.

    ``app_state`` (optional) is FastAPI's ``request.app.state``; when
    provided, the shared ``store`` attribute is swapped *inside* the lock
    so concurrent requests cannot read the old store object after its
    backing collection has been deleted.

    Steps (under the shared destructive-op lock):
      1. Empty live profile.yaml
      2. Delete + recreate company/docs/, clear ChromaDB company collection
      3. Wipe episodic rows including chat history (chat_messages first,
         then sessions, then decisions/initiatives/advice_given/
         scheduled_actions/voice_personas) AND run/audit history
         (workflow_runs, audit_log, eval_runs) so /jobs and /audit reset
         too
      4. DELETE people + child tables (authority scope, availability)
      5. DELETE departments + Goals, then re-seed 8 default departments
      5a. Re-bootstrap principal briefs, department cadences, and (if
          enabled) the nudge-scan heartbeat — without this Today stays
          blank until the next API restart
      6. Remove the _user_backup/ directory entirely (sentinel goes with it)
      7. Swap app.state.store inside the lock so concurrent reads cannot
         hit the deleted-then-recreated collection mid-flight
    """
    from openexecutive.knowledge.store import ChromaDBStore
    from openexecutive.memory.company_profile import CompanyProfile

    async with _FIXTURE_OP_LOCK:
        # 1. Live profile → empty
        CompanyProfile().save_to_yaml(settings.company_profile_path)

        # 2. Live docs + ChromaDB
        company_docs_dir: Path = settings.company_profile_path.parent / "docs"
        if company_docs_dir.exists():
            shutil.rmtree(company_docs_dir)
        company_docs_dir.mkdir(parents=True, exist_ok=True)

        store = ChromaDBStore(persist_directory=settings.vector_store_path)
        store.delete_company_docs()
        store.delete_documents(
            collection=ChromaDBStore.RESEARCH_COLLECTION,
            where={"type": "recent_research"},
        )
        store.delete_notion_docs()
        from openexecutive.knowledge.notion_sync import reset_local_state

        reset_local_state(profile_path=settings.company_profile_path)

        # 2b. Company-authored skills — delete the filesystem directory and
        # the company-source rows from the shared `skills` ChromaDB
        # collection. Built-in skills (source='builtin') are preserved so
        # the box still has its default skill library after a reset.
        from openexecutive.knowledge.skills_index import SKILLS_COLLECTION
        company_skills_dir: Path = settings.company_profile_path.parent / "skills"
        if company_skills_dir.exists():
            shutil.rmtree(company_skills_dir)
        store.delete_documents(
            collection=SKILLS_COLLECTION,
            where={"source": "company"},
        )

        # 3. Episodic rows — includes chat history, voice personas, alerts
        # state (alerts, mutes, preferences), AND the run/audit
        # history (workflow_runs, audit_log, eval_runs) so a reset truly
        # returns the box to factory state. Without the run/audit wipe the
        # /jobs Runs tab and /audit view kept showing the previous fixture's
        # executions and made reset look broken.
        # Order matters under PRAGMA foreign_keys=ON: children before parents
        # (chat_messages → sessions; external_signals → watchlist). The three
        # run/audit tables have no
        # FK relationships into the others, so their position is safe; the
        # external-monitoring tables (watchlist + external_signals) also live
        # in this DB and must be wiped so a reset starts with a clean
        # watchlist instead of the previous company's monitors.
        from openexecutive.memory.episodic import DB_PATH as EPISODIC_DB_PATH
        from openexecutive.monitoring import store as monitoring_store

        # Ensure the monitoring schema exists before the DELETE pass — in
        # production the lifespan inits it, but reset is also called directly
        # (tests, scripts) against a DB that only has the episodic schema, and
        # a missing table would raise mid-wipe. Idempotent. Guarded on the DB
        # already existing so we never *materialise* a DB the caller never
        # created — that would defeat _delete_all_rows' own exists() short
        # circuit (and there's nothing to wipe in a DB that isn't there).
        if EPISODIC_DB_PATH.exists():
            monitoring_store.initialize_db(EPISODIC_DB_PATH)
        episodic_cleared = _delete_all_rows(
            EPISODIC_DB_PATH,
            (
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
            ),
        )

        # 4. People (child tables first to satisfy FK ordering)
        from openexecutive.people import store as people_store
        people_cleared = _delete_all_rows(
            people_store.DB_PATH,
            ("person_authority_scope", "person_availability", "people"),
        )

        # 5. Departments + Goals, then re-seed defaults. ``departments_meta``
        # carries the "default-departments-already-seeded" sentinel; without
        # wiping it here, ``seed_default_departments`` short-circuits to a
        # no-op on any reset after the first ever (the sentinel is set at
        # API boot via ``api/main.py``), and the box ends up with zero
        # departments — which then nukes the cadence bootstrap downstream.
        from openexecutive.departments import store as dept_store
        _delete_all_rows(
            dept_store.DB_PATH,
            ("department_goals", "departments", "departments_meta"),
        )
        departments_seeded = dept_store.seed_default_departments()

        # 5a. Re-bootstrap the built-in scheduled actions that
        # ``api/main.py`` enqueues at process startup. The episodic wipe
        # above just deleted them, but the bootstrap functions only run on
        # API boot — without re-calling them here, the box stays "blank
        # slate" until the next deploy/restart. That's what makes Today
        # look stuck for the principal: no morning brief, no EoD digest,
        # no reflection, and no department check-ins.
        #
        # All three calls are idempotent (each checks for a pending/running
        # row before inserting), so calling them again on a freshly-empty
        # table simply enqueues one row apiece.
        from openexecutive.config import get_settings
        from openexecutive.departments.cadence import bootstrap_cadences
        from openexecutive.scheduler.runner import seed_principal_briefs

        try:
            seed_principal_briefs()
        except Exception:
            logger.exception("reset: seed_principal_briefs failed")
        try:
            bootstrap_cadences()
        except Exception:
            logger.exception("reset: bootstrap_cadences failed")
        if get_settings().nudge_scan_enabled:
            try:
                from openexecutive.scheduler.nudge_engine import bootstrap_nudge_scan
                bootstrap_nudge_scan()
            except Exception:
                logger.exception("reset: bootstrap_nudge_scan failed")
        if get_settings().external_monitor_enabled:
            try:
                from openexecutive.monitoring.pipeline import (
                    bootstrap_external_monitor_scan,
                )
                bootstrap_external_monitor_scan()
            except Exception:
                logger.exception("reset: bootstrap_external_monitor_scan failed")
        if get_settings().watchlist_research_enabled:
            try:
                from openexecutive.monitoring.research.scheduler import (
                    bootstrap_watchlist_research_scan,
                )
                bootstrap_watchlist_research_scan()
            except Exception:
                logger.exception(
                    "reset: bootstrap_watchlist_research_scan failed"
                )

        # 5b. External per-person memory (Honcho) — best-effort. The
        # wrapper swallows its own errors and audits them; the second
        # try/except here catches anything outside that (e.g. ImportError
        # from a missing honcho-ai package on a non-Honcho build) so a
        # Honcho problem can never block the local reset from finishing.
        #
        # Reset means reset everywhere: delete the active workspace, and
        # if a per-fixture override is active, ALSO delete the env-default
        # workspace so the operator's real Honcho data is wiped too.
        try:
            from openexecutive.clients.slots import CLIENT_WORKSPACE_PREFIX
            from openexecutive.memory.honcho_client import (
                clear_active_workspace_id,
                delete_workspace_and_reset_client,
                get_active_workspace_id,
            )

            active_ws = get_active_workspace_id()
            default_ws = settings.honcho_workspace_id
            # Client-slot workspaces are durable peer memory tied to slot
            # dirs, which reset preserves — skip deleting those.
            if not active_ws.startswith(CLIENT_WORKSPACE_PREFIX):
                await delete_workspace_and_reset_client(active_ws)
            # Clear the override BEFORE the second delete. The first
            # call dropped the client cache; without clearing the file
            # here, the next ``_get_client()`` would rebuild against the
            # just-deleted demo workspace and the ``default_ws`` delete
            # would emit a NotFoundError instead of doing real work.
            clear_active_workspace_id()
            if active_ws != default_ws:
                await delete_workspace_and_reset_client(default_ws)
        except Exception:
            logger.exception(
                "reset: honcho workspace delete failed; local reset continuing"
            )

        # 6. Snapshot directory (sentinel lives inside it)
        backup_dir = _user_backup_dir(settings)
        if backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)

        # 6a. Exit client mode, but preserve the slot dirs — they are explicit,
        # named save files; deleting a client is its own deliberate action
        # (DELETE /clients/{slug}), not a side effect of reset.
        try:
            from openexecutive.clients.slots import _active_client_sentinel

            _active_client_sentinel(settings).unlink(missing_ok=True)
        except Exception:
            logger.exception("reset: clearing active-client sentinel failed")

        # 7. Swap the shared store inside the lock to close the race where a
        # concurrent reader could otherwise see the deleted/recreated
        # collection through the previous store instance.
        if app_state is not None and hasattr(app_state, "store"):
            app_state.store = ChromaDBStore(
                persist_directory=settings.vector_store_path
            )

        return {
            "reset": True,
            "departments_seeded": departments_seeded,
            "episodic_cleared": episodic_cleared,
            "people_cleared": people_cleared,
        }


def _delete_all_rows(
    db_path: Path, tables: tuple[str, ...]
) -> dict[str, int]:
    """DELETE FROM each table; return per-table row count deleted.

    Skips silently if the DB file doesn't exist yet (fresh install path).
    Tables must be listed in FK-respecting order (children before parents)
    — PRAGMA foreign_keys is enabled here to match how the rest of the app
    opens this database, so violations will raise rather than silently
    leave orphan rows.
    """
    if not db_path.exists():
        return dict.fromkeys(tables, 0)
    counts: dict[str, int] = {}
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        for table in tables:
            cursor = conn.execute(f"DELETE FROM {table}")
            counts[table] = int(cursor.rowcount)
        conn.commit()
    finally:
        conn.close()
    return counts


def get_fixture_status(settings: Any) -> dict[str, Any]:
    """Lightweight status for the UI: which fixture is active and is there a snapshot?"""
    backup_dir = _user_backup_dir(settings)
    sentinel = _fixture_active_sentinel(settings)

    active_fixture: str | None = None
    if sentinel.exists():
        try:
            content = sentinel.read_text().strip()
            # Defence-in-depth: only return the value if it matches the same
            # allowlist used by /fixtures/{name}/load. Anything else (garbage,
            # hand-edit, tampering) is treated as "no fixture active".
            if content and _SAFE_NAME_RE.match(content):
                active_fixture = content
        except Exception:
            active_fixture = None

    has_snapshot = (backup_dir / "profile.yaml").exists()
    return {"active_fixture": active_fixture, "has_snapshot": has_snapshot}


class FixtureActiveError(RuntimeError):
    """Raised when a destructive op would overwrite the only backup."""


async def snapshot_user_state_async(
    settings: Any, *, force: bool = False
) -> dict[str, Any]:
    """Async, lock-protected entry point for snapshot.

    Use this from any concurrent caller (HTTP route, scheduler). The sync
    ``snapshot_user_state`` below remains the inner implementation and is
    safe to call directly from inside ``_FIXTURE_OP_LOCK`` (as ``load_fixture``
    does for its auto-snapshot pass) — but external callers must go through
    here so the lock serialises against concurrent load / unload.
    """
    async with _FIXTURE_OP_LOCK:
        return snapshot_user_state(settings, force=force)


def snapshot_user_state(settings: Any, *, force: bool = False) -> dict[str, Any]:
    """Write the live company state to ``_user_backup/`` so unload can restore it.

    Refuses by default when a fixture is currently active — calling snapshot
    while a fixture is loaded would overwrite the only copy of the user's
    real company with fixture data, silently destroying it. Callers that
    really mean it can pass ``force=True``.

    Idempotent when allowed. Clears the ``.fixture_active`` sentinel after
    writing so the current state becomes "the user's company" by declaration.
    Safe to call on empty environments — missing sources produce empty
    snapshot artifacts that round-trip cleanly through restore.
    """
    sentinel = _fixture_active_sentinel(settings)
    if sentinel.exists() and not force:
        raise FixtureActiveError(
            "Cannot snapshot while a fixture is active — current state is "
            "fixture data, not your company. Unload first, then snapshot."
        )

    backup_dir = _user_backup_dir(settings)
    backup_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. profile.yaml ────────────────────────────────────────────────────
    src_profile = settings.company_profile_path
    dst_profile = backup_dir / "profile.yaml"
    if src_profile.exists():
        shutil.copy2(src_profile, dst_profile)
    else:
        # Write empty profile so restore can detect "no real profile to apply".
        from openexecutive.memory.company_profile import CompanyProfile

        CompanyProfile().save_to_yaml(dst_profile)

    # ── 2. docs/ ───────────────────────────────────────────────────────────
    src_docs = src_profile.parent / "docs"
    dst_docs = backup_dir / "docs"
    if dst_docs.exists():
        shutil.rmtree(dst_docs)
    dst_docs.mkdir(parents=True, exist_ok=True)
    docs_count = 0
    if src_docs.exists():
        for f in src_docs.glob("*.md"):
            shutil.copy2(f, dst_docs / f.name)
            docs_count += 1

    # ── 3. memory.json (episodic) ─────────────────────────────────────────
    memory_count = _dump_episodic_memory(backup_dir / "memory.json")

    # ── 4. people.yaml ─────────────────────────────────────────────────────
    people_count = _dump_people(backup_dir / "people.yaml")

    # ── 5. departments.yaml ───────────────────────────────────────────────
    departments_count = _dump_departments(backup_dir / "departments.yaml")

    # Clear active-fixture marker — this state IS the user's company now.
    _fixture_active_sentinel(settings).unlink(missing_ok=True)

    return {
        "snapshot_taken": True,
        "docs_snapshotted": docs_count,
        "memory_snapshotted": memory_count,
        "people_snapshotted": people_count,
        "departments_snapshotted": departments_count,
    }


def _dump_episodic_memory(memory_path: Path) -> dict[str, int]:
    """Dump decisions / initiatives / advice rows to memory.json.

    Format mirrors fixture memory.json exactly so ``_seed_episodic_memory``
    round-trips cleanly. ``scheduled_actions`` is dumped as an empty list:
    the seeder DOES re-populate that table from fixtures (relative
    ``run_at`` sentinels and all), but user-snapshot round-tripping would
    capture stale in-flight actions, so we deliberately drop them here.
    """
    from openexecutive.memory.episodic import (
        list_advice,
        list_decisions,
        list_initiatives,
    )

    decisions = [d.model_dump(exclude={"id"}) for d in list_decisions()]
    initiatives = [i.model_dump(exclude={"id"}) for i in list_initiatives()]
    advice = [a.model_dump(exclude={"id"}) for a in list_advice()]

    payload = {
        "decisions": decisions,
        "initiatives": initiatives,
        "advice_given": advice,
        "scheduled_actions": [],
    }
    memory_path.write_text(json.dumps(payload, indent=2, default=str))
    return {
        "decisions": len(decisions),
        "initiatives": len(initiatives),
        "advice_given": len(advice),
    }


def _dump_people(people_path: Path) -> int:
    """Dump the people table to people.yaml in the format ``_seed_people`` expects."""
    from openexecutive.people import store as people_store

    people = people_store.list_people()
    rows = []
    for p in people:
        row: dict[str, Any] = {
            "full_name": p.full_name,
            "role": p.role,
            "is_principal": p.is_principal,
            "department_slugs": list(p.department_slugs),
            "preferred_channel": p.preferred_channel,
            "response_sla_hours": p.response_sla_hours,
            "authority_scope": [s.value for s in p.authority_scope],
        }
        if p.email:
            row["email"] = p.email
        if p.slack_user_id:
            row["slack_user_id"] = p.slack_user_id
        if p.telegram_chat_id:
            row["telegram_chat_id"] = p.telegram_chat_id
        if p.discord_user_id:
            row["discord_user_id"] = p.discord_user_id
        if p.availability:
            row["availability"] = [
                {
                    "weekdays": list(w.weekdays),
                    "start_local": w.start_local,
                    "end_local": w.end_local,
                    "timezone": w.timezone,
                }
                for w in p.availability
            ]
        rows.append(row)

    import yaml

    people_path.write_text(yaml.safe_dump({"people": rows}, sort_keys=False))
    return len(rows)


def _dump_departments(dept_path: Path) -> int:
    """Dump departments + Goals to departments.yaml in ``_seed_departments`` format."""
    from openexecutive.departments import store as dept_store
    from openexecutive.people import store as people_store

    # Build person_id -> full_name lookup so head_person_id can be re-expressed
    # as head_person_name (the format the seeder expects).
    id_to_name = {p.id: p.full_name for p in people_store.list_people() if p.id is not None}

    states = dept_store.list_departments()
    rows = []
    for s in states:
        c = s.config
        row: dict[str, Any] = {
            "slug": c.slug,
            "title": c.title,
            "specialist_key": c.specialist_key,
            "authority_level": c.authority_level.value,
            "charter": {
                "mission": c.charter.mission,
                "scope": list(c.charter.scope),
                "out_of_scope": list(c.charter.out_of_scope),
            },
            "cadences": dict(c.cadences),
        }
        head_name = id_to_name.get(c.head_person_id) if c.head_person_id else None
        if head_name:
            row["head_person_name"] = head_name
        if s.headcount is not None:
            row["headcount"] = s.headcount
        if s.budget_usd is not None:
            row["budget_usd"] = s.budget_usd
        if s.goals:
            row["goals"] = [
                {
                    "period_type": g.period_type,
                    "period_value": g.period_value,
                    "key_result": g.key_result,
                    "target": g.target,
                    "current": g.current,
                    "status": g.status,
                }
                for g in s.goals
            ]
        rows.append(row)

    import yaml

    dept_path.write_text(yaml.safe_dump({"departments": rows}, sort_keys=False))
    return len(rows)


def _seed_episodic_memory(memory_path: Path, settings: Any) -> dict[str, int]:
    """Clear episodic tables and insert seed rows from memory.json."""
    _empty = {
        "decisions": 0,
        "initiatives": 0,
        "advice_given": 0,
        "scheduled_actions": 0,
        "alerts": 0,
    }
    if not memory_path.exists():
        return dict(_empty)

    try:
        data: dict[str, Any] = json.loads(memory_path.read_text())
    except Exception:
        return dict(_empty)

    # Use the module-level DB_PATH as authoritative default. Avoid any string
    # comparison against relative paths — Path normalises "./" away, which made
    # the old `str(db_path) == "./episodic_memory.db"` check always False.
    from openexecutive.memory.episodic import DB_PATH as _DEFAULT_DB_PATH

    attr = getattr(settings, "episodic_db_path", None)
    db_path = Path(str(attr)) if attr else _DEFAULT_DB_PATH
    if not db_path.exists():
        db_path = _DEFAULT_DB_PATH
    if not db_path.exists():
        return dict(_empty)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    counts: dict[str, int] = {}
    try:
        conn.execute("DELETE FROM decisions")
        conn.execute("DELETE FROM initiatives")
        conn.execute("DELETE FROM advice_given")
        conn.execute("DELETE FROM scheduled_actions")
        # Alerts live in the same episodic DB; wipe so reloads are idempotent.
        # Guarded on table existence: a partial/legacy DB (or a minimal test
        # DB) may not have the alerts table yet, and the seeder must not hard
        # fail on that.
        alerts_table_exists = bool(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alerts'"
            ).fetchone()
        )
        if alerts_table_exists:
            conn.execute("DELETE FROM alerts")

        decisions = data.get("decisions", [])
        for row in decisions:
            conn.execute(
                "INSERT INTO decisions (timestamp, domain, summary, rationale, outcome, tags, department) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    row.get("timestamp", ""),
                    row.get("domain", "general"),
                    row.get("summary", ""),
                    row.get("rationale", ""),
                    row.get("outcome", ""),
                    row.get("tags", ""),
                    row.get("department", ""),
                ),
            )
        counts["decisions"] = len(decisions)

        initiatives = data.get("initiatives", [])
        for row in initiatives:
            conn.execute(
                "INSERT INTO initiatives (title, status, created_at, updated_at, summary, department) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    row.get("title", ""),
                    row.get("status", "active"),
                    row.get("created_at", ""),
                    row.get("updated_at", ""),
                    row.get("summary", ""),
                    row.get("department", ""),
                ),
            )
        counts["initiatives"] = len(initiatives)

        now = datetime.now(UTC)
        name_to_id: dict[str, int] = getattr(_seed_people, "_name_to_id", {})

        advice = data.get("advice_given", [])
        for row in advice:
            # Resolve a relative `timestamp` sentinel (e.g. "-1d", "-2d") so
            # seeded advice reads as recent in the activity rail and stays
            # reload-stable; absolute ISO and anything else pass through.
            ts = _resolve_run_at(row.get("timestamp", ""), now) or row.get("timestamp", "")
            conn.execute(
                "INSERT INTO advice_given (timestamp, domain, query_summary, advice_summary, department) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    ts,
                    row.get("domain", "general"),
                    row.get("query_summary", ""),
                    row.get("advice_summary", ""),
                    row.get("department", ""),
                ),
            )
        counts["advice_given"] = len(advice)

        # Pre-staged scheduled actions. Fixtures use these to demonstrate
        # act/propose/escalate behaviors within seconds of load, and to seed the
        # briefing's "In flight" / "Awaiting" lanes. `run_at` (and the optional
        # `awaiting_response_since`) accept a relative sentinel (e.g. "+30s",
        # "-45m", "-30h", "+2d") resolved against now-at-load-time so rows stay
        # "live" across reloads; absolute ISO timestamps still work.
        #
        # Demo-data safety contract for fixture authors: the scheduler claims
        # EVERY pending row whose run_at has passed (`claim_due_actions`:
        # `status='pending' AND run_at<=now` — no kind/channel filter), then
        # dispatches it subject to the department's authority gate. So a
        # PAST-dated row of ANY kind fires on the first tick after load and may
        # send a real message (fixtures use real emails/Discord IDs) unless its
        # department gates it to propose/defer. Keep staged rows FUTURE-dated.
        # The "overdue" briefing visual belongs in the Awaiting lane: put a PAST
        # `awaiting_response_since` on a FUTURE-dated row — overdue there is
        # computed from that field vs the person's SLA, independent of run_at,
        # so the row stays pending and is never claimed or sent.
        scheduled = data.get("scheduled_actions", [])
        seeded = 0
        for row in scheduled:
            run_at = _resolve_run_at(row.get("run_at", ""), now)
            if run_at is None:
                continue
            assigned_name = row.get("assigned_to_person")
            assigned_id = name_to_id.get(assigned_name) if assigned_name else None
            awaiting_raw = row.get("awaiting_response_since")
            awaiting_since = _resolve_run_at(awaiting_raw, now) if awaiting_raw else None
            conn.execute(
                "INSERT INTO scheduled_actions ("
                "created_at, run_at, channel, channel_ref, intent_text, "
                "originating_session_id, status, attempts, last_error, "
                "department, kind, assigned_to_person_id, "
                "awaiting_response_since, scope_key, required_scope"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    now.isoformat(),
                    run_at,
                    row.get("channel", "any"),
                    row.get("channel_ref", ""),
                    row.get("intent_text", ""),
                    None,
                    row.get("status", "pending"),
                    0,
                    "",
                    row.get("department", ""),
                    row.get("kind", "ad_hoc"),
                    assigned_id,
                    awaiting_since,
                    row.get("scope_key"),
                    row.get("required_scope"),
                ),
            )
            seeded += 1
        counts["scheduled_actions"] = seeded

        # Pre-staged alerts (briefing proposals). Seed the "Needs you" /
        # "Across the team" / "Monitoring" lanes deterministically on load.
        # `routed_to_person` is resolved by name → id (same cache as scheduled
        # actions); an unrouted external signal lands in Monitoring. Inserted
        # `unread` so they surface in /today. `created_at` accepts a relative
        # sentinel so items read as recent and stay reload-stable.
        alerts = data.get("alerts", []) if alerts_table_exists else []
        alert_seeded = 0
        for row in alerts:
            routed_name = row.get("routed_to_person")
            routed_id = name_to_id.get(routed_name) if routed_name else None
            created_at = _resolve_run_at(row.get("created_at", ""), now) or now.isoformat()
            conn.execute(
                "INSERT OR IGNORE INTO alerts ("
                "external_id, source, severity, headline, body, suggested_action, "
                "topic_tags, dedup_key, status, created_at, routed_to_person_id"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row.get("external_id", ""),
                    row.get("source", "internal"),
                    row.get("severity", "medium"),
                    row.get("headline", ""),
                    row.get("body", ""),
                    row.get("suggested_action", ""),
                    json.dumps(row.get("topic_tags", []) or []),
                    row.get("dedup_key", ""),
                    row.get("status", "unread"),
                    created_at,
                    routed_id,
                ),
            )
            alert_seeded += 1
        counts["alerts"] = alert_seeded

        conn.commit()
    finally:
        conn.close()

    return counts


def _seed_people(people_path: Path) -> int:
    """Replace the people table with rows from people.yaml. Returns count seeded.

    Wipes existing people and inserts the fixture's roster. Returns dict
    {name: id} so departments seeding can reference person IDs by name.
    """
    if not people_path.exists():
        return 0

    try:
        import yaml

        data = yaml.safe_load(people_path.read_text()) or {}
    except Exception:
        return 0

    rows = data.get("people", []) if isinstance(data, dict) else []
    if not rows:
        return 0

    from openexecutive.people import store as people_store
    from openexecutive.people.models import (
        AuthorityScope,
        AvailabilityWindow,
    )

    # Wipe existing rows — fixture loading is destructive by design.
    db_path = people_store.DB_PATH
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        try:
            # Child tables first to satisfy foreign-key ordering when PRAGMA
            # foreign_keys=ON. department_slugs are stored as a JSON column on
            # `people` itself (no separate junction table).
            conn.execute("DELETE FROM person_authority_scope")
            conn.execute("DELETE FROM person_availability")
            conn.execute("DELETE FROM people")
            conn.commit()
        finally:
            conn.close()

    name_to_id: dict[str, int] = {}
    for row in rows:
        full_name = row.get("full_name") or row.get("name")
        if not full_name:
            continue
        person_id = people_store.upsert_person(
            full_name=full_name,
            role=row.get("role", ""),
            email=row.get("email"),
            slack_user_id=row.get("slack_user_id"),
            telegram_chat_id=row.get("telegram_chat_id"),
            discord_user_id=row.get("discord_user_id"),
            preferred_channel=row.get("preferred_channel", "any"),
            response_sla_hours=int(row.get("response_sla_hours", 24)),
            department_slugs=list(row.get("department_slugs", [])),
            is_principal=bool(row.get("is_principal", False)),
        )
        name_to_id[full_name] = person_id

        scopes_raw = row.get("authority_scope", [])
        scopes: list[AuthorityScope] = []
        for s in scopes_raw:
            try:
                scopes.append(AuthorityScope(s))
            except ValueError:
                continue
        if scopes:
            people_store.set_authority_scope(person_id, scopes)

        windows_raw = row.get("availability", [])
        windows = []
        for w in windows_raw:
            try:
                windows.append(
                    AvailabilityWindow(
                        weekdays=list(w.get("weekdays", [])),
                        start_local=w.get("start_local", "09:00"),
                        end_local=w.get("end_local", "17:00"),
                        timezone=w.get("timezone", "UTC"),
                    )
                )
            except Exception:
                continue
        if windows:
            people_store.set_availability(person_id, windows)

    # Cache name->id for the departments seed step.
    _seed_people._name_to_id = name_to_id  # type: ignore[attr-defined]
    return len(name_to_id)


def _seed_departments(dept_path: Path) -> int:
    """Replace departments + Goals from departments.yaml. Returns count seeded.

    Wipes existing departments+Goals and inserts the fixture's org shape.
    Honours nullable specialist_key for informational-only departments.
    Resolves head_person_name to person IDs using the cache from _seed_people.

    Accepts either the legacy ``okrs`` key with a ``quarter`` field or the
    current ``goals`` key with ``period_type`` + ``period_value`` so existing
    fixture YAMLs continue to load after the Phase E rename.
    """
    if not dept_path.exists():
        return 0

    try:
        import yaml

        data = yaml.safe_load(dept_path.read_text()) or {}
    except Exception:
        return 0

    rows = data.get("departments", []) if isinstance(data, dict) else []
    if not rows:
        return 0

    from openexecutive.departments import store as dept_store
    from openexecutive.departments.models import AuthorityLevel

    db_path = dept_store.DB_PATH
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("DELETE FROM department_goals")
            conn.execute("DELETE FROM departments")
            conn.commit()
        finally:
            conn.close()

    name_to_id: dict[str, int] = getattr(_seed_people, "_name_to_id", {})

    now_ts = sqlite3.connect(":memory:").execute("SELECT datetime('now')").fetchone()[0]

    conn = sqlite3.connect(str(db_path))
    try:
        for row in rows:
            slug = row.get("slug")
            title = row.get("title")
            if not slug or not title:
                continue
            specialist_key = row.get("specialist_key")  # may be None — informational
            charter = row.get("charter", {}) or {}
            head_name = row.get("head_person_name")
            head_id = name_to_id.get(head_name) if head_name else None
            try:
                authority = AuthorityLevel(row.get("authority_level", "propose_only"))
            except ValueError:
                authority = AuthorityLevel.PROPOSE_ONLY

            conn.execute(
                """
                INSERT INTO departments
                  (slug, title, specialist_key,
                   charter_mission, charter_scope_json, charter_out_of_scope_json,
                   authority_level, head_person_id, cadences_json,
                   headcount, budget_usd, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    slug,
                    title,
                    specialist_key,
                    charter.get("mission", ""),
                    json.dumps(charter.get("scope", [])),
                    json.dumps(charter.get("out_of_scope", [])),
                    authority.value,
                    head_id,
                    json.dumps(row.get("cadences", {}) or {}),
                    row.get("headcount"),
                    row.get("budget_usd"),
                    now_ts,
                ),
            )
            # Accept both new ("goals") and legacy ("okrs") YAML shapes.
            goal_rows = row.get("goals") or row.get("okrs") or []
            for goal in goal_rows:
                # Legacy "quarter" → period_value with period_type="quarter".
                period_type = goal.get("period_type", "quarter")
                period_value = goal.get("period_value") or goal.get("quarter") or ""
                conn.execute(
                    """
                    INSERT INTO department_goals
                      (department_slug, period_type, period_value, key_result, target, current, status,
                       created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        slug,
                        period_type,
                        period_value,
                        goal.get("key_result", ""),
                        goal.get("target", ""),
                        goal.get("current", ""),
                        goal.get("status", "on_track"),
                        now_ts,
                        now_ts,
                    ),
                )
        conn.commit()
    finally:
        conn.close()

    return len(rows)
