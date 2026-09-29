"""Deterministic grounding of unattended prose (briefing/grounding.py).

The checker must catch a fabricated colleague and a wrong figure, and must
not trip on the brief's own section headers, dates, times, quarters or small
counts — a false positive drops a real line from the principal's brief.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from openexecutive.briefing import grounding as g
from openexecutive.briefing.grounding import (
    HELD_BACK_ALL,
    SOURCES_HEADER,
    GroundingScope,
    GroundingSource,
    check_text,
    extract_figures,
    extract_names,
    ground_alert_text,
    ground_brief,
    sources_from_context,
    sources_from_text,
)

CONTEXT = """PERIOD: 2026-09-29

INBOUND SINCE THE LAST BRIEF (3 messages, newest first, local times; quoted):
- 08:14 email from Dana Whitfield <dana.whitfield@acme.com>: Lease renewal for 48 units due Friday
- 07:50 email from priya@x.com: Budget $1,234,567 approved
- 07:10 slack from Tom Ruiz: can we sync?

DEPARTMENTS WITH RISK:
- Sales: at_risk=2 off_track=1 awaiting=0
  • Q3 pipeline (at_risk: 1.2M of 2M)
CARRIED OVER: 12 older item(s) still open (oldest 9d) — see /today
"""


@pytest.fixture
def sources() -> list[GroundingSource]:
    return sources_from_context(CONTEXT)


@pytest.fixture(autouse=True)
def _no_company_data(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, Any]]]:
    """Grounding reads the profile, departments and roster; keep them empty
    and capture audit rows instead of writing them."""
    monkeypatch.setattr(g, "profile_sources", lambda: [])
    monkeypatch.setattr(g, "org_sources", lambda: [])
    monkeypatch.setattr(g, "roster", lambda: ["Dana Whitfield", "Tom Ruiz"])
    monkeypatch.setattr(g, "grounding_mode", lambda: "enforce")
    monkeypatch.setattr(g, "citations_enabled", lambda: True)
    rows: list[tuple[str, str, dict[str, Any]]] = []

    def _log(event_type: str, summary: str, **kw: Any) -> None:
        rows.append((event_type, summary, kw))

    monkeypatch.setattr("openexecutive.audit.log_event", _log)
    return rows


def _bad(text: str, sources: list[GroundingSource], roster: list[str] | None = None) -> list[str]:
    report = check_text(text, sources, roster or [])
    return report.names_bad + report.figures_bad


# --------------------------------------------------------------------------- #
# Figures
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text",
    [
        "Board meets 2026-10-02 at 9:30",
        "Call at 10am, then 3pm",
        "Due Sep 29 or 29th September",
        "Q3 plan, FY26 budget, H1 review",
        "3 of 5 goals on track; top 3 today",
        "1. First item",
        "Waiting 14 days, 3 weeks overdue",
        "Alert #42 is open",
        "Plan for 2027",
        "See [1] above",
    ],
)
def test_figure_exemptions(text: str) -> None:
    assert extract_figures(text) == []


@pytest.mark.parametrize(
    ("text", "value", "kind"),
    [
        ("Burn is $1.4M", 1_400_000, "money"),
        ("Budget of $1,234,567", 1_234_567, "money"),
        ("Churn is 12%", 12, "pct"),
        ("12.5 percent of revenue", 12.5, "pct"),
        ("48 units renewing", 48, "count"),
        ("About 10k signups", 10_000, "count"),
        ("£3 billion market", 3e9, "money"),
    ],
)
def test_figures_extracted(text: str, value: float, kind: str) -> None:
    [fig] = extract_figures(text)
    assert fig.value == pytest.approx(value)
    assert fig.kind == kind


def test_figure_matches_within_shown_precision(sources: list[GroundingSource]) -> None:
    assert _bad("Budget of $1.2M approved", sources) == []
    assert _bad("Budget of $1.3M approved", sources) == ["$1.3M"]
    assert _bad("Budget of $1,234,567 approved", sources) == []
    assert _bad("Budget of $1,234,000 approved", sources) == ["$1,234,000"]


def test_wrong_unit_count_is_caught(sources: list[GroundingSource]) -> None:
    assert _bad("Lease renewal covers 48 units", sources) == []
    assert _bad("Lease renewal covers 47 units", sources) == ["47"]


def test_percent_from_two_numbers_on_one_line(sources: list[GroundingSource]) -> None:
    # 1.2M of 2M is 60%; 18% is nowhere.
    assert _bad("Pipeline at 60% of target", sources) == []
    assert _bad("Revenue up 18% this quarter", sources) == ["18%"]


def test_section_bullet_count_grounds_a_count() -> None:
    ctx = "PEOPLE WAITING ON YOU:\n" + "\n".join(f"- Person {i} (x)" for i in range(12))
    assert _bad("12 people are waiting on you", sources_from_context(ctx)) == []
    assert _bad("13 people are waiting on you", sources_from_context(ctx)) == ["13"]


def test_profile_figures_ground() -> None:
    profile = sources_from_text("**ARR**: $90,000,000\n  Runway: 14.0 months", "Company profile", "p")
    assert _bad("ARR is $90M", profile) == []
    assert _bad("ARR is $95M", profile) == ["$95M"]


# --------------------------------------------------------------------------- #
# Names
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text",
    [
        "**Top call** — Approve the lease renewal.",
        "**Top Three Today**",
        "**Needs you**",
        "**What changed**",
        "Monday is packed; OKR review in Q3.",
        "Take it to the Board via Slack.",
        "Burn trending high.",
        "**Sleep on this** — is the Acme deal worth it?",
        "Everyone needs the deck by Friday.",
    ],
)
def test_no_name_candidates_in_brief_furniture(text: str) -> None:
    assert extract_names(text) == []


@pytest.mark.parametrize(
    ("text", "names"),
    [
        ("Marcus Lee flagged the lease", ["Marcus Lee"]),
        ("ask Marcus to sign", ["Marcus"]),
        ("Marcus's reply is overdue", ["Marcus"]),
        ("Marcus wrote about the lease", ["Marcus"]),
        ("Loop in Sarah before noon", ["Sarah"]),
        ("Mary-Jane O'Brien signed", ["Mary-Jane O'Brien"]),
        ("Ask Ludwig van Beethoven", ["Ludwig van Beethoven"]),
    ],
)
def test_person_names_found(text: str, names: list[str]) -> None:
    assert extract_names(text) == names


def test_fabricated_colleague_is_caught(sources: list[GroundingSource]) -> None:
    assert _bad("Marcus Lee flagged the lease — ask Marcus to sign.", sources) == [
        "Marcus Lee", "Marcus",
    ]


def test_real_people_pass_in_any_form(sources: list[GroundingSource]) -> None:
    assert _bad("Sign Dana's lease renewal", sources) == []
    assert _bad("Dana Whitfield wrote about the lease", sources) == []
    assert _bad("ask Tom to sync", sources) == []


def test_names_cannot_be_spliced_across_lines(sources: list[GroundingSource]) -> None:
    # "Dana" and "Ruiz" both appear — on different lines.
    assert _bad("Dana Ruiz wrote about the lease", sources) == ["Dana Ruiz"]


def test_email_local_part_grounds_a_name() -> None:
    src = sources_from_text("email from dana.whitfield@acme.com: lease", "Inbound", "i")
    assert _bad("Dana Whitfield wrote", src) == []


def test_roster_person_mentioned_by_first_name_grounds_full_name() -> None:
    src = sources_from_text("slack from Dana: lease signed", "Inbound", "i")
    assert _bad("Dana Whitfield signed the lease", src, ["Dana Whitfield"]) == []
    # Being on the roster alone is not enough.
    assert _bad("Tom Ruiz signed the lease", src, ["Dana Whitfield", "Tom Ruiz"]) == ["Tom Ruiz"]


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #


def test_sources_are_labelled_by_section(sources: list[GroundingSource]) -> None:
    labels = {s.label for s in sources}
    assert "Inbound since the last brief" in labels
    assert "Departments with risk" in labels
    assert "Carried over" in labels
    lease = next(s for s in sources if "Lease" in s.text)
    assert lease.label == "Inbound since the last brief"
    assert not lease.text.startswith("-")


# --------------------------------------------------------------------------- #
# ground_brief
# --------------------------------------------------------------------------- #

BRIEF = """**Top call** — Sign Dana's lease renewal for 48 units by Friday.

