"""Client-slot round-trip tests: save → switch → switch back must be lossless.

The slot mechanism's whole contract is "a slot is a faithful save file" —
these tests prove the SQLite state (decisions, scheduled actions, onboarding
plans), the company artifacts (profile, docs, mcp_servers.json), and the
operator-level tables behave correctly across switches. The vector and
Honcho layers are stubbed: they're side effects of a switch, not part of the
round-trip contract under test.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.clients import slots
from openexecutive.clients.slots import (
    ClientSlotConflictError,
    ClientSlotNotFoundError,
    activate_client_slot,
    create_client_slot,
    delete_client_slot,
    get_active_client,
    list_client_slots,
    save_active_client,
)


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Isolated company dir + episodic DB, with vector/Honcho layers stubbed."""
    company = tmp_path / "company"
    company.mkdir()
    settings = SimpleNamespace(
        company_profile_path=company / "profile.yaml",
        vector_store_path=tmp_path / "chroma",
        mcp_servers_config_path=company / "mcp_servers.json",
        honcho_workspace_id="default-ws",
    )

    db_path = tmp_path / "episodic.db"
    from openexecutive.departments import store as dept_store
    from openexecutive.memory import episodic
    from openexecutive.people import store as people_store
    from openexecutive.staff_onboarding import store as onboarding_store

    monkeypatch.setattr(episodic, "DB_PATH", db_path)
    monkeypatch.setattr(people_store, "DB_PATH", db_path)
    monkeypatch.setattr(dept_store, "DB_PATH", db_path)
    episodic.initialize_db(db_path)
    people_store.initialize_db(db_path)
    dept_store.initialize_db(db_path)
    onboarding_store.initialize_db(db_path)

    async def _no_vector(_settings: Any, _app_state: Any) -> int:
        return 0

    reseed_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(slots, "_rebuild_vector_state", _no_vector)
    monkeypatch.setattr(slots, "_set_honcho_client_workspace", lambda _slug: None)
    monkeypatch.setattr(
        slots, "_reseed_blank_defaults", lambda **kw: reseed_calls.append(kw)
    )
    return SimpleNamespace(
        settings=settings, db_path=db_path, company=company, reseed_calls=reseed_calls
    )


def _insert_decision(db_path: Path, summary: str) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO decisions (timestamp, domain, summary, rationale, outcome, tags, department) "
            "VALUES ('2026-06-01T00:00:00', 'strategy', ?, '', '', '', '')",
            (summary,),
        )
        conn.commit()
    finally:
        conn.close()


def _decision_summaries(db_path: Path) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return [r[0] for r in conn.execute("SELECT summary FROM decisions").fetchall()]
    finally:
        conn.close()


def _seed_live_company(env: SimpleNamespace, name: str = "Acme Corp") -> None:
    env.settings.company_profile_path.write_text(f"name: {name}\n")
    docs = env.company / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "strategy.md").write_text(f"# {name} strategy")
    env.settings.mcp_servers_config_path.write_text('{"servers": {"crayon": {}}}')
    _insert_decision(env.db_path, f"{name} decision")


async def test_create_from_current_captures_state_and_activates(env: SimpleNamespace) -> None:
    _seed_live_company(env, "Acme Corp")

    result = await create_client_slot(
        env.settings, display_name="Acme Corp", source="current"
    )

    assert result["slug"] == "acme_corp"
    assert result["active"] is True
    assert get_active_client(env.settings) == "acme_corp"

    slot = env.company / "_client_slots" / "acme_corp"
    assert (slot / "profile.yaml").exists()
    assert (slot / "docs" / "strategy.md").exists()
    assert (slot / "mcp_servers.json").exists()
    assert (slot / "state.db").exists()

    listed = list_client_slots(env.settings)
    assert [s["slug"] for s in listed] == ["acme_corp"]
    assert listed[0]["has_state"] is True
    assert listed[0]["has_mcp_config"] is True


