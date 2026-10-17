"""What a finished run did, in plain words, for the activity feed.

A run's `title` is a log label ("Executive Research (periodic)", "Check-in:
sales 2026-W42") and its artifact is the full write-up. Pulse's Done for you
and All activity need neither: they need a short name for the work and one
line on what came of it. Both are read from the run's stored row, so older
runs read the same way as new ones and nothing extra is written or called.

The line is built only from the stored artifact, which the run's viewers can
already open in full (`artifact_records`). A brief gets no line (its text is
the brief itself; a private one is stored as `PRIVATE_RUN_ARTIFACT`). The
reflection gets neither a line nor a link: it reads the principal's live
alerts and its write-up is not screened for what is private to them.
"""
from __future__ import annotations

import re
from typing import Any

from openexecutive.workflows.persistence import PRIVATE_RUN_ARTIFACT

# The plain name each scheduled or system workflow goes by in the feed.
_NAMES: dict[str, str] = {
    "executive_research": "Researched your market",
    "morning_brief": "Sent the morning brief",
    "end_of_day_digest": "Sent the end-of-day wrap-up",
    "weekly_review": "Wrote the weekly review",
    "executive_reflection": "Looked back on the day",
}

# Runs whose write-up is the deliverable itself (or private): no line.
_NO_LINE: frozenset[str] = frozenset({
    "morning_brief", "end_of_day_digest", "executive_reflection",
})

# Never linked from the feed (module docstring).
_NO_LINK: frozenset[str] = frozenset({"executive_reflection"})

# Stored when a run finished without a write-up.
_EMPTY_ARTIFACTS: frozenset[str] = frozenset({"", "(no artifact)", PRIVATE_RUN_ARTIFACT})

_MAX_LINE = 160

# "Morning Brief 2026-10-17", "Executive Research (periodic)".
_TITLE_NOISE = re.compile(r"\s*(\(.*\)|\d{4}-\d{2}-\d{2}|\d{4}-W\d{2})\s*$")


def has_write_up(artifact: str | None) -> bool:
    """Whether the run kept a write-up worth opening."""
    return (artifact or "").strip() not in _EMPTY_ARTIFACTS


def write_up_id(run: dict[str, Any]) -> str | None:
    """The artifact id the feed links as "Read it", or None."""
    if run.get("workflow_name") in _NO_LINK or not has_write_up(run.get("artifact")):
        return None
    return f"run:{run['run_id']}"


def plain_name(run: dict[str, Any]) -> str:
    """The short name of the work a run did."""
    name = run.get("workflow_name") or ""
    if name in _NAMES:
        return _NAMES[name]
    if name == "department_check_in":
        return f"Checked in with {_department_title(run)}"
    # Anything else was named when it started ("Teardown of Acme"): keep
    # that, without the date or the note on how it started.
    title = run.get("title") or ""
    while (cleaned := _TITLE_NOISE.sub("", title)) != title:
        title = cleaned
    if title.strip():
        return title.strip()
    from openexecutive.workflows import WORKFLOW_REGISTRY

    workflow = WORKFLOW_REGISTRY.get(name)
    return _sentence_case(workflow.title) if workflow is not None else "Finished a piece of work"


def outcome_line(run: dict[str, Any]) -> str | None:
    """One line on what came of a run, or None when there is nothing to say."""
    name = run.get("workflow_name") or ""
    artifact = run.get("artifact") or ""
    if name in _NO_LINE or not has_write_up(artifact):
        return None
    if name == "executive_research":
        return _research_line(artifact)
    if name == "department_check_in":
        return _check_in_line(artifact)
    if name == "weekly_review":
        return _weekly_line(artifact) or _first_sentence(artifact)
    return _first_sentence(artifact)


# ---------------------------------------------------------------------------
# Per-workflow lines, read from the Markdown each workflow renders.
# ---------------------------------------------------------------------------