**What changed**
- Budget of $1.2M approved.
- Marcus Lee flagged the lease.

**At risk**
- Revenue up 18% this quarter"""


@pytest.mark.asyncio
async def test_ground_brief_drops_cites_and_notes(
    _no_company_data: list[tuple[str, str, dict[str, Any]]],
) -> None:
    out, result = await ground_brief(BRIEF, context=CONTEXT, kind="k", private=True)
    assert result is not None
    assert "Marcus" not in out and "18%" not in out
    # The emptied "At risk" header goes too.
    assert "**At risk**" not in out
    assert "48 [1] units" in out
    assert "$1.2M [2] approved" in out
    assert "_Held back 2 lines" in out
    body, footer = out.split(SOURCES_HEADER)
    assert "Held back" in body
    assert "- [1] Inbound since the last brief — " in footer
    assert "- [2] Inbound since the last brief — " in footer and "Budget $1,234,567" in footer
    # Never "[n]:" at a line start (a Markdown link reference definition).
    assert not any(ln.lstrip().startswith("[") for ln in out.splitlines())
    [(event_type, _summary, kw)] = _no_company_data
    assert event_type == "grounding"
    assert kw["private"] is True
    assert kw["details"]["names_bad"] == ["Marcus Lee"]
    assert kw["details"]["figures_bad"] == ["18%"]
    assert len(kw["details"]["held_back"]) == 2


@pytest.mark.asyncio
async def test_clean_text_without_figures_is_byte_identical(
    _no_company_data: list[tuple[str, str, dict[str, Any]]],
) -> None:
    text = "**Top call** — Sign Dana's lease renewal by Friday."
    out, _ = await ground_brief(text, context=CONTEXT, kind="k")
    assert out == text
    assert _no_company_data == []


@pytest.mark.asyncio
async def test_everything_ungrounded_is_held_back_whole() -> None:
    out, result = await ground_brief(
        "**Needs you**\n- Marcus Lee wants $9M", context=CONTEXT, kind="k",
    )
    assert out == HELD_BACK_ALL
    assert result is not None and len(result.held) == 1


@pytest.mark.asyncio
async def test_report_mode_leaves_text_and_audits(
    monkeypatch: pytest.MonkeyPatch,
    _no_company_data: list[tuple[str, str, dict[str, Any]]],
) -> None:
    monkeypatch.setattr(g, "grounding_mode", lambda: "report")
    out, result = await ground_brief(BRIEF, context=CONTEXT, kind="k")
    assert out == BRIEF
    assert result is not None and len(result.held) == 2
    assert _no_company_data and _no_company_data[0][2]["details"]["mode"] == "report"


@pytest.mark.asyncio
async def test_off_mode_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(g, "grounding_mode", lambda: "off")
    assert await ground_brief(BRIEF, context=CONTEXT, kind="k") == (BRIEF, None)


@pytest.mark.asyncio
async def test_citations_can_be_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(g, "citations_enabled", lambda: False)
    text = "**Top call** — Sign the renewal for 48 units."
    out, _ = await ground_brief(text, context=CONTEXT, kind="k")
    assert out == text


@pytest.mark.asyncio
async def test_citations_cap_and_sanitise() -> None:
    lines = [f"- ITEM{i}: [x](javascript:y) <b>{100 + i}</b> things" for i in range(12)]
    ctx = "STUFF:\n" + "\n".join(lines)
    brief = "\n".join(f"- {100 + i} things" for i in range(12))
    out, _ = await ground_brief(brief, context=ctx, kind="k")
    footer = out.split(SOURCES_HEADER)[1]
    assert footer.count("\n- [") == 9
    assert "[x]" not in footer and "<b>" not in footer and "(javascript" not in footer
    assert "- 111 things" in out  # the 12th figure: over the cap, left unmarked


@pytest.mark.asyncio
async def test_repair_call_used_when_it_grounds(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts: list[str] = []

    async def _repair(draft: str, context: str, system: str, report: Any) -> str:
        prompts.append(report.summary())
        return draft.replace("- Marcus Lee flagged the lease.\n", "").replace(
            "- Revenue up 18% this quarter", "- Sales pipeline at risk"
        )

    monkeypatch.setattr(g, "_repair", _repair)
    out, result = await ground_brief(BRIEF, context=CONTEXT, kind="k", system="SYS")
    assert prompts == ["'Marcus Lee', '18%'"]
    assert result is not None and result.repaired and result.held == []
    assert "Held back" not in out
    assert "Sales pipeline at risk" in out


@pytest.mark.asyncio
async def test_repair_failure_falls_back_to_dropping(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(*_a: Any, **_k: Any) -> str:
        raise RuntimeError("provider down")

    monkeypatch.setattr(g, "_repair", _boom)
    out, result = await ground_brief(BRIEF, context=CONTEXT, kind="k", system="SYS")
    assert result is not None and not result.repaired and len(result.held) == 2
    assert "Marcus" not in out


@pytest.mark.asyncio
async def test_internal_error_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("bug")

    monkeypatch.setattr(g, "check_text", _boom)
    assert await ground_brief(BRIEF, context=CONTEXT, kind="k") == (BRIEF, None)


# --------------------------------------------------------------------------- #
# GroundingScope (unattended tool loops)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_scope_refuses_ungrounded_outward_prose(
    sources: list[GroundingSource],
    _no_company_data: list[tuple[str, str, dict[str, Any]]],
) -> None:
    calls: list[dict[str, Any]] = []

    async def _send(tool_input: dict[str, Any]) -> str:
        calls.append(tool_input)
        return json.dumps({"ok": True})

    scope = GroundingScope(sources, [], surface="test")
    handlers = scope.guard({"create_alert": _send, "send_company_broadcast": _send})
    refused = json.loads(await handlers["create_alert"](
        {"subject": "Lease", "body": "Marcus Lee says the renewal is 52 units."}
    ))
    assert "error" in refused and "'Marcus Lee'" in refused["error"] and "'52'" in refused["error"]
    assert calls == []
    ok = await handlers["send_company_broadcast"]({"text": "Dana's renewal covers 48 units."})
    assert json.loads(ok) == {"ok": True}
    assert len(calls) == 1
    assert _no_company_data[0][2]["details"]["tool"] == "create_alert"


@pytest.mark.asyncio
async def test_tool_results_ground_later_prose(sources: list[GroundingSource]) -> None:
    async def _lookup(_tool_input: dict[str, Any]) -> str:
        return json.dumps({"person_id": 7, "full_name": "Marcus Lee", "role": "Property manager"})

    async def _send(_tool_input: dict[str, Any]) -> str:
        return json.dumps({"ok": True})

    scope = GroundingScope(sources, [], surface="test")
    handlers = scope.guard({"lookup_person": _lookup, "message_person": _send})
    msg = {"person_id": 7, "text": "Marcus Lee: Dana needs the lease checked."}
    assert "error" in json.loads(await handlers["message_person"](msg))
    await handlers["lookup_person"]({"query": "property manager"})
    assert json.loads(await handlers["message_person"](msg)) == {"ok": True}


@pytest.mark.asyncio
async def test_non_outward_tools_and_report_mode_are_not_refused(
    monkeypatch: pytest.MonkeyPatch, sources: list[GroundingSource],
) -> None:
    async def _ok(_tool_input: dict[str, Any]) -> str:
        return "{}"

    scope = GroundingScope(sources, [], surface="test")
    assert await scope.guard({"lookup_person": _ok})["lookup_person"]({"query": "Marcus Lee"}) == "{}"
    monkeypatch.setattr(g, "grounding_mode", lambda: "report")
    scope = GroundingScope(sources, [], surface="test")
    assert await scope.guard({"create_alert": _ok})["create_alert"](
        {"subject": "x", "body": "Marcus Lee"}
    ) == "{}"


def test_filter_section_drops_ungrounded_flags(sources: list[GroundingSource]) -> None:
    text = (
        "**Acted on:**\n- Marcus Lee pinged\n\n"
        "**Flagged for the brief:**\n- Dana's renewal: 48 units\n- Marcus Lee wants a call\n\n"
        "**Quiet:** nothing else.\n\n---\n_Tool calls this run:_"
    )
    scope = GroundingScope(sources, [], surface="test")
    out, held = scope.filter_section(text, "Flagged for the brief")
    assert held == ["- Marcus Lee wants a call"]
    assert "Dana's renewal" in out
    # Only the flags section is filtered.
    assert "- Marcus Lee pinged" in out
    assert "**Quiet:**" in out


def test_filter_section_numbers_lines_like_check_text(sources: list[GroundingSource]) -> None:
    """A lone \\r inside a flag must not shift which line is dropped."""
    text = (
        "**Flagged for the brief:**\n- Dana's renewal:\r48 units\n"
        "- Marcus Lee wants a call\n- Nothing else pressing\n\n**Quiet:** done."
    )
    scope = GroundingScope(sources, [], surface="test")
    out, held = scope.filter_section(text, "Flagged for the brief")
    assert held == ["- Marcus Lee wants a call"]
    assert "Marcus" not in out
    assert "- Dana's renewal:\r48 units\n" in out and "- Nothing else pressing" in out


# --------------------------------------------------------------------------- #
# Alert text
# --------------------------------------------------------------------------- #


def test_alert_text_falls_back_to_the_event() -> None:
    src = sources_from_text("Lease renewal\nDana Whitfield: renewal for 48 units due Friday", "Event", "e")
    headline, body, action, changed = ground_alert_text(
        "Marcus Lee: lease renewal", "Dana asks to renew 48 units. Marcus Lee manages it.",
        "Call Marcus Lee",
        sources=src, fallback_headline="Lease renewal",
        fallback_body="Dana Whitfield: renewal for 48 units due Friday", surface="t",
    )
    assert changed
    assert headline == "Lease renewal"
    assert body == "Dana asks to renew 48 units."
    assert action == ""


def test_alert_text_unchanged_when_grounded() -> None:
    src = sources_from_text("Dana Whitfield: renewal for 48 units", "Event", "e")
    assert ground_alert_text(
        "Dana: renewal", "Renewal for 48 units.", "Reply to Dana",
        sources=src, fallback_headline="x", fallback_body="y", surface="t",
    ) == ("Dana: renewal", "Renewal for 48 units.", "Reply to Dana", False)


def test_grounding_is_a_declared_audit_event_type() -> None:
    from openexecutive.audit.logger import EVENT_TYPES

    assert "grounding" in EVENT_TYPES


def test_raw_event_fallbacks_are_made_inert() -> None:
    """The fallback is attacker-written event text a broadcast posts as is:
    no live link, no Slack <!channel>, no @everyone."""
    src = sources_from_text("Revenue fell by forty-two million", "Event", "e")
    headline, body, _action, changed = ground_alert_text(
        "Revenue fell $42M", "Revenue fell $42M.", "",
        sources=src,
        fallback_headline="Revenue fell <!channel> see https://evil.example/x @everyone",
        fallback_body="<https://evil.example/pay|Reset password> now @here www.evil.example",
        surface="t",
    )
    assert changed
    for text in (headline, body):
        assert "evil.example" not in text and "<" not in text and "|" not in text
        assert "@everyone" not in text and "@here" not in text
    assert "(link removed)" in headline


@pytest.mark.asyncio
async def test_citation_snippets_mask_links_and_mentions() -> None:
    ctx = (
        "INBOUND SINCE THE LAST BRIEF (1 message):\n"
        "- 08:14 email from Al <al@b.co>: 48 units, pay at https://evil.example/pay @everyone"
    )
    out, _ = await ground_brief("- Renewal covers 48 units", context=ctx, kind="k")
    footer = out.split(SOURCES_HEADER)[1]
    assert "evil.example" not in footer and "@everyone" not in footer
    assert "(link removed)" in footer and "al@b.co" in footer