async def test_switch_round_trip_is_lossless(env: SimpleNamespace) -> None:
    # Client A: full company with an onboarding plan and a scheduled action.
    _seed_live_company(env, "Acme Corp")
    from openexecutive.staff_onboarding import store as onboarding_store
    from openexecutive.staff_onboarding.models import OnboardingTemplate, TaskSpec

    onboarding_store.upsert_template(
        OnboardingTemplate(
            name="cfo_ramp",
            title="Fractional CFO ramp",
            department="finance",
            task_specs=[TaskSpec(title="Cash review")],
        ),
        env.db_path,
    )
    conn = sqlite3.connect(str(env.db_path))
    conn.execute(
        "INSERT INTO scheduled_actions (created_at, run_at, channel, channel_ref, "
        "intent_text, status, attempts, last_error, department, kind) "
        "VALUES ('2026-06-01T00:00:00', '2099-01-01T00:00:00', 'any', '', "
        "'follow up with Acme board', 'pending', 0, '', '', 'ad_hoc')"
    )
    conn.commit()
    conn.close()

    await create_client_slot(env.settings, display_name="Acme Corp", source="current")
    await create_client_slot(env.settings, display_name="Beta Inc", source="blank")

    # Switch to Beta: live state must be Beta's (empty), not Acme's.
    await activate_client_slot(env.settings, "beta_inc")
    assert get_active_client(env.settings) == "beta_inc"
    assert _decision_summaries(env.db_path) == []
    assert not env.settings.mcp_servers_config_path.exists()
    assert "Beta Inc" in env.settings.company_profile_path.read_text()

    # Do Beta-specific work, then switch back to Acme.
    _insert_decision(env.db_path, "Beta decision")
    (env.company / "docs").mkdir(exist_ok=True)
    (env.company / "docs" / "beta.md").write_text("# Beta")

    await activate_client_slot(env.settings, "acme_corp")
    assert get_active_client(env.settings) == "acme_corp"
    assert _decision_summaries(env.db_path) == ["Acme Corp decision"]
    assert (env.company / "docs" / "strategy.md").exists()
    assert not (env.company / "docs" / "beta.md").exists()
    assert env.settings.mcp_servers_config_path.exists()
    assert onboarding_store.get_template("cfo_ramp", env.db_path) is not None
    conn = sqlite3.connect(str(env.db_path))
    actions = conn.execute("SELECT intent_text FROM scheduled_actions").fetchall()
    conn.close()
    assert actions == [("follow up with Acme board",)]

    # And Beta's work survived its park.
    await activate_client_slot(env.settings, "beta_inc")
    assert _decision_summaries(env.db_path) == ["Beta decision"]
    assert (env.company / "docs" / "beta.md").exists()
    assert not (env.company / "docs" / "strategy.md").exists()


async def test_generated_fixtures_survive_switches(env: SimpleNamespace) -> None:
    """The fixture library is operator-level — never swapped with the client."""
    from openexecutive.fixtures import store as fixtures_store

    fixtures_store.initialize_db(env.db_path)
    _seed_live_company(env)
    await create_client_slot(env.settings, display_name="Acme", source="current")
    await create_client_slot(env.settings, display_name="Beta", source="blank")

    conn = sqlite3.connect(str(env.db_path))
    conn.execute(
        "INSERT INTO generated_fixtures (name, display_name, created_at, updated_at) "
        "VALUES ('halcyon_test', 'Halcyon Test', '2026-06-01', '2026-06-01')"
    )
    conn.commit()
    conn.close()

    await activate_client_slot(env.settings, "beta")

    conn = sqlite3.connect(str(env.db_path))
    rows = conn.execute("SELECT name FROM generated_fixtures").fetchall()
    conn.close()
    assert rows == [("halcyon_test",)]


