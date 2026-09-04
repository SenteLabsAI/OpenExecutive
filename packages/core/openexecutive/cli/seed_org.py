"""``openexecutive seed-org`` — apply a real company's org to a running API.

Reads a private directory of fixture-shaped files and upserts them through the
public HTTP API, so an operator can configure (and re-configure) an instance
for a real organization without committing company data to the repo and
without the demo-mode fixture loader, which wipes state and only reads the
fixtures baked into the image.

Directory layout (every file optional)::

    profile.yaml       # {company: {...}}  — CompanyProfile fields
    people.yaml        # {people: [...]}   — Person fields + optional reports_to
    departments.yaml   # {departments: [...]} — same shape as fixtures/companies
    docs/*.md|txt|pdf|docx|doc
    docs.yaml          # {docs: [{file: name.md, domain: marketing}, ...]}

Semantics:

* Profile is a full replace (``PUT /company-profile``).
* People match by email (case-insensitive), then by full name. ``reports_to``
  is a person's name and is resolved in a second pass.
* Departments match by ``slug`` (then by the slug the server would derive
  from the title, then by exact title). Existing departments are patched in
  place; missing ones are created via ``POST /departments`` (the server derives
  the slug from the title, so the YAML slug is remapped to the server's slug and
  people's ``department_slugs`` follow). Retitling a department the seed
  created earlier therefore needs its YAML ``slug`` set to the server slug it
  got, or the run creates a second department. ``head_person_name`` resolves
  through the people pass. Goals match by ``(period_value, key_result)``;
  rewording a ``key_result`` creates a new goal rather than renaming the old.
* Docs upload once per filename; ``--reindex-docs`` deletes and re-uploads.
* ``--dry-run`` prints every write it would make and sends none.
"""

from __future__ import annotations

import mimetypes
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import click
import httpx
import yaml
from rich.console import Console
from rich.table import Table

from openexecutive.memory.company_profile import CompanyProfile
from openexecutive.people.models import AuthorityScope

console = Console()

DOC_EXTENSIONS = frozenset({".md", ".txt", ".pdf", ".docx", ".doc"})
DOC_DOMAINS = frozenset(
    {"strategy", "finance", "hr", "legal", "operations", "marketing", "board", "product", "general"}
)
_PERSON_CREATE_FIELDS = (
    "full_name",
    "role",
    "is_principal",
    "department_slugs",
    "email",
    "slack_user_id",
    "telegram_chat_id",
    "discord_user_id",
    "preferred_channel",
    "response_sla_hours",
    "on_leave_until",
    "authority_scope",
    "availability",
)
# PATCH cannot change is_principal; it is reported instead of silently ignored.
_PERSON_PATCH_FIELDS = tuple(f for f in _PERSON_CREATE_FIELDS if f != "is_principal")
_DEPARTMENT_PATCH_FIELDS = (
    "title",
    "authority_level",
    "cadences",
    "headcount",
    "budget_usd",
    "slack_channel_id",
    "discord_channel_id",
    "telegram_chat_id",
)
_GOAL_FIELDS = ("period_type", "period_value", "key_result", "target", "current", "status")
# Uploads of large PDFs and first-time ChromaDB indexing can take a while.
_HTTP_TIMEOUT_S = 120.0
# Keep server error bodies readable in the CLI without flooding the terminal.
_ERROR_DETAIL_MAX_CHARS = 500
_GOAL_LABEL_MAX_CHARS = 50


class SeedError(click.ClickException):
    """A validation or HTTP failure that should stop the seed with a clear message."""