def _research_line(artifact: str) -> str:
    """`executive_research._render_artifact`: findings, then what it did."""
    found = re.search(r"_(\d+) finding\(s\) after dedup", artifact)
    looked = int(found.group(1)) if found else 0
    messages = flagged = other = 0
    for line in _section(artifact, "Actions routed"):
        if not line.startswith("- ✓"):
            continue
        tool = re.search(r"`([^`]+)`", line)
        tool_name = tool.group(1) if tool else ""
        if tool_name == "create_alert":
            flagged += 1
        elif tool_name.startswith("send_") or tool_name.endswith("_dm"):
            messages += 1
        else:
            other += 1
    watching = sum(1 for line in _section(artifact, "Now watching") if line.startswith("- "))
    suggested = sum(
        1 for line in _section(artifact, "Suggested for your approval") if line.startswith("- ")
    )

    did = [
        part for part in (
            _count(messages, "sent {n} message", "sent {n} messages"),
            _count(flagged, "flagged {n} thing for you", "flagged {n} things for you"),
            _count(other, "took {n} other step", "took {n} other steps"),
            _count(watching, "started watching {n} thing", "started watching {n} things"),
            _count(suggested, "suggested {n} thing to watch", "suggested {n} things to watch"),
        ) if part
    ]
    lead = _count(looked, "Looked at {n} update.", "Looked at {n} updates.") or "Found nothing new."
    if not did:
        return lead if not looked else f"{lead} Nothing needed you."
    return f"{lead} {_capitalise(_join(did))}."


def _check_in_line(artifact: str) -> str:
    """`department_check_in._assemble_artifact`: its proposed actions."""
    proposed = sum(1 for line in _section(artifact, "Proposed Actions") if line.startswith("- "))
    if not proposed:
        return "Reviewed the goals. Nothing needed action."
    noun = "action" if proposed == 1 else "actions"
    return f"Reviewed the goals and proposed {proposed} {noun}."


def _weekly_line(artifact: str) -> str | None:
    """`weekly_review`: the first of next week's top three."""
    for line in _section(artifact, "Next week's top 3"):
        first = re.match(r"\d+\.\s+(.*)", line)
        if first:
            what = first.group(1).split(" — ", 1)[0]
            return _clip(f"Top for next week: {_plain(what)}")
    return None


def _first_sentence(artifact: str) -> str | None:
    """The first line of prose in a write-up, without its Markdown."""
    for raw in artifact.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "---", "|", "```")):
            continue
        if line.startswith("_") and line.endswith("_"):
            continue  # an italic note ("_Note: …_"), not the content
        line = re.sub(r"^([-*]|\d+\.)\s+", "", line)
        text = _plain(line)
        if text:
            sentence = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
            return _clip(sentence)
    return None


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _department_title(run: dict[str, Any]) -> str:
    inputs = run.get("inputs") or {}
    slug = inputs.get("department_slug") if isinstance(inputs, dict) else None
    if not slug:
        match = re.match(r"Check-in:\s*(\S+)", run.get("title") or "")
        slug = match.group(1) if match else ""
    if slug:
        try:
            from openexecutive.departments import store as dept_store

            dept = dept_store.get_department(slug)
            if dept is not None and dept.config.title:
                return str(dept.config.title)
        except Exception:  # noqa: BLE001 - a name is cosmetic; fall back to the slug.
            pass
        return slug.replace("_", " ").replace("-", " ").title()
    return "a team"


def _section(artifact: str, heading: str) -> list[str]:
    """The stripped, non-empty lines under a `## heading`, up to the next one."""
    lines: list[str] = []
    inside = False
    for raw in artifact.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            if inside:
                break
            inside = line.lstrip("#").strip().startswith(heading)
            continue
        if inside and line:
            lines.append(line)
    return lines


def _count(n: int, one: str, many: str) -> str:
    if n <= 0:
        return ""
    return (one if n == 1 else many).format(n=n)


def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"


def _plain(text: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    return re.sub(r"[*_`]+", "", text).strip()


def _clip(text: str) -> str:
    if len(text) <= _MAX_LINE:
        return text
    return text[: _MAX_LINE - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def _capitalise(text: str) -> str:
    return text[:1].upper() + text[1:]


def _sentence_case(title: str) -> str:
    """"Board Prep" → "Board prep"; leaves acronyms ("MBR", "GTM") alone."""
    words = title.split()
    return " ".join(
        w if i == 0 or w.isupper() else w.lower() for i, w in enumerate(words)
    )