async def test_first_activation_snapshots_original_company(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Activating with no active client preserves the user's company first."""
    _seed_live_company(env, "My Real Co")
    snapshots: list[str] = []
    monkeypatch.setattr(
        slots, "snapshot_user_state", lambda s: snapshots.append("taken")
    )

    await create_client_slot(env.settings, display_name="Beta", source="blank")
    await activate_client_slot(env.settings, "beta")

    assert snapshots == ["taken"]
    assert get_active_client(env.settings) == "beta"


async def test_create_from_current_snapshots_original_company(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Entering client mode via create-from-current must leave a _user_backup
    restore point so POST /fixtures/unload remains a working exit path."""
    _seed_live_company(env, "My Real Co")
    snapshots: list[str] = []
    monkeypatch.setattr(
        slots, "snapshot_user_state", lambda s: snapshots.append("taken")
    )

    await create_client_slot(env.settings, display_name="My Real Co", source="current")
    assert snapshots == ["taken"]

    # An existing backup is never overwritten by a later create.
    backup = env.company / "_user_backup"
    backup.mkdir(exist_ok=True)
    (backup / "profile.yaml").write_text("name: Original\n")
    # Exit client mode, then re-enter via a fresh create-from-current.
    (env.company / "_client_slots" / ".active_client").unlink()
    await create_client_slot(env.settings, display_name="Second", source="current")
    assert snapshots == ["taken"]  # no second snapshot


async def test_save_active_client_checkpoints_without_switching(env: SimpleNamespace) -> None:
    _seed_live_company(env)
    await create_client_slot(env.settings, display_name="Acme", source="current")

    _insert_decision(env.db_path, "late decision")
    result = await save_active_client(env.settings)
    assert result["saved"] is True

    # The slot's state.db now contains the late decision.
    slot_db = env.company / "_client_slots" / "acme" / "state.db"
    conn = sqlite3.connect(str(slot_db))
    rows = conn.execute("SELECT summary FROM decisions ORDER BY id").fetchall()
    conn.close()
    assert ("late decision",) in rows


async def test_save_with_no_active_client_raises(env: SimpleNamespace) -> None:
    with pytest.raises(ClientSlotConflictError):
        await save_active_client(env.settings)


async def test_operations_refuse_while_fixture_active(env: SimpleNamespace) -> None:
    backup = env.company / "_user_backup"
    backup.mkdir()
    (backup / ".fixture_active").write_text("halcyon_motors")

    with pytest.raises(ClientSlotConflictError):
        await create_client_slot(env.settings, display_name="Acme", source="current")
    with pytest.raises(ClientSlotConflictError):
        await save_active_client(env.settings)


async def test_create_from_current_refused_when_client_active(env: SimpleNamespace) -> None:
    _seed_live_company(env)
    await create_client_slot(env.settings, display_name="Acme", source="current")
    with pytest.raises(ClientSlotConflictError):
        await create_client_slot(env.settings, display_name="Other", source="current")


async def test_delete_refuses_active_then_deletes_parked(env: SimpleNamespace) -> None:
    _seed_live_company(env)
    await create_client_slot(env.settings, display_name="Acme", source="current")
    await create_client_slot(env.settings, display_name="Beta", source="blank")

    with pytest.raises(ClientSlotConflictError):
        await delete_client_slot(env.settings, "acme")

    result = await delete_client_slot(env.settings, "beta")
    assert result == {"deleted": True, "slug": "beta"}
    assert [s["slug"] for s in list_client_slots(env.settings)] == ["acme"]


async def test_activate_unknown_slug_raises_not_found(env: SimpleNamespace) -> None:
    with pytest.raises(ClientSlotNotFoundError):
        await activate_client_slot(env.settings, "nope")


async def test_activate_already_active_is_a_noop(env: SimpleNamespace) -> None:
    _seed_live_company(env)
    await create_client_slot(env.settings, display_name="Acme", source="current")
    result = await activate_client_slot(env.settings, "acme")
    assert result == {"slug": "acme", "already_active": True}


async def test_sentinel_garbage_is_ignored(env: SimpleNamespace) -> None:
    root = env.company / "_client_slots"
    root.mkdir()
    (root / ".active_client").write_text("../../etc/passwd")
    assert get_active_client(env.settings) is None


# ── Generated (engagement-intake) seed slots ────────────────────────────────


def _intake_bundle() -> dict[str, Any]:
    """A minimal valid engagement bundle (FixtureBundle shape)."""
    return {
        "profile": {
            "name": "Meridian Solar",
            "industry": "Commercial solar",
            "stage": "Private",
            "mission": "Margin-positive installs.",
        },
        "people": [
            {"full_name": "Dana Reyes", "role": "CEO", "is_principal": True},
            {"full_name": "Lee Park", "role": "VP Ops"},
        ],
        "departments": [
            {
                "slug": "operations",
                "title": "Operations",
                "head_person_name": "Lee Park",
            }
        ],
        "memory": {
            "decisions": [
                {
                    "timestamp": "2026-05-01T00:00:00",
                    "domain": "operations",
                    "summary": "Standardized on single-vendor inverters",
                }
            ],
            "initiatives": [],
            "advice_given": [],
            "alerts": [],
        },
        "docs": [
            {"filename": "intake_brief.md", "content": "# Intake brief"},
            {"filename": "open_questions.md", "content": "# Open questions"},
        ],
    }


async def test_generated_seed_slot_create_does_not_touch_live(env: SimpleNamespace) -> None:
    _seed_live_company(env, "My Real Co")

    result = await create_client_slot(
        env.settings,
        display_name="Meridian Solar",
        source="generated",
        bundle=_intake_bundle(),
        intake_description="kickoff call notes",
    )

    assert result["origin"] == "generated"
    assert result["active"] is False
    assert get_active_client(env.settings) is None
    # Live state untouched.
    assert "My Real Co" in env.settings.company_profile_path.read_text()
    assert _decision_summaries(env.db_path) == ["My Real Co decision"]

    slot = env.company / "_client_slots" / "meridian_solar"
    assert (slot / "profile.yaml").exists()
    assert (slot / "people.yaml").exists()
    assert (slot / "departments.yaml").exists()
    assert (slot / "memory.json").exists()
    assert sorted(p.name for p in (slot / "docs").glob("*.md")) == [
        "intake_brief.md",
        "open_questions.md",
    ]
    assert not (slot / "state.db").exists()

    listed = list_client_slots(env.settings)
    assert listed[0]["origin"] == "generated"


# "≈", "→" and "✅" sit outside cp1252, so an unencoded write_text() raises
# UnicodeEncodeError on a default Windows install. Accented Latin-1 characters
# alone would NOT reproduce it — they encode fine in cp1252.
_NON_ASCII_DOC = "Orçamento ≈ R$ 300 → prioridade: presença digital ✅"


async def test_generated_slot_pins_encoding_on_every_text_write(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slot creation must never write or read text at the platform default.

    ``Path.write_text()`` with no ``encoding`` uses the C-level locale
    (cp1252 on a default Windows install), which raised mid-write and
    surfaced as ``ClientSlotError: Failed to write client slot: 'charmap'
    codec can't encode ...`` for any client whose docs were not
    cp1252-representable. Doc bodies are the only artifact written verbatim —
    profile/people/departments YAML and memory.json go through
    ``yaml.safe_dump`` / ``json.dumps``, which escape non-ASCII and are pure
    ASCII on disk — but every site in this path is pinned so the next verbatim
    write is safe by default.

    This asserts the *contract* (each call passes an explicit encoding) rather
    than simulating a locale, because the locale cannot be faked from Python:
    patching ``locale.getencoding`` does not affect ``Path.write_text``, which
    reads the C-level locale. A symptom-based test would therefore be inert on
    the UTF-8 platform CI runs on, and would police nothing.
    """
    real_write, real_read = Path.write_text, Path.read_text
    offenders: list[str] = []

    def guarded_write(self, data, encoding=None, errors=None, newline=None):  # type: ignore[no-untyped-def]
        if encoding is None:
            offenders.append(f"write_text: {self.name}")
        return real_write(self, data, encoding=encoding or "utf-8", errors=errors, newline=newline)

    # No `newline` here: Path.read_text only accepts it on Python 3.13+, and CI
    # runs 3.11 — forwarding it unconditionally would TypeError the moment a
    # read_text call enters the guarded path (exactly the regression this guard
    # exists to catch). **kwargs keeps the forwarding correct on every version.
    def guarded_read(self, encoding=None, **kwargs):  # type: ignore[no-untyped-def]
        if encoding is None:
            offenders.append(f"read_text: {self.name}")
        return real_read(self, encoding=encoding or "utf-8", **kwargs)

    monkeypatch.setattr(Path, "write_text", guarded_write)
    monkeypatch.setattr(Path, "read_text", guarded_read)

    bundle = _intake_bundle()
    bundle["docs"] = [{"filename": "plano.md", "content": _NON_ASCII_DOC}]
    await create_client_slot(
        env.settings,
        display_name="Consultório Ação — Psicologia",
        slug="consultorio_acao",
        source="generated",
        bundle=bundle,
    )
    monkeypatch.undo()

    assert offenders == []

    # And the bytes that landed really are UTF-8, losslessly.
    slot = env.company / "_client_slots" / "consultorio_acao"
    assert (slot / "docs" / "plano.md").read_bytes().decode("utf-8") == _NON_ASCII_DOC


async def test_generated_seed_slot_activation_seeds_org_and_memory(
    env: SimpleNamespace,
) -> None:
    _seed_live_company(env, "My Real Co")
    await create_client_slot(env.settings, display_name="My Real Co", source="current")
    await create_client_slot(
        env.settings,
        display_name="Meridian Solar",
        source="generated",
        bundle=_intake_bundle(),
    )

    await activate_client_slot(env.settings, "meridian_solar")

    # Seeded people, departments, and memory landed in the live DB.
    assert _decision_summaries(env.db_path) == [
        "Standardized on single-vendor inverters"
    ]
    conn = sqlite3.connect(str(env.db_path))
    people = sorted(
        r[0] for r in conn.execute("SELECT full_name FROM people").fetchall()
    )
    depts = [
        r[0] for r in conn.execute("SELECT slug FROM departments").fetchall()
    ]
    conn.close()
    assert people == ["Dana Reyes", "Lee Park"]
    assert depts == ["operations"]
    # Default-department reseed was skipped (the draft supplied an org).
    assert env.reseed_calls[-1] == {"seed_departments": False}
    assert "Meridian Solar" in env.settings.company_profile_path.read_text()
    assert (env.company / "docs" / "intake_brief.md").exists()


async def test_seed_slot_save_back_prefers_state_db(env: SimpleNamespace) -> None:
    """Once a seed slot has been saved back, state.db wins over seed files."""
    _seed_live_company(env, "My Real Co")
    await create_client_slot(env.settings, display_name="My Real Co", source="current")
    await create_client_slot(
        env.settings,
        display_name="Meridian Solar",
        source="generated",
        bundle=_intake_bundle(),
    )
    await activate_client_slot(env.settings, "meridian_solar")
    _insert_decision(env.db_path, "engagement week-1 decision")

    # Park Meridian (save-back writes state.db), then return.
    await activate_client_slot(env.settings, "my_real_co")
    slot = env.company / "_client_slots" / "meridian_solar"
    assert (slot / "state.db").exists()
    assert (slot / "people.yaml").exists()  # birth record kept

    await activate_client_slot(env.settings, "meridian_solar")
    # state.db restore: both the seeded and the new decision survive, and the
    # seeders did NOT run again (people not duplicated).
    assert sorted(_decision_summaries(env.db_path)) == [
        "Standardized on single-vendor inverters",
        "engagement week-1 decision",
    ]
    conn = sqlite3.connect(str(env.db_path))
    n_people = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    conn.close()
    assert n_people == 2


async def test_generated_requires_valid_bundle(env: SimpleNamespace) -> None:
    with pytest.raises(slots.ClientSlotError):
        await create_client_slot(
            env.settings, display_name="X", source="generated", bundle=None
        )
    # Referential failure (department head not in roster) → rejected, no husk.
    bad = _intake_bundle()
    bad["departments"][0]["head_person_name"] = "Ghost"
    with pytest.raises(slots.ClientSlotError):
        await create_client_slot(
            env.settings, display_name="Ghost Co", source="generated", bundle=bad
        )
    assert not (env.company / "_client_slots" / "ghost_co").exists()