def derived_slug(title: str) -> str:
    """Mirror of the server-side slug derivation for ``POST /departments``."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or "department"


# --------------------------------------------------------------------------- #
# Input loading
# --------------------------------------------------------------------------- #

@dataclass
class DocSpec:
    path: Path
    domain: str


@dataclass
class SeedBundle:
    profile: dict[str, Any] | None
    people: list[dict[str, Any]]
    departments: list[dict[str, Any]]
    docs: list[DocSpec]


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise SeedError(f"{path.name}: invalid YAML — {exc}") from exc
    if not isinstance(data, dict):
        raise SeedError(f"{path.name}: expected a mapping at the top level")
    return data


def _load_docs(seed_dir: Path) -> list[DocSpec]:
    docs_dir = seed_dir / "docs"
    if not docs_dir.is_dir():
        return []
    domains: dict[str, str] = {}
    manifest_path = seed_dir / "docs.yaml"
    if manifest_path.exists():
        manifest = _load_yaml(manifest_path)
        for entry in manifest.get("docs", []) or []:
            if not isinstance(entry, dict) or "file" not in entry:
                raise SeedError("docs.yaml: each entry needs a `file` key")
            domain = str(entry.get("domain", "general"))
            if domain not in DOC_DOMAINS:
                raise SeedError(
                    f"docs.yaml: `{entry['file']}` has unknown domain `{domain}` "
                    f"(valid: {', '.join(sorted(DOC_DOMAINS))})"
                )
            domains[str(entry["file"])] = domain
    specs: list[DocSpec] = []
    docs_root = docs_dir.resolve()
    for path in sorted(docs_dir.iterdir()):
        if path.name.startswith(".") or path.suffix.lower() not in DOC_EXTENSIONS:
            continue
        # A seed directory may have been unpacked from someone else's archive;
        # refuse symlinks (and anything resolving outside docs/) so a link named
        # `handbook.md` cannot smuggle an arbitrary local file into the knowledge base.
        _check_regular_file_inside(path, docs_root)
        specs.append(DocSpec(path=path, domain=domains.get(path.name, "general")))
    unknown = sorted(set(domains) - {s.path.name for s in specs})
    if unknown:
        raise SeedError(f"docs.yaml lists files not present in docs/: {', '.join(unknown)}")
    return specs


def _check_regular_file_inside(path: Path, docs_root: Path) -> None:
    """Reject symlinks, hardlinks, and anything resolving outside docs/.

    Re-run right before the bytes are read (not only at load time) so the
    entry cannot be swapped between validation and upload.
    """
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(docs_root):
        raise SeedError(f"docs/{path.name}: symlinks and files outside docs/ are not allowed")
    if path.stat().st_nlink != 1:
        raise SeedError(f"docs/{path.name}: hardlinked files are not allowed")


def _read_doc_bytes(spec: DocSpec) -> bytes:
    _check_regular_file_inside(spec.path, spec.path.parent.resolve())
    return spec.path.read_bytes()


def load_seed_dir(seed_dir: Path) -> SeedBundle:
    """Parse and validate the seed directory. Raises SeedError on bad input."""
    if not seed_dir.is_dir():
        raise SeedError(f"Seed directory not found: {seed_dir}")

    profile: dict[str, Any] | None = None
    profile_path = seed_dir / "profile.yaml"
    if profile_path.exists():
        raw = _load_yaml(profile_path)
        profile = raw.get("company", raw)
        try:
            validated = CompanyProfile.model_validate(profile)
        except Exception as exc:  # pydantic ValidationError
            raise SeedError(f"profile.yaml does not validate as a CompanyProfile:\n{exc}") from exc
        if validated.is_empty():
            raise SeedError("profile.yaml: `company.name` is required")
        profile = validated.model_dump()

    people: list[dict[str, Any]] = []
    people_path = seed_dir / "people.yaml"
    if people_path.exists():
        for row in _load_yaml(people_path).get("people", []) or []:
            people.append(_normalize_person(row))

    departments: list[dict[str, Any]] = []
    dept_path = seed_dir / "departments.yaml"
    if dept_path.exists():
        for row in _load_yaml(dept_path).get("departments", []) or []:
            departments.append(_normalize_department(row))
        seen: set[str] = set()
        for dept in departments:
            if dept["slug"] in seen:
                raise SeedError(f"departments.yaml: slug `{dept['slug']}` appears more than once")
            seen.add(dept["slug"])

    if profile is None and not people and not departments:
        raise SeedError(f"Nothing to seed in {seed_dir} (no profile.yaml, people.yaml, or departments.yaml)")

    return SeedBundle(profile=profile, people=people, departments=departments, docs=_load_docs(seed_dir))


def _normalize_person(row: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise SeedError("people.yaml: each person must be a mapping")
    person = dict(row)
    # Accept the fixture loader's `name` alias.
    if "full_name" not in person and "name" in person:
        person["full_name"] = person.pop("name")
    if not str(person.get("full_name", "")).strip():
        raise SeedError("people.yaml: every person needs a `full_name`")
    valid_scopes = {s.value for s in AuthorityScope}
    for token in person.get("authority_scope", []) or []:
        if token not in valid_scopes:
            raise SeedError(
                f"people.yaml: `{person['full_name']}` has unknown authority scope `{token}` "
                f"(valid: {', '.join(sorted(valid_scopes))})"
            )
    unknown = set(person) - set(_PERSON_CREATE_FIELDS) - {"reports_to"}
    if unknown:
        raise SeedError(
            f"people.yaml: `{person['full_name']}` has unsupported keys: {', '.join(sorted(unknown))}"
        )
    return person


def _normalize_department(row: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise SeedError("departments.yaml: each department must be a mapping")
    dept = dict(row)
    if not str(dept.get("slug", "")).strip():
        raise SeedError("departments.yaml: every department needs a `slug`")
    if not str(dept.get("title", "")).strip():
        raise SeedError(f"departments.yaml: `{dept['slug']}` needs a `title`")
    goals: list[dict[str, Any]] = []
    for raw_goal in (dept.pop("goals", None) or dept.pop("okrs", None) or []):
        goal = dict(raw_goal)
        if "quarter" in goal:  # legacy fixture key
            goal.setdefault("period_value", goal.pop("quarter"))
            goal.setdefault("period_type", "quarter")
        goal.setdefault("period_type", "quarter")
        for required in ("period_value", "key_result", "target"):
            if not str(goal.get(required, "")).strip():
                raise SeedError(
                    f"departments.yaml: a goal in `{dept['slug']}` is missing `{required}`"
                )
        goals.append({k: goal[k] for k in _GOAL_FIELDS if k in goal})
    dept.pop("okrs", None)
    dept["goals"] = goals
    return dept


# --------------------------------------------------------------------------- #
# HTTP client (dry-run aware)
# --------------------------------------------------------------------------- #

class ApiClient:
    """Thin wrapper over httpx that records writes and can withhold them."""

    def __init__(self, base_url: str, api_key: str | None, *, dry_run: bool, transport: httpx.BaseTransport | None = None):
        headers = {"x-api-key": api_key} if api_key else {}
        self._client = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=_HTTP_TIMEOUT_S, transport=transport)
        self.dry_run = dry_run
        self.planned_writes: list[str] = []

    def close(self) -> None:
        self._client.close()

    def get(self, path: str, **kwargs: Any) -> Any:
        return self._check(self._client.get(path, **kwargs)).json()

    def write(self, method: str, path: str, *, describe: str, **kwargs: Any) -> Any:
        """Send a mutating request unless dry-run; returns parsed JSON or None."""
        self.planned_writes.append(f"{method} {path}  — {describe}")
        if self.dry_run:
            return None
        resp = self._check(self._client.request(method, path, **kwargs))
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    @staticmethod
    def _check(resp: httpx.Response) -> httpx.Response:
        if resp.status_code >= 400:
            detail = resp.text[:_ERROR_DETAIL_MAX_CHARS]
            hint = ""
            if resp.status_code == 401:
                hint = " (is BACKEND_SHARED_SECRET exported and correct for this API?)"
            raise SeedError(f"{resp.request.method} {resp.request.url.path} → {resp.status_code}{hint}: {detail}")
        return resp


# --------------------------------------------------------------------------- #
# Apply
# --------------------------------------------------------------------------- #

@dataclass
class SeedReport:
    profile: str = "skipped"
    people_created: list[str] = field(default_factory=list)
    people_updated: list[str] = field(default_factory=list)
    departments_created: list[str] = field(default_factory=list)
    departments_updated: list[str] = field(default_factory=list)
    departments_deleted: list[str] = field(default_factory=list)
    goals_created: int = 0
    goals_updated: int = 0
    docs_uploaded: list[str] = field(default_factory=list)
    docs_skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def apply_seed(
    bundle: SeedBundle,
    api: ApiClient,
    *,
    prune_departments: bool = False,
    reindex_docs: bool = False,
    allow_email_change: bool = False,
) -> SeedReport:
    report = SeedReport()
    if prune_departments and not bundle.departments:
        raise SeedError("--prune-departments needs a departments.yaml — refusing to delete every department")
    if bundle.profile is not None:
        api.write("PUT", "/company-profile", describe=f"profile `{bundle.profile['name']}`", json=bundle.profile)
        report.profile = f"replaced ({bundle.profile['name']})"

    slug_map = _ensure_departments(bundle.departments, api, report)
    people_ids = _apply_people(bundle.people, api, report, slug_map, allow_email_change=allow_email_change)
    _apply_department_details(bundle.departments, api, report, slug_map, people_ids)
    if prune_departments:
        _prune_departments(api, report, slug_map)
    _apply_docs(bundle.docs, api, report, reindex=reindex_docs)
    return report


def _ensure_departments(departments: list[dict[str, Any]], api: ApiClient, report: SeedReport) -> dict[str, str]:
    """First pass: make every department exist. Returns yaml-slug → server-slug."""
    states = api.get("/departments")
    existing = {d["config"]["slug"] for d in states}
    by_title = {d["config"]["title"].strip().lower(): d["config"]["slug"] for d in states}
    slug_map: dict[str, str] = {}
    for dept in departments:
        yaml_slug = dept["slug"]
        # A department created on an earlier run lives under the server-derived
        # slug (or was renamed to this title); match those before creating, or
        # every re-run would mint a `-2` duplicate.
        match = next(
            (
                candidate
                for candidate in (yaml_slug, derived_slug(dept["title"]), by_title.get(dept["title"].strip().lower()))
                if candidate and candidate in existing
            ),
            None,
        )
        if match is not None:
            _claim_slug(slug_map, yaml_slug, match)
            continue
        mission = (dept.get("charter") or {}).get("mission", "")
        created = api.write(
            "POST",
            "/departments",
            describe=f"create department `{dept['title']}`",
            json={"title": dept["title"], "mission": mission},
        )
        actual = created["config"]["slug"] if created else derived_slug(dept["title"])
        if actual != yaml_slug:
            report.warnings.append(
                f"department `{yaml_slug}` was created as `{actual}` (server derives slugs from titles); "
                "people.department_slugs were remapped"
            )
        _claim_slug(slug_map, yaml_slug, actual)
        existing.add(actual)
        report.departments_created.append(actual)
    return slug_map


def _claim_slug(slug_map: dict[str, str], yaml_slug: str, server_slug: str) -> None:
    """Record yaml→server slug, refusing to let two YAML departments land on one server row
    (e.g. titles "R&D" and "R!D" both derive to `r-d`)."""
    taken = next((y for y, srv in slug_map.items() if srv == server_slug), None)
    if taken is not None:
        raise SeedError(
            f"departments.yaml: `{yaml_slug}` and `{taken}` both resolve to server department `{server_slug}`; "
            "give them distinct titles/slugs"
        )
    slug_map[yaml_slug] = server_slug


def _person_key_index(rows: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    by_email = {p["email"].lower(): p for p in rows if p.get("email")}
    by_name = {p["full_name"].strip().lower(): p for p in rows if p.get("full_name")}
    return by_email, by_name


def _apply_people(
    people: list[dict[str, Any]],
    api: ApiClient,
    report: SeedReport,
    slug_map: dict[str, str],
    *,
    allow_email_change: bool = False,
) -> dict[str, int]:
    """Upsert people; returns lower-cased full name → person id (ids unknown in dry-run are omitted)."""
    existing_rows = api.get("/people")
    by_email, by_name = _person_key_index(existing_rows)
    names = [row["full_name"].strip().lower() for row in existing_rows if row.get("full_name")]
    for duplicate in sorted({n for n in names if names.count(n) > 1}):
        report.warnings.append(
            f"server has more than one person named `{duplicate}`; name matching is ambiguous — give them emails"
        )
    # Seed the name→id map with everyone already on the server so `reports_to`
    # can point at a manager who is not part of this (possibly partial) seed.
    ids: dict[str, int] = {key: int(row["id"]) for key, row in by_name.items()}
    for index, person in enumerate(people, start=1):
        name_key = person["full_name"].strip().lower()
        email_key = (person.get("email") or "").lower()
        match = by_email.get(email_key) if email_key else None
        if match is None:
            match = by_name.get(name_key)
            _guard_email_change(person, match, allow_email_change)
        person_id = _upsert_person(person, match, api, report, slug_map)
        # In dry-run a create returns no id; use a negative placeholder so the
        # dependent reports_to PATCH still shows up in the planned writes.
        ids[name_key] = person_id if person_id is not None else -index
    _link_reports_to(people, ids, api, report)
    return ids


def _guard_email_change(person: dict[str, Any], match: dict[str, Any] | None, allow: bool) -> None:
    """A name-only match that would change an existing email is refused by default.

    A Person's email is the sign-in allowlist and gates channel access, so a
    namesake in a shared people.yaml must not be able to hand someone's login
    (and authority scopes) to a different address without the operator opting in.
    """
    if match is None or allow:
        return
    current = (match.get("email") or "").lower()
    wanted = (person.get("email") or "").lower()
    if current and wanted and current != wanted:
        raise SeedError(
            f"`{person['full_name']}` matched by name but the server has email `{match['email']}` "
            f"and people.yaml says `{person['email']}`. Re-run with --allow-email-change if this is "
            "the same person switching addresses."
        )


def _upsert_person(
    person: dict[str, Any],
    match: dict[str, Any] | None,
    api: ApiClient,
    report: SeedReport,
    slug_map: dict[str, str],
) -> int | None:
    """POST a new person or PATCH the matched one. Returns the server id when known."""
    payload = {k: person[k] for k in _PERSON_CREATE_FIELDS if k in person}
    if "department_slugs" in payload:
        payload["department_slugs"] = [slug_map.get(s, s) for s in payload["department_slugs"]]

    if match is None:
        created = api.write("POST", "/people", describe=f"create person `{person['full_name']}`", json=payload)
        report.people_created.append(person["full_name"])
        return created["id"] if created else None

    if "is_principal" in person and bool(person["is_principal"]) != bool(match.get("is_principal")):
        report.warnings.append(
            f"`{person['full_name']}`: is_principal differs from the server and cannot be changed via PATCH; "
            "archive and recreate the person to change it"
        )
    patch = {k: v for k, v in payload.items() if k in _PERSON_PATCH_FIELDS}
    api.write("PATCH", f"/people/{match['id']}", describe=f"update person `{person['full_name']}`", json=patch)
    report.people_updated.append(person["full_name"])
    return int(match["id"])


def _link_reports_to(
    people: list[dict[str, Any]],
    ids: dict[str, int],
    api: ApiClient,
    report: SeedReport,
) -> None:
    """Second pass: resolve `reports_to: <full name>` now that every person has an id."""
    for person in people:
        if not person.get("reports_to"):
            continue
        name_key = person["full_name"].strip().lower()
        manager_key = str(person["reports_to"]).strip().lower()
        person_id, manager_id = ids.get(name_key), ids.get(manager_key)
        if person_id is None or manager_id is None:
            report.warnings.append(
                f"`{person['full_name']}`: reports_to `{person['reports_to']}` matches nobody in people.yaml or on the server"
            )
            continue
        api.write(
            "PATCH",
            f"/people/{person_id}",
            describe=f"set reports_to for `{person['full_name']}` → `{person['reports_to']}`",
            json={"reports_to_person_id": manager_id},
        )


def _apply_department_details(
    departments: list[dict[str, Any]],
    api: ApiClient,
    report: SeedReport,
    slug_map: dict[str, str],
    people_ids: dict[str, int],
) -> None:
    states = {d["config"]["slug"]: d for d in api.get("/departments")}
    for dept in departments:
        slug = slug_map[dept["slug"]]
        state = states.get(slug)
        patch: dict[str, Any] = {k: dept[k] for k in _DEPARTMENT_PATCH_FIELDS if k in dept}
        if "charter" in dept:
            charter = dict(dept["charter"])
            charter.setdefault("scope", [])
            charter.setdefault("out_of_scope", [])
            patch["charter"] = charter
        head_name = dept.get("head_person_name")
        if head_name:
            head_id = people_ids.get(str(head_name).strip().lower())
            if head_id is not None:
                patch["head_person_id"] = head_id
            else:
                report.warnings.append(f"department `{slug}`: head_person_name `{head_name}` not found in people")
        if patch:
            api.write("PATCH", f"/departments/{slug}", describe=f"update department `{slug}`", json=patch)
        if slug not in report.departments_created:
            report.departments_updated.append(slug)

        _apply_goals(slug, dept["goals"], (state or {}).get("goals", []), api, report)


def _apply_goals(
    slug: str,
    wanted: list[dict[str, Any]],
    existing: list[dict[str, Any]],
    api: ApiClient,
    report: SeedReport,
) -> None:
    """Create missing goals and PATCH only the fields that differ on matched ones."""
    goal_index = {(g["period_value"], g["key_result"]): g for g in existing}
    for goal in wanted:
        label = goal["key_result"][:_GOAL_LABEL_MAX_CHARS]
        current = goal_index.get((goal["period_value"], goal["key_result"]))
        if current is None:
            api.write("POST", f"/departments/{slug}/goals", describe=f"create goal `{label}`", json=goal)
            report.goals_created += 1
            continue
        diff = {k: v for k, v in goal.items() if current.get(k) != v}
        if diff:
            api.write("PATCH", f"/departments/{slug}/goals/{current['id']}", describe=f"update goal `{label}`", json=diff)
            report.goals_updated += 1


def _prune_departments(api: ApiClient, report: SeedReport, slug_map: dict[str, str]) -> None:
    keep = set(slug_map.values())
    for state in api.get("/departments"):
        slug = state["config"]["slug"]
        if slug in keep:
            continue
        api.write("DELETE", f"/departments/{slug}", describe=f"delete department `{slug}` (not in seed)")
        report.departments_deleted.append(slug)


def _apply_docs(docs: list[DocSpec], api: ApiClient, report: SeedReport, *, reindex: bool) -> None:
    if not docs:
        return
    existing = {d["filename"] for d in api.get("/documents").get("documents", [])}
    for spec in docs:
        name = spec.path.name
        if name in existing and not reindex:
            report.docs_skipped.append(name)
            continue
        if name in existing:
            # Percent-encode: `#` or `?` in a filename would otherwise be parsed
            # as a fragment/query and DELETE a different document.
            api.write("DELETE", f"/documents/{quote(name, safe='')}", describe=f"remove `{name}` before re-upload")
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        api.write(
            "POST",
            "/documents",
            describe=f"upload `{name}` (domain={spec.domain})",
            files={"file": (name, _read_doc_bytes(spec), mime)},
            data={"domain": spec.domain},
        )
        report.docs_uploaded.append(name)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def render_report(report: SeedReport, api: ApiClient) -> None:
    table = Table(title="seed-org summary" + (" (dry run — nothing sent)" if api.dry_run else ""))
    table.add_column("Item")
    table.add_column("Result")
    table.add_row("profile", report.profile)
    table.add_row("people created", ", ".join(report.people_created) or "—")
    table.add_row("people updated", ", ".join(report.people_updated) or "—")
    table.add_row("departments created", ", ".join(report.departments_created) or "—")
    table.add_row("departments updated", ", ".join(report.departments_updated) or "—")
    table.add_row("departments deleted", ", ".join(report.departments_deleted) or "—")
    table.add_row("goals", f"{report.goals_created} created, {report.goals_updated} updated")
    table.add_row("docs uploaded", ", ".join(report.docs_uploaded) or "—")
    table.add_row("docs skipped (already present)", ", ".join(report.docs_skipped) or "—")
    console.print(table)
    if api.dry_run:
        console.print("[bold]Planned writes:[/bold]")
        for line in api.planned_writes:
            console.print(f"  {line}")
    for warning in report.warnings:
        console.print(f"[yellow]warning:[/yellow] {warning}")


def _is_cleartext_remote(api_url: str) -> bool:
    parts = urlsplit(api_url)
    return parts.scheme == "http" and (parts.hostname or "") not in {"localhost", "127.0.0.1", "::1"}


@click.command("seed-org")
@click.option("--api", "api_url", required=True, help="Base URL of the Open Executive API, e.g. http://localhost:8000")
@click.option(
    "--dir",
    "seed_dir",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Directory holding profile.yaml / people.yaml / departments.yaml / docs/.",
)
@click.option("--prune-departments", is_flag=True, help="Delete departments on the server that are not in departments.yaml.")
@click.option("--reindex-docs", is_flag=True, help="Delete and re-upload docs that already exist on the server.")
@click.option(
    "--allow-email-change",
    is_flag=True,
    help="Let a person matched by name switch to the email in people.yaml (changes their sign-in identity).",
)
@click.option("--dry-run", is_flag=True, help="Show every write that would be sent, send none.")
def seed_org(
    api_url: str,
    seed_dir: Path,
    prune_departments: bool,
    reindex_docs: bool,
    allow_email_change: bool,
    dry_run: bool,
) -> None:
    """Upsert a company's profile, people, departments, goals, and docs from DIR.

    Authenticates with the BACKEND_SHARED_SECRET environment variable (sent as
    the x-api-key header). Safe to re-run: existing rows are matched and patched.
    """
    api_key = os.environ.get("BACKEND_SHARED_SECRET")
    if not api_key:
        console.print("[yellow]BACKEND_SHARED_SECRET is not set — sending unauthenticated requests (fine for `make dev`).[/yellow]")
    elif _is_cleartext_remote(api_url):
        console.print(
            "[yellow]warning:[/yellow] --api uses plain http to a non-local host — BACKEND_SHARED_SECRET "
            "will be sent in cleartext. Use https:// for deployed instances."
        )
    bundle = load_seed_dir(seed_dir)
    api = ApiClient(api_url, api_key, dry_run=dry_run)
    try:
        report = apply_seed(
            bundle,
            api,
            prune_departments=prune_departments,
            reindex_docs=reindex_docs,
            allow_email_change=allow_email_change,
        )
    finally:
        api.close()
    render_report(report, api)
