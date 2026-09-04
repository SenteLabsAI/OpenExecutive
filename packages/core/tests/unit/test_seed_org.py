"""Tests for `openexecutive seed-org` against an in-memory fake of the HTTP API."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
import yaml
from click.testing import CliRunner

from openexecutive.cli.seed_org import (
    ApiClient,
    SeedError,
    apply_seed,
    derived_slug,
    load_seed_dir,
    seed_org,
)

# --------------------------------------------------------------------------- #
# Fake API
# --------------------------------------------------------------------------- #

class FakeApi:
    """Just enough of /company-profile, /people, /departments, /documents."""

    def __init__(self, *, departments: list[str] | None = None, people: list[dict[str, Any]] | None = None):
        self.profile: dict[str, Any] | None = None
        self.people: list[dict[str, Any]] = []
        self.departments: dict[str, dict[str, Any]] = {}
        self.goals: dict[int, dict[str, Any]] = {}
        self.documents: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.raw_calls: list[tuple[str, str]] = []  # percent-encoded paths as sent on the wire
        self._next_person = 1
        self._next_goal = 1
        for slug in departments or []:
            self.departments[slug] = self._dept_state(slug, slug.title())
        for row in people or []:
            self._add_person(row)

    @staticmethod
    def _dept_state(slug: str, title: str, mission: str = "") -> dict[str, Any]:
        return {
            "config": {"slug": slug, "title": title, "specialist_key": None,
                       "charter": {"mission": mission, "scope": [], "out_of_scope": []},
                       "authority_level": "propose_only", "head_person_id": None, "cadences": {}},
            "goals": [], "headcount": None, "budget_usd": None,
        }

    def _add_person(self, row: dict[str, Any]) -> dict[str, Any]:
        person = {"id": self._next_person, "is_principal": False, "reports_to_person_id": None,
                  "department_slugs": [], "authority_scope": [], "email": None, **row}
        self._next_person += 1
        self.people.append(person)
        return person

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        self.raw_calls.append((method, request.url.raw_path.decode().split("?")[0]))
        body = json.loads(request.content) if request.content and request.headers.get("content-type", "").startswith("application/json") else {}

        if path == "/company-profile" and method == "PUT":
            self.profile = body
            return httpx.Response(200, json=body)
        if path == "/people" and method == "GET":
            return httpx.Response(200, json=self.people)
        if path == "/people" and method == "POST":
            return httpx.Response(201, json=self._add_person(body))
        m = re.fullmatch(r"/people/(\d+)", path)
        if m and method == "PATCH":
            person = next(p for p in self.people if p["id"] == int(m.group(1)))
            person.update(body)
            return httpx.Response(200, json=person)
        if path == "/departments" and method == "GET":
            out = []
            for slug, state in self.departments.items():
                state["goals"] = [g for g in self.goals.values() if g["department_slug"] == slug]
                out.append(state)
            return httpx.Response(200, json=out)
        if path == "/departments" and method == "POST":
            slug = derived_slug(body["title"])
            self.departments[slug] = self._dept_state(slug, body["title"], body.get("mission", ""))
            return httpx.Response(201, json=self.departments[slug])
        m = re.fullmatch(r"/departments/([a-z0-9_-]+)", path)
        if m and method == "PATCH":
            state = self.departments[m.group(1)]
            for key in ("headcount", "budget_usd"):
                if key in body:
                    state[key] = body.pop(key)
            state["config"].update(body)
            return httpx.Response(200, json=state)
        if m and method == "DELETE":
            self.departments.pop(m.group(1))
            return httpx.Response(204)
        m = re.fullmatch(r"/departments/([a-z0-9_-]+)/goals", path)
        if m and method == "POST":
            goal = {"id": self._next_goal, "department_slug": m.group(1), "current": "", "status": "on_track", **body}
            self.goals[goal["id"]] = goal
            self._next_goal += 1
            return httpx.Response(201, json=goal)
        m = re.fullmatch(r"/departments/([a-z0-9_-]+)/goals/(\d+)", path)
        if m and method == "PATCH":
            goal = self.goals[int(m.group(2))]
            goal.update(body)
            return httpx.Response(200, json=goal)
        if path == "/documents" and method == "GET":
            return httpx.Response(200, json={"documents": [{"filename": n} for n in self.documents]})
        if path == "/documents" and method == "POST":
            ct = request.headers["content-type"]
            assert ct.startswith("multipart/form-data"), ct
            raw = request.content.decode("utf-8", errors="replace")
            name = re.search(r'filename="([^"]+)"', raw).group(1)
            domain = re.search(r'name="domain"\r\n\r\n([^\r]+)', raw).group(1)
            self.documents[name] = domain
            return httpx.Response(200, json={"filename": name, "chunks_indexed": 1, "domain": domain, "status": "ok"})
        m = re.fullmatch(r"/documents/([^/]+)", path)
        if m and method == "DELETE":
            self.documents.pop(unquote(m.group(1)))
            return httpx.Response(200, json={"status": "deleted"})
        return httpx.Response(404, json={"detail": f"unhandled {method} {path}"})


# --------------------------------------------------------------------------- #
# Seed directory fixture
# --------------------------------------------------------------------------- #

PROFILE = {"company": {"name": "Sente Labs", "industry": "AI R&D", "stage": "Bootstrapped LLC", "headcount": 5}}
PEOPLE = {
    "people": [
        {"full_name": "Rufus Johnson", "role": "Principal", "is_principal": True,
         "email": "rufus@example.com", "authority_scope": ["wildcard"], "department_slugs": ["engineering"]},
        {"name": "Alex Cohen", "role": "Sales Lead", "email": "Alex@Example.com", "reports_to": "Rufus Johnson",
         "authority_scope": ["spend_lt_10k"], "department_slugs": ["strategy"]},
    ]
}
DEPARTMENTS = {
    "departments": [
        {"slug": "strategy", "title": "Sales & Client Development", "head_person_name": "Alex Cohen",
         "authority_level": "escalate", "charter": {"mission": "Win engagements", "scope": ["pipeline"]},
         "cadences": {"check_in": "weekly@mon@14:00"},
         "okrs": [{"quarter": "Q3 2026", "key_result": "10 working sessions", "target": "10", "current": "2", "status": "at_risk"}]},
        {"slug": "engineering", "title": "Engineering — Open Executive", "head_person_name": "Rufus Johnson",
         "charter": {"mission": "Ship OE"}, "headcount": 3,
         "goals": [{"period_type": "quarter", "period_value": "Q3 2026", "key_result": "QA live", "target": "done"}]},
    ]
}


@pytest.fixture()
def seed_dir(tmp_path: Path) -> Path:
    (tmp_path / "profile.yaml").write_text(yaml.safe_dump(PROFILE))
    (tmp_path / "people.yaml").write_text(yaml.safe_dump(PEOPLE))
    (tmp_path / "departments.yaml").write_text(yaml.safe_dump(DEPARTMENTS))
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "brand.md").write_text("# Brand\n")
    (docs / "notes.txt").write_text("notes\n")
    (docs / "ignored.png").write_bytes(b"\x89PNG")
    (tmp_path / "docs.yaml").write_text(yaml.safe_dump({"docs": [{"file": "brand.md", "domain": "marketing"}]}))
    return tmp_path


def _client(fake: FakeApi, *, dry_run: bool = False) -> ApiClient:
    return ApiClient("http://fake", "secret", dry_run=dry_run, transport=fake.transport())


# --------------------------------------------------------------------------- #
# Loading / validation
# --------------------------------------------------------------------------- #

def test_load_seed_dir_normalizes(seed_dir: Path) -> None:
    bundle = load_seed_dir(seed_dir)
    assert bundle.profile is not None and bundle.profile["name"] == "Sente Labs"
    assert bundle.people[1]["full_name"] == "Alex Cohen"  # `name` alias accepted
    strategy = bundle.departments[0]
    assert strategy["goals"][0] == {
        "period_type": "quarter", "period_value": "Q3 2026", "key_result": "10 working sessions",
        "target": "10", "current": "2", "status": "at_risk",
    }
    assert [d.path.name for d in bundle.docs] == ["brand.md", "notes.txt"]
    assert {d.path.name: d.domain for d in bundle.docs} == {"brand.md": "marketing", "notes.txt": "general"}


@pytest.mark.parametrize(
    ("filename", "content", "message"),
    [
        ("people.yaml", {"people": [{"full_name": "X", "authority_scope": ["god_mode"]}]}, "unknown authority scope"),
        ("people.yaml", {"people": [{"role": "no name"}]}, "needs a `full_name`"),
        ("people.yaml", {"people": [{"full_name": "X", "title": "unsupported"}]}, "unsupported keys"),
        ("departments.yaml", {"departments": [{"title": "No slug"}]}, "needs a `slug`"),
        ("departments.yaml", {"departments": [{"slug": "x", "title": "X", "goals": [{"key_result": "k"}]}]}, "missing `period_value`"),
        ("docs.yaml", {"docs": [{"file": "brand.md", "domain": "nope"}]}, "unknown domain"),
        ("docs.yaml", {"docs": [{"file": "missing.md", "domain": "general"}]}, "not present in docs/"),
        ("profile.yaml", {"company": {"industry": "no name"}}, "`company.name` is required"),
    ],
)
def test_load_seed_dir_rejects_bad_input(seed_dir: Path, filename: str, content: dict, message: str) -> None:
    (seed_dir / filename).write_text(yaml.safe_dump(content))
    with pytest.raises(SeedError, match=re.escape(message)):
        load_seed_dir(seed_dir)


def test_load_seed_dir_requires_something(tmp_path: Path) -> None:
    with pytest.raises(SeedError, match="Nothing to seed"):
        load_seed_dir(tmp_path)


def test_derived_slug_matches_server_rules() -> None:
    assert derived_slug("Engineering — Open Executive") == "engineering-open-executive"
    assert derived_slug("R&D & New Products") == "r-d-new-products"
    assert derived_slug("!!!") == "department"


# --------------------------------------------------------------------------- #
# Apply: create, update, idempotency, prune, docs
# --------------------------------------------------------------------------- #

def test_first_run_creates_everything(seed_dir: Path) -> None:
    fake = FakeApi(departments=["strategy", "finance"])
    report = apply_seed(load_seed_dir(seed_dir), _client(fake))

    assert fake.profile["name"] == "Sente Labs"
    assert report.profile == "replaced (Sente Labs)"
    assert report.people_created == ["Rufus Johnson", "Alex Cohen"]

    rufus = next(p for p in fake.people if p["full_name"] == "Rufus Johnson")
    alex = next(p for p in fake.people if p["full_name"] == "Alex Cohen")
    assert rufus["is_principal"] is True
    assert alex["reports_to_person_id"] == rufus["id"]
    # engineering was created under the server-derived slug and people were remapped
    assert "engineering-open-executive" in fake.departments
    assert rufus["department_slugs"] == ["engineering-open-executive"]
    assert report.departments_created == ["engineering-open-executive"]
    assert any("remapped" in w for w in report.warnings)

    strategy = fake.departments["strategy"]
    assert strategy["config"]["title"] == "Sales & Client Development"
    assert strategy["config"]["authority_level"] == "escalate"
    assert strategy["config"]["head_person_id"] == alex["id"]
    assert strategy["config"]["charter"] == {"mission": "Win engagements", "scope": ["pipeline"], "out_of_scope": []}
    assert strategy["config"]["cadences"] == {"check_in": "weekly@mon@14:00"}
    assert fake.departments["engineering-open-executive"]["headcount"] == 3
    assert report.goals_created == 2
    goals_by_slug = {g["department_slug"]: g for g in fake.goals.values()}
    assert goals_by_slug["strategy"]["period_value"] == "Q3 2026"
    assert goals_by_slug["strategy"]["status"] == "at_risk"

    assert fake.documents == {"brand.md": "marketing", "notes.txt": "general"}
    assert report.docs_uploaded == ["brand.md", "notes.txt"]
    # finance is untouched without --prune-departments
    assert "finance" in fake.departments and report.departments_deleted == []


def test_second_run_is_idempotent(seed_dir: Path) -> None:
    fake = FakeApi(departments=["strategy"])
    apply_seed(load_seed_dir(seed_dir), _client(fake))
    fake.calls.clear()

    report = apply_seed(load_seed_dir(seed_dir), _client(fake))
    assert report.people_created == [] and report.people_updated == ["Rufus Johnson", "Alex Cohen"]
    assert report.departments_created == []
    assert report.goals_created == 0 and report.goals_updated == 0
    assert report.docs_uploaded == [] and report.docs_skipped == ["brand.md", "notes.txt"]
    assert ("POST", "/people") not in fake.calls
    assert ("POST", "/departments") not in fake.calls
    assert ("POST", "/documents") not in fake.calls
    assert len(fake.people) == 2


def test_update_matches_by_email_case_insensitively_and_warns_on_principal(seed_dir: Path) -> None:
    fake = FakeApi(
        departments=["strategy"],
        people=[{"full_name": "Alexander Cohen", "email": "ALEX@example.com", "role": "old"},
                {"full_name": "Rufus Johnson", "email": "other@example.com", "is_principal": False}],
    )
    # Rufus is matched by name with a different email → needs the explicit opt-in.
    report = apply_seed(load_seed_dir(seed_dir), _client(fake), allow_email_change=True)
    assert report.people_created == []
    alex = next(p for p in fake.people if p["email"] == "ALEX@example.com" or p["email"] == "Alex@Example.com")
    assert alex["role"] == "Sales Lead"           # matched on email despite name difference
    assert any("is_principal differs" in w for w in report.warnings)  # name-matched Rufus is not principal


def test_name_match_refuses_email_change_without_flag(seed_dir: Path) -> None:
    fake = FakeApi(
        departments=["strategy"],
        people=[{"full_name": "Alex Cohen", "email": "alex@corp.example", "authority_scope": ["spend_lt_2k"]}],
    )
    with pytest.raises(SeedError, match="--allow-email-change"):
        apply_seed(load_seed_dir(seed_dir), _client(fake))
    alex = next(p for p in fake.people if p["full_name"] == "Alex Cohen")
    assert alex["email"] == "alex@corp.example" and alex["authority_scope"] == ["spend_lt_2k"]

    report = apply_seed(load_seed_dir(seed_dir), _client(fake), allow_email_change=True)
    assert "Alex Cohen" in report.people_updated
    assert alex["email"] == "Alex@Example.com"


def test_hardlinked_doc_is_rejected(seed_dir: Path, tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-key.txt"
    outside.write_text("SSH PRIVATE KEY\n")
    (seed_dir / "docs" / "handbook.md").hardlink_to(outside)
    with pytest.raises(SeedError, match="hardlinked"):
        load_seed_dir(seed_dir)


def test_doc_swapped_after_validation_is_rejected_at_upload(seed_dir: Path, tmp_path: Path) -> None:
    bundle = load_seed_dir(seed_dir)
    outside = tmp_path.parent / f"{tmp_path.name}-late.txt"
    outside.write_text("late\n")
    target = seed_dir / "docs" / "brand.md"
    target.unlink()
    target.symlink_to(outside)
    fake = FakeApi(departments=["strategy"])
    with pytest.raises(SeedError, match="symlinks"):
        apply_seed(bundle, _client(fake))
    assert "brand.md" not in fake.documents


def test_goal_update_only_sends_diff(seed_dir: Path) -> None:
    fake = FakeApi(departments=["strategy"])
    apply_seed(load_seed_dir(seed_dir), _client(fake))
    data = yaml.safe_load((seed_dir / "departments.yaml").read_text())
    data["departments"][0]["okrs"][0]["current"] = "7"
    (seed_dir / "departments.yaml").write_text(yaml.safe_dump(data))

    report = apply_seed(load_seed_dir(seed_dir), _client(fake))
    assert report.goals_updated == 1 and report.goals_created == 0
    goal = next(g for g in fake.goals.values() if g["department_slug"] == "strategy")
    assert goal["current"] == "7" and goal["status"] == "at_risk"


def test_prune_deletes_departments_not_in_seed(seed_dir: Path) -> None:
    fake = FakeApi(departments=["strategy", "hr", "board_comms"])
    report = apply_seed(load_seed_dir(seed_dir), _client(fake), prune_departments=True)
    assert sorted(report.departments_deleted) == ["board_comms", "hr"]
    assert set(fake.departments) == {"strategy", "engineering-open-executive"}


def test_prune_refuses_without_departments_yaml(seed_dir: Path) -> None:
    (seed_dir / "departments.yaml").unlink()
    fake = FakeApi(departments=["strategy", "hr"])
    with pytest.raises(SeedError, match="refusing to delete every department"):
        apply_seed(load_seed_dir(seed_dir), _client(fake), prune_departments=True)
    assert set(fake.departments) == {"strategy", "hr"}
    assert fake.profile is None and fake.people == []  # aborted before any write
    assert all(method == "GET" for method, _ in fake.calls)


def test_duplicate_yaml_slug_is_rejected(seed_dir: Path) -> None:
    (seed_dir / "departments.yaml").write_text(yaml.safe_dump({"departments": [
        {"slug": "eng", "title": "Engineering", "charter": {"mission": "a"}},
        {"slug": "eng", "title": "Engineering Ops", "charter": {"mission": "b"}},
    ]}))
    with pytest.raises(SeedError, match="slug `eng` appears more than once"):
        load_seed_dir(seed_dir)


def test_dry_run_warns_on_unknown_head_person(seed_dir: Path) -> None:
    (seed_dir / "departments.yaml").write_text(yaml.safe_dump({"departments": [
        {"slug": "strategy", "title": "Strategy", "head_person_name": "Nonexistent Person"},
    ]}))
    fake = FakeApi(departments=["strategy"])
    report = apply_seed(load_seed_dir(seed_dir), _client(fake, dry_run=True))
    assert any("Nonexistent Person" in w for w in report.warnings)


def test_duplicate_server_names_warn(seed_dir: Path) -> None:
    fake = FakeApi(departments=["strategy"], people=[{"full_name": "Sam Twin"}, {"full_name": "sam twin"}])
    report = apply_seed(load_seed_dir(seed_dir), _client(fake))
    assert any("more than one person named `sam twin`" in w for w in report.warnings)


def test_colliding_derived_slugs_are_rejected(seed_dir: Path) -> None:
    (seed_dir / "departments.yaml").write_text(yaml.safe_dump({"departments": [
        {"slug": "rnd", "title": "R&D", "charter": {"mission": "a"}},
        {"slug": "rd2", "title": "R!D", "charter": {"mission": "b"}},
    ]}))
    fake = FakeApi()
    with pytest.raises(SeedError, match="both resolve to server department `r-d`"):
        apply_seed(load_seed_dir(seed_dir), _client(fake))


def test_reports_to_resolves_against_server_only_manager(seed_dir: Path) -> None:
    fake = FakeApi(departments=["strategy"], people=[{"full_name": "Existing Boss", "email": "boss@example.com"}])
    (seed_dir / "people.yaml").write_text(yaml.safe_dump({"people": [
        {"full_name": "Nova Newhire", "email": "nova@example.com", "reports_to": "existing boss"},
    ]}))
    report = apply_seed(load_seed_dir(seed_dir), _client(fake))
    nova = next(p for p in fake.people if p["full_name"] == "Nova Newhire")
    boss = next(p for p in fake.people if p["full_name"] == "Existing Boss")
    assert nova["reports_to_person_id"] == boss["id"]
    assert not any("reports_to" in w for w in report.warnings)


def test_dry_run_plans_reports_to_and_warns_on_unknown_manager(seed_dir: Path) -> None:
    (seed_dir / "people.yaml").write_text(yaml.safe_dump({"people": [
        {"full_name": "Rufus Johnson", "email": "r@example.com"},
        {"full_name": "Nova Newhire", "email": "nova@example.com", "reports_to": "Rufus Johnson"},
        {"full_name": "Lost Soul", "email": "lost@example.com", "reports_to": "Nonexistent Manager"},
    ]}))
    fake = FakeApi(departments=["strategy"])
    api = _client(fake, dry_run=True)
    report = apply_seed(load_seed_dir(seed_dir), api)
    assert any("set reports_to for `Nova Newhire` → `Rufus Johnson`" in line for line in api.planned_writes)
    assert any("Lost Soul" in w and "Nonexistent Manager" in w for w in report.warnings)
    assert all(method == "GET" for method, _ in fake.calls)


def test_reindex_docs_deletes_then_reuploads(seed_dir: Path) -> None:
    fake = FakeApi(departments=["strategy"])
    apply_seed(load_seed_dir(seed_dir), _client(fake))
    fake.calls.clear()
    report = apply_seed(load_seed_dir(seed_dir), _client(fake), reindex_docs=True)
    assert report.docs_uploaded == ["brand.md", "notes.txt"]
    assert ("DELETE", "/documents/brand.md") in fake.calls
    assert fake.calls.index(("DELETE", "/documents/brand.md")) < fake.calls.index(("POST", "/documents"))


def test_reindex_encodes_filenames_in_delete_path(seed_dir: Path) -> None:
    (seed_dir / "docs" / "Q3 Plan #2.md").write_text("# plan\n")
    fake = FakeApi(departments=["strategy"])
    apply_seed(load_seed_dir(seed_dir), _client(fake))
    assert "Q3 Plan #2.md" in fake.documents
    fake.calls.clear()
    apply_seed(load_seed_dir(seed_dir), _client(fake), reindex_docs=True)
    assert ("DELETE", "/documents/Q3%20Plan%20%232.md") in fake.raw_calls
    assert "Q3 Plan #2.md" in fake.documents and "Q3 Plan " not in fake.documents


def test_symlinked_doc_is_rejected(seed_dir: Path, tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-secret.txt"
    outside.write_text("SECRET=1\n")
    (seed_dir / "docs" / "handbook.md").symlink_to(outside)
    with pytest.raises(SeedError, match="symlinks"):
        load_seed_dir(seed_dir)


def test_cleartext_remote_detection() -> None:
    from openexecutive.cli.seed_org import _is_cleartext_remote

    assert _is_cleartext_remote("http://openexec-api-qa.fly.dev")
    assert not _is_cleartext_remote("https://openexec-api-qa.fly.dev")
    assert not _is_cleartext_remote("http://localhost:8000")
    assert not _is_cleartext_remote("http://127.0.0.1:8000")


def test_dry_run_sends_no_writes(seed_dir: Path) -> None:
    fake = FakeApi(departments=["strategy", "hr"])
    api = _client(fake, dry_run=True)
    report = apply_seed(load_seed_dir(seed_dir), api, prune_departments=True)
    assert all(method == "GET" for method, _ in fake.calls)
    assert fake.profile is None and fake.people == [] and "hr" in fake.departments
    assert report.people_created == ["Rufus Johnson", "Alex Cohen"]
    assert report.departments_deleted == ["hr"]
    assert any(line.startswith("PUT /company-profile") for line in api.planned_writes)
    assert any(line.startswith("DELETE /departments/hr") for line in api.planned_writes)


def test_http_error_is_a_clear_seed_error(seed_dir: Path) -> None:
    def deny(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    api = ApiClient("http://fake", None, dry_run=False, transport=httpx.MockTransport(deny))
    with pytest.raises(SeedError, match="401.*BACKEND_SHARED_SECRET"):
        apply_seed(load_seed_dir(seed_dir), api)


# --------------------------------------------------------------------------- #
# CLI surface
# --------------------------------------------------------------------------- #

def test_cli_dry_run_end_to_end(seed_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeApi(departments=["strategy"])

    def fake_init(self: ApiClient, base_url: str, api_key: str | None, *, dry_run: bool, transport: Any = None) -> None:
        _init_with_fake(self, base_url, api_key, dry_run, fake)

    monkeypatch.setattr(ApiClient, "__init__", fake_init)
    monkeypatch.setenv("BACKEND_SHARED_SECRET", "s3cret")
    result = CliRunner().invoke(seed_org, ["--api", "http://fake", "--dir", str(seed_dir), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "dry run" in result.output
    assert "PUT /company-profile" in result.output
    assert all(method == "GET" for method, _ in fake.calls)


def _init_with_fake(self: ApiClient, base_url: str, api_key: str | None, dry_run: bool, fake: FakeApi) -> None:
    self._client = httpx.Client(base_url=base_url, headers={"x-api-key": api_key or ""}, transport=fake.transport())
    self.dry_run = dry_run
    self.planned_writes = []


def test_cli_reports_validation_errors(seed_dir: Path) -> None:
    (seed_dir / "people.yaml").write_text(yaml.safe_dump({"people": [{"role": "nameless"}]}))
    result = CliRunner().invoke(seed_org, ["--api", "http://fake", "--dir", str(seed_dir), "--dry-run"])
    assert result.exit_code != 0
    assert "needs a `full_name`" in result.output
