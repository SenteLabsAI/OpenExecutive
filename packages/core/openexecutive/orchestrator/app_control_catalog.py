"""What chat can do in the app (``app_control``): chats, topics, documents,
files, alerts, watch suggestions, workflows, memory, departments, skills and
the person's own settings, plus links to screens.

Each action calls the route its button calls (see ``app_control``), so this
file is wiring: which route, what to send it, what to say after. An action
whose route checks nothing about the caller (its page is the principal's) is
``principal_only``. What waits for a tap: deletes, shares, turning on
something that sends as the person or lets more happen on its own, and
approving a workflow's waiting step.

Not here on purpose: the person's notes ("Keep track of what happens") are
private and read only through ``recall_history``, which marks the chat
private first; account, sign-in, billing and company settings stay on their
screens.
"""
from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from openexecutive.orchestrator.app_control import (
    Action,
    Item,
    Kind,
    Refused,
    register_action,
    register_kind,
    register_screen,
    with_body,
)


async def _call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call a route function: await an async one, run a plain one off the
    event loop (FastAPI runs those in its threadpool too)."""
    if inspect.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    return await asyncio.to_thread(fn, *args, **kwargs)


def _q(text: str) -> str:
    return f"“{text}”" if text else "it"


def _on(value: str) -> bool:
    v = value.strip().lower()
    if v in ("on", "true", "yes", "enable", "enabled", "1"):
        return True
    if v in ("off", "false", "no", "disable", "disabled", "0"):
        return False
    raise Refused("Say on or off.")


# --------------------------------------------------------------------------- #
# Chats
# --------------------------------------------------------------------------- #


async def _find_chats(request: Any) -> list[Item]:
    from openexecutive.api.routes import sessions

    rows = await _call(sessions.get_sessions, request)
    return [
        Item(id=s.session_id, title=s.title or "Untitled chat", detail="pinned" if s.pinned_at else "")
        for s in rows
    ]


async def _chat_title(request: Any, session_id: str) -> str:
    from openexecutive.api.routes import sessions

    summary = await _call(sessions.get_session, session_id, request)
    return str(summary.title or "")


async def _pin_chat(request: Any, session_id: str, pinned: bool) -> str:
    from openexecutive.api.routes import sessions

    summary = await _call(sessions.update_session, session_id, sessions.SessionUpdate(pinned=pinned), request)
    return f"{'Pinned' if pinned else 'Unpinned'} {_q(summary.title)}"


async def _rename_chat(request: Any, session_id: str, value: str) -> str:
    from openexecutive.api.routes import sessions

    summary = await _call(sessions.update_session, session_id, sessions.SessionUpdate(title=value), request)
    return f"Renamed the chat {_q(summary.title)}"


async def _file_chat(request: Any, session_id: str, value: str) -> str:
    from openexecutive.api.routes import sessions

    summary = await _call(
        sessions.update_session, session_id, sessions.SessionUpdate(topic_name=value), request,
    )
    return f"Moved {_q(summary.title)} to the topic {_q(value)}"


async def _delete_chat(request: Any, session_id: str, value: str) -> str:
    from openexecutive.api.routes import sessions

    title = await _chat_title(request, session_id)
    await _call(sessions.delete_session_route, session_id, request)
    return f"Deleted the chat {_q(title)}"


def _teammate(name: str) -> Any:
    """The one active team member ``name`` names (full name, first name or
    email), or Refused."""
    from openexecutive.people.store import list_people

    wanted = " ".join(name.lower().split())
    team = [p for p in list_people() if p.kind == "team" and not p.archived and p.id is not None]
    exact = [p for p in team if p.full_name.lower() == wanted or (p.email or "").lower() == wanted]
    found = exact or [p for p in team if wanted and wanted in p.full_name.lower()]
    if not found:
        raise Refused(f"There's nobody on the team called {_q(name)}.")
    if len(found) > 1:
        raise Refused(f"More than one person matches {_q(name)}: {', '.join(p.full_name for p in found[:5])}. Say which.")
    return found[0]


async def _shared_ids(request: Any, session_id: str) -> list[int]:
    from openexecutive.api.routes import sessions

    return [s.person_id for s in await _call(sessions.get_session_shares, session_id, request)]


async def _share_chat(request: Any, session_id: str, value: str) -> str:
    from openexecutive.api.routes import sessions

    person = _teammate(value)
    ids = sorted({*await _shared_ids(request, session_id), person.id})
    await _call(sessions.put_session_shares, session_id, sessions.SharesUpdate(person_ids=ids), request)
    return f"Shared {_q(await _chat_title(request, session_id))} with {person.full_name}"


async def _unshare_chat(request: Any, session_id: str, value: str) -> str:
    from openexecutive.api.routes import sessions

    person = _teammate(value)
    current = await _shared_ids(request, session_id)
    if person.id not in current:
        raise Refused(f"This chat isn't shared with {person.full_name}.")
    ids = [i for i in current if i != person.id]
    await _call(sessions.put_session_shares, session_id, sessions.SharesUpdate(person_ids=ids), request)
    return f"Stopped sharing {_q(await _chat_title(request, session_id))} with {person.full_name}"


# --------------------------------------------------------------------------- #
# Topics and areas
# --------------------------------------------------------------------------- #


async def _topics(request: Any) -> list[Any]:
    from openexecutive.api.routes import sessions

    return list((await sessions.get_topics(request)).topics)


async def _find_topics(request: Any) -> list[Item]:
    return [
        Item(id=str(t.id), title=t.name, detail=", ".join(
            x for x in ((f"in {t.area}" if t.area else ""), ("pinned" if t.pinned_at else "")) if x
        ))
        for t in await _topics(request)
    ]


async def _topic_id(request: Any, topic_id: str) -> int:
    try:
        return int(topic_id)
    except ValueError:
        raise Refused("That isn't a topic id. Look it up with find_in_app.") from None


async def _topic_name(request: Any, topic_id: int) -> str:
    return next((t.name for t in await _topics(request) if t.id == topic_id), "")


async def _rename_topic(request: Any, topic_id: str, value: str) -> str:
    from openexecutive.api.routes import sessions

    tid = await _topic_id(request, topic_id)
    topic = await _call(sessions.update_topic, tid, sessions.TopicUpdate(name=value), request)
    return f"Renamed the topic {_q(topic.name)}"


async def _pin_topic(request: Any, topic_id: str, pinned: bool) -> str:
    from openexecutive.api.routes import sessions

    tid = await _topic_id(request, topic_id)
    topic = await _call(sessions.update_topic, tid, sessions.TopicUpdate(pinned=pinned), request)
    return f"{'Pinned' if pinned else 'Unpinned'} the topic {_q(topic.name)}"


async def _merge_topic(request: Any, topic_id: str, value: str) -> str:
    from openexecutive.api.routes import sessions

    tid = await _topic_id(request, topic_id)
    wanted = value.strip().lower()
    topics = await _topics(request)
    into = [t for t in topics if t.id != tid and (str(t.id) == wanted or t.name.lower() == wanted)]
    if len(into) != 1:
        raise Refused(f"There's no other topic called {_q(value)}.")
    name = await _topic_name(request, tid)
    await _call(sessions.merge_topic, tid, sessions.TopicMerge(into_id=into[0].id), request)
    return f"Moved the chats in {_q(name)} into {_q(into[0].name)}"


async def _remove_topic(request: Any, topic_id: str, value: str) -> str:
    from openexecutive.api.routes import sessions

    tid = await _topic_id(request, topic_id)
    name = await _topic_name(request, tid)
    await _call(sessions.ungroup_topic, tid, request)
    return f"Removed the topic {_q(name)}. Its chats are still there"


async def _find_areas(request: Any) -> list[Item]:
    areas = sorted({t.area for t in await _topics(request) if t.area})
    return [Item(id=a, title=a) for a in areas]


async def _rename_area(request: Any, area: str, value: str) -> str:
    from openexecutive.api.routes import sessions

    await _call(sessions.rename_area, sessions.AreaRename(area=area, name=value), request)
    return f"Renamed the area {_q(area)} to {_q(value)}"


async def _ungroup_area(request: Any, area: str, value: str) -> str:
    from openexecutive.api.routes import sessions

    await _call(sessions.ungroup_area, sessions.AreaUngroup(area=area), request)
    return f"Took the topics out of {_q(area)}"


# --------------------------------------------------------------------------- #
# Documents (the Documents page) and uploaded files
# --------------------------------------------------------------------------- #


async def _find_documents(request: Any) -> list[Item]:
    from openexecutive.api.routes import artifacts

    active = (await artifacts.list_artifacts(request, limit=200, archived=False))["artifacts"]
    archived = (await artifacts.list_artifacts(request, limit=100, archived=True))["artifacts"]
    return [Item(id=a.id, title=a.title, detail=a.format_label) for a in active] + [
        Item(id=a.id, title=a.title, detail="archived") for a in archived
    ]


async def _document_title(request: Any, composite_id: str) -> str:
    from openexecutive.api.routes import artifacts

    return str((await artifacts.get_artifact(composite_id, request)).title)


async def _archive_document(request: Any, composite_id: str, value: str) -> str:
    from openexecutive.api.routes import artifacts

    title = await _document_title(request, composite_id)
    await artifacts.archive_artifact(composite_id, request)
    return f"Archived {_q(title)}"


async def _restore_document(request: Any, composite_id: str, value: str) -> str:
    from openexecutive.api.routes import artifacts

    title = await _document_title(request, composite_id)
    await artifacts.restore_artifact(composite_id, request)
    return f"Restored {_q(title)}"


async def _delete_document(request: Any, composite_id: str, value: str) -> str:
    from openexecutive.api.routes import artifacts

    title = await _document_title(request, composite_id)
    await artifacts.delete_artifact(composite_id, request)
    return f"Deleted {_q(title)}"


async def _find_files(request: Any) -> list[Item]:
    from openexecutive.api.routes import documents

    docs = (await documents.list_documents(request))["documents"]
    return [Item(id=d["filename"], title=d["filename"]) for d in docs]


async def _delete_file(request: Any, filename: str, value: str) -> str:
    from openexecutive.api.routes import documents

    await documents.delete_document(filename, request)
    return f"Deleted the file {_q(filename)}"


_SOURCES = {"drive": "Google Drive", "onedrive": "OneDrive", "notion": "Notion"}


async def _find_sources(request: Any) -> list[Item]:
    from openexecutive.api.routes import documents

    return [Item(id=s, title=label) for s, label in _SOURCES.items() if documents._source_enabled(s)]


async def _sync_source(request: Any, source: str, value: str) -> str:
    from openexecutive.api.routes import documents

    if source not in _SOURCES:
        raise Refused("That isn't a connected source.")
    await documents.sync_source(source)  # type: ignore[arg-type]
    return f"Started syncing {_SOURCES[source]}"


# --------------------------------------------------------------------------- #
# Alerts: only the cards the server showed or found this turn (ack_alert's rule)
# --------------------------------------------------------------------------- #


def _trusted_alert_ids() -> set[int]:
    from openexecutive.orchestrator.schedule_tools import current_session

    session = current_session.get()
    return {int(i) for i in (getattr(session, "trusted_alert_ids", None) or set())}


async def _find_alerts(request: Any) -> list[Item]:
    from openexecutive.alerts import store

    items = []
    for alert_id in sorted(_trusted_alert_ids()):
        alert = store.get_alert(alert_id)
        if alert is not None and alert.status in ("unread", "read"):
            items.append(Item(id=str(alert_id), title=alert.headline))
    return items


async def _snooze_alert(request: Any, alert_id: str, value: str) -> str:
    from openexecutive.api.routes import alerts

    try:
        aid = int(alert_id)
    except ValueError:
        raise Refused("That isn't an alert id.") from None
    if aid not in _trusted_alert_ids():
        raise Refused("I can only snooze a card you were shown here. Look it up with find_alerts first.")
    try:
        until = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise Refused("Give the time as an ISO date and time, e.g. 2026-10-20T09:00:00-04:00.") from None
    alert = await _call(alerts.snooze_alert, aid, alerts.SnoozeBody(until=until), request)
    when = until.astimezone(UTC).strftime("%b %-d, %H:%M UTC")
    return f"Snoozed {_q(alert.headline)} until {when}"


async def _prepare_clear_all(request: Any, item_id: str, value: str) -> str:
    ids = sorted(_trusted_alert_ids())
    if not ids:
        raise Refused("There are no cards in front of us to clear. Open Today first.")
    return ",".join(str(i) for i in ids[:500])


async def _clear_alerts(request: Any, item_id: str, value: str) -> str:
    from openexecutive.api.routes import alerts

    ids = [int(i) for i in value.split(",") if i.strip().isdigit()]
    if not ids:
        raise Refused("There are no cards to clear.")
    done = await _call(alerts.bulk_ack_alerts, alerts.BulkAckBody(status="ack", alert_ids=ids), request)
    return f"Cleared {done.count} card{'' if done.count == 1 else 's'}"


# --------------------------------------------------------------------------- #
# Watch suggestions (the principal's watch list)
# --------------------------------------------------------------------------- #


async def _find_watch_suggestions(request: Any) -> list[Item]:
    from openexecutive.api.routes import watchlist

    out = []
    for w in await _call(watchlist.list_watchlist):
        try:
            watchlist._pending_suggestion_or_409(w.slug)
        except Exception:
            continue
        out.append(Item(id=w.slug, title=w.target, detail=(w.notes or "")[:120]))
    return out


async def _approve_watch(request: Any, slug: str, value: str) -> str:
    from openexecutive.api.routes import watchlist

    item = await _call(watchlist.approve_watchlist_suggestion, slug)
    return f"Now watching {_q(item.target)}"


async def _decline_watch(request: Any, slug: str, value: str) -> str:
    from openexecutive.api.routes import watchlist

    await _call(watchlist.decline_watchlist_suggestion, slug, None)
    return f"Declined watching {_q(slug)}"


# --------------------------------------------------------------------------- #
# Workflows and their runs
# --------------------------------------------------------------------------- #


async def _find_workflows(request: Any) -> list[Item]:
    from openexecutive.api.routes import workflows

    defs = (await workflows.list_custom_workflows())["definitions"]
    return [
        Item(id=d["name"], title=d.get("title") or d["name"], detail="on" if d.get("is_active") else "off")
        for d in defs
    ]


async def _workflow_title(name: str) -> str:
    from openexecutive.api.routes import workflows

    found = await workflows.get_custom_workflow(name)
    return str(found.get("title") or name)


async def _turn_workflow(request: Any, name: str, on: bool) -> str:
    from openexecutive.api.routes import workflows

    defn = await workflows.activate_custom_workflow(name, with_body(request, {"is_active": on}))
    title = defn.get("title") or name
    return f"Turned {_q(title)} {'on' if on else 'off'}"


async def _delete_workflow(request: Any, name: str, value: str) -> str:
    from openexecutive.api.routes import workflows

    title = await _workflow_title(name)
    await workflows.delete_custom_workflow(name)
    return f"Deleted the workflow {_q(title)}"


async def _find_runs(request: Any) -> list[Item]:
    from openexecutive.api.routes import workflows

    runs = (await workflows.list_workflow_runs(request, limit=50))["runs"]
    return [
        Item(id=r["run_id"], title=r.get("title") or r.get("workflow_name") or r["run_id"], detail=str(r.get("status") or ""))
        for r in runs
    ]


async def _run_title(request: Any, run_id: str) -> str:
    from openexecutive.api.routes import workflows

    run = await workflows.get_workflow_run(run_id, request)
    return str(run.get("title") or run.get("workflow_name") or run_id)


async def _rerun(request: Any, run_id: str, value: str) -> str:
    from openexecutive.api.routes import workflows

    title = await _run_title(request, run_id)
    await _call(workflows.rerun_workflow_run, run_id, request)
    return f"Started {_q(title)} again"


async def _decide_run(request: Any, run_id: str, decision: str) -> str:
    from openexecutive.api.routes import workflows

    title = await _run_title(request, run_id)
    await workflows.decide_workflow_run(run_id, with_body(request, {"decision": decision}))
    return f"{'Approved' if decision == 'approve' else 'Turned down'} the waiting step in {_q(title)}"


async def _delete_run(request: Any, run_id: str, value: str) -> str:
    from openexecutive.api.routes import workflows

    title = await _run_title(request, run_id)
    await workflows.delete_workflow_run(run_id, request)
    return f"Deleted the run {_q(title)}"


# --------------------------------------------------------------------------- #
# Memory: past decisions, initiatives and advice; suggested facts
# --------------------------------------------------------------------------- #


async def _find_memories(request: Any) -> list[Item]:
    from openexecutive.api.routes import episodic

    items = [
        Item(id=f"decision:{d.id}", title=d.summary[:120], detail="decision")
        for d in await _call(episodic.get_decisions)
    ]
    items += [
        Item(id=f"initiative:{i.id}", title=i.title[:120], detail=f"initiative, {i.status}")
        for i in await _call(episodic.get_initiatives)
    ]
    items += [
        Item(id=f"advice:{a.id}", title=(a.query_summary or a.advice_summary)[:120], detail="advice")
        for a in await _call(episodic.get_advice_list)
    ]
    return items


def _memory_ref(ref: str) -> tuple[str, int]:
    kind, _, raw = ref.partition(":")
    if kind not in ("decision", "initiative", "advice") or not raw.isdigit():
        raise Refused("That isn't a memory id. Look it up with find_in_app.")
    return kind, int(raw)


async def _edit_memory(request: Any, ref: str, value: str) -> str:
    from openexecutive.api.routes import episodic

    kind, mid = _memory_ref(ref)
    if kind == "decision":
        await _call(episodic.patch_decision, mid, episodic.DecisionUpdate(summary=value))
    elif kind == "initiative":
        await _call(episodic.patch_initiative, mid, episodic.InitiativeUpdate(summary=value))
    else:
        await _call(episodic.patch_advice, mid, episodic.AdviceUpdate(advice_summary=value))
    return f"Updated the {kind}"


async def _delete_memory(request: Any, ref: str, value: str) -> str:
    from openexecutive.api.routes import episodic

    kind, mid = _memory_ref(ref)
    remove = {
        "decision": episodic.remove_decision,
        "initiative": episodic.remove_initiative,
        "advice": episodic.remove_advice,
    }[kind]
    await _call(remove, mid)
    return f"Deleted the {kind} from memory"


async def _find_suggested_facts(request: Any) -> list[Item]:
    from openexecutive.api.routes import episodic

    page = await _call(episodic.get_facts, request, include_inactive=True, limit=200)
    return [
        Item(id=str(f.id), title=f"{f.subject}: {f.statement}"[:160], detail="waiting for your OK")
        for f in page.facts if f.status == "proposed"
    ]


async def _review_fact(request: Any, fact_id: str, approve: bool) -> str:
    from openexecutive.api.routes import episodic

    if not fact_id.isdigit():
        raise Refused("That isn't a fact id.")
    if approve:
        fact = await _call(episodic.approve_standing_fact, int(fact_id), request)
        return f"Approved the fact {_q(fact.subject)}"
    fact = await _call(episodic.decline_standing_fact, int(fact_id), request, None)
    return f"Declined the fact {_q(fact.subject)}"


# --------------------------------------------------------------------------- #
# Departments and skills
# --------------------------------------------------------------------------- #


async def _find_departments(request: Any) -> list[Item]:
    from openexecutive.api.routes import departments

    return [Item(id=d.config.slug, title=d.config.title) for d in await _call(departments.list_departments)]


async def _add_department(request: Any, item_id: str, value: str) -> str:
    from openexecutive.api.routes import departments

    made = await _call(departments.create_department, departments.DepartmentCreate(title=value))
    return f"Added the department {_q(made.config.title)}"


async def _rename_department(request: Any, slug: str, value: str) -> str:
    from openexecutive.api.routes import departments

    await _call(departments.patch_department, slug, departments.DepartmentPatch(title=value))
    return f"Renamed the department {_q(value)}"


async def _delete_department(request: Any, slug: str, value: str) -> str:
    from openexecutive.api.routes import departments

    title = (await _call(departments.get_department, slug)).config.title
    await _call(departments.delete_department, slug)
    return f"Deleted the department {_q(title)}"


async def _find_skills(request: Any) -> list[Item]:
    from openexecutive.api.routes import skills

    listed = await skills.list_all_skills(include_hidden=True)
    return [Item(id=s.name, title=s.name, detail="hidden" if s.hidden else s.source) for s in listed.skills]


async def _delete_skill(request: Any, name: str, value: str) -> str:
    from openexecutive.api.routes import skills

    await skills.delete_skill_endpoint(name, request)
    return f"Removed the skill {_q(name)}"


async def _restore_skill(request: Any, name: str, value: str) -> str:
    from openexecutive.api.routes import skills

    await skills.restore_skill_endpoint(name, request)
    return f"Restored the skill {_q(name)}"


# --------------------------------------------------------------------------- #
# The person's own settings
# --------------------------------------------------------------------------- #

# id → what the person calls it
_SETTINGS: dict[str, str] = {
    "quiet_hours": "Quiet hours",
    "delivery": "How messages arrive (instant or digest)",
    "urgent_through": "Let urgent things through quiet hours",
    "act_as_me": "Act as me",
    "inbox_drafts": "Draft replies to my inbox",
    "handle_it": "Handle it for me",
    "take_the_lead": "Take the lead as me",
}
# Turning these on lets the Executive send as the person or act on its own.
_WIDENING = frozenset({"act_as_me", "handle_it", "take_the_lead"})


async def _find_settings(request: Any) -> list[Item]:
    from openexecutive.api.routes import delegation, notifications

    items = []
    try:
        n = await _call(notifications.get_notifications, request)
        q = n.quiet_hours
        items += [
            Item(id="quiet_hours", title=_SETTINGS["quiet_hours"], detail=f"{q.start}-{q.end}" if q.enabled else "off"),
            Item(id="delivery", title=_SETTINGS["delivery"], detail=n.delivery),
            Item(id="urgent_through", title=_SETTINGS["urgent_through"], detail="on" if n.urgent_through else "off"),
        ]
    except Exception:
        pass
    try:
        d = await delegation.get_delegation(request)
        state = d.model_dump()
        items.append(Item(id="act_as_me", title=_SETTINGS["act_as_me"], detail="on" if state.get("enabled") else "off"))
        items += [Item(id=k, title=_SETTINGS[k]) for k in ("inbox_drafts", "handle_it", "take_the_lead")]
    except Exception:
        pass
    return items


def _quiet_hours(value: str, current: Any) -> Any:
    from openexecutive.people.notifications import QuietHours

    v = value.strip().lower()
    if v in ("off", "none", "no"):
        return current.model_copy(update={"enabled": False})
    start, sep, end = v.replace(" to ", "-").replace("–", "-").partition("-")
    if not sep:
        raise Refused("Give quiet hours as start-end in 24-hour time, e.g. 22:00-07:00, or off.")
    try:
        return QuietHours(enabled=True, start=start.strip(), end=end.strip(), days=current.days)
    except ValueError:
        raise Refused("Give quiet hours as start-end in 24-hour time, e.g. 22:00-07:00, or off.") from None


async def _change_setting(request: Any, setting: str, value: str) -> str:
    from openexecutive.api.routes import delegation, notifications

    label = _SETTINGS.get(setting)
    if label is None:
        raise Refused("That isn't a setting I can change.")
    if setting in ("quiet_hours", "delivery", "urgent_through"):
        n = await _call(notifications.get_notifications, request)
        quiet, delivery, urgent = n.quiet_hours, n.delivery, n.urgent_through
        if setting == "quiet_hours":
            quiet = _quiet_hours(value, quiet)
            said = f"{quiet.start}-{quiet.end}" if quiet.enabled else "off"
        elif setting == "delivery":
            if value.strip().lower() not in ("instant", "digest"):
                raise Refused("Say instant or digest.")
            delivery = value.strip().lower()
            said = delivery
        else:
            urgent = _on(value)
            said = "on" if urgent else "off"
        await _call(notifications.update_notifications, request, notifications.NotificationsUpdate(
            quiet_hours=quiet, delivery=delivery, urgent_through=urgent, timezone=n.timezone,
        ))
        return f"Set {label} to {said}"
    on = _on(value)
    if setting == "act_as_me":
        await delegation.update_delegation(request, delegation.DelegationUpdate(enabled=on))
    elif setting == "inbox_drafts":
        await delegation.update_delegation_inbox(request, delegation.InboxUpdate(enabled=on))
    elif setting == "handle_it":
        await delegation.update_delegation_handle_it(request, delegation.HandleItUpdate(enabled=on))
    else:
        await delegation.update_take_the_lead_as_you(request, delegation.LeadUpdate(enabled=on))
    return f"Turned {label} {'on' if on else 'off'}"


def _setting_needs_tap(setting: str, value: str) -> bool:
    if setting not in _WIDENING:
        return False
    try:
        return _on(value)
    except Refused:
        return True


async def _check_inbox(request: Any, item_id: str, value: str) -> str:
    from openexecutive.api.routes import delegation

    await delegation.check_delegation_inbox(request)
    return "Started checking your inbox"


async def _find_lead_rules(request: Any) -> list[Item]:
    from openexecutive.api.routes import delegation

    rules = (await _call(delegation.get_my_lead_rules, request)).rules
    return [Item(id=str(r.id), title=f"{r.kind}: {r.value}") for r in rules]


async def _add_lead_rule(request: Any, item_id: str, value: str) -> str:
    from openexecutive.api.routes import delegation

    kind, sep, text = value.partition(":")
    if not sep:
        raise Refused("Give the rule as kind: value, e.g. domain: acme.com. Kinds: person, domain, words, amount.")
    await _call(
        delegation.add_my_lead_rule, request, delegation.LeadRuleIn(kind=kind.strip().lower(), value=text.strip()),
    )
    return f"Added the rule: always ask first about {kind.strip().lower()} {_q(text.strip())}"


async def _remove_lead_rule(request: Any, rule_id: str, value: str) -> str:
    from openexecutive.api.routes import delegation

    if not rule_id.isdigit():
        raise Refused("That isn't a rule id.")
    await _call(delegation.delete_my_lead_rule, request, int(rule_id))
    return "Removed the rule"


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


def _fixed(fn: Callable[..., Any], *extra: Any) -> Any:
    async def run(request: Any, item_id: str, value: str) -> str:
        return await fn(request, item_id, *extra)  # type: ignore[no-any-return]

    return run


async def _nothing(request: Any) -> list[Item]:
    return []


_KINDS = (
    Kind("chat", "chat", _find_chats),
    Kind("topic", "chat topic", _find_topics),
    Kind("topic_area", "topic area", _find_areas),
    Kind("document", "document", _find_documents),
    Kind("file", "uploaded file", _find_files, principal_only=True),
    Kind("source", "synced source", _find_sources, principal_only=True),
    Kind("alert", "card on Today", _find_alerts),
    Kind("alert_board", "the cards on Today", _nothing, principal_only=True, no_item=True),
    Kind("watch_suggestion", "suggested watch", _find_watch_suggestions, principal_only=True),
    Kind("workflow", "workflow", _find_workflows, principal_only=True),
    Kind("workflow_run", "workflow run", _find_runs),
    Kind("memory", "memory (decision, initiative or advice)", _find_memories, principal_only=True),
    Kind("suggested_fact", "suggested fact", _find_suggested_facts, principal_only=True),
    Kind("department", "department", _find_departments, principal_only=True),
    Kind("new_department", "new department", _nothing, principal_only=True, no_item=True),
    Kind("skill", "skill", _find_skills, principal_only=True),
    Kind("setting", "setting", _find_settings),
    Kind("inbox", "inbox", _nothing, no_item=True),
    Kind("lead_rule", "Take the lead rule", _find_lead_rules),
    Kind("new_lead_rule", "new Take the lead rule", _nothing, no_item=True),
)

_ACTIONS = (
    # Chats
    Action("pin_chat", "chat", "Pin a chat to the top of the chat list", _fixed(_pin_chat, True)),
    Action("unpin_chat", "chat", "Unpin a chat", _fixed(_pin_chat, False)),
    Action("rename_chat", "chat", "Rename a chat", _rename_chat, value="the new name"),
    Action("move_chat_to_topic", "chat", "File a chat under a topic (made if new)", _file_chat, value="the topic's name"),
    Action("delete_chat", "chat", "Delete a chat for good", _delete_chat, confirm=True,
           card="Delete the chat {title}"),
    Action("share_chat", "chat", "Share a chat with a team member, who can then read and write in it",
           _share_chat, confirm=True, value="the person's name", card="Share the chat {title} with {value}"),
    Action("unshare_chat", "chat", "Stop sharing a chat with someone", _unshare_chat, value="the person's name"),
    # Topics
    Action("rename_topic", "topic", "Rename a chat topic", _rename_topic, value="the new name"),
    Action("pin_topic", "topic", "Pin a chat topic", _fixed(_pin_topic, True)),
    Action("unpin_topic", "topic", "Unpin a chat topic", _fixed(_pin_topic, False)),
    Action("merge_topic", "topic", "Move every chat in a topic into another topic", _merge_topic,
           value="the other topic's name"),
    Action("remove_topic", "topic", "Remove a topic; its chats stay, under no topic", _remove_topic),
    Action("rename_topic_area", "topic_area", "Rename an area that groups topics", _rename_area,
           value="the new name"),
    Action("ungroup_topic_area", "topic_area", "Take the topics out of an area", _ungroup_area),
    # Documents and files
    Action("archive_document", "document", "Archive a document (it can be restored)", _archive_document),
    Action("restore_document", "document", "Restore an archived document", _restore_document),
    Action("delete_document", "document", "Delete a document for good", _delete_document, confirm=True,
           card="Delete the document {title}"),
    Action("delete_file", "file", "Delete an uploaded file and what was learned from it", _delete_file,
           confirm=True, card="Delete the uploaded file {title}"),
    Action("sync_source", "source", "Sync a connected source now", _sync_source),
    # Alerts
    Action("snooze_alert", "alert", "Hide a card on Today until a time, when it comes back", _snooze_alert,
           value="when it comes back, ISO 8601 with a zone, at most 30 days out"),
    Action("clear_all_alerts", "alert_board", "Clear every card shown on Today", _clear_alerts, confirm=True,
           card="Clear every card on Today", prepare=_prepare_clear_all),
    # Watch suggestions
    Action("approve_watch", "watch_suggestion", "Start a suggested watch", _approve_watch),
    Action("decline_watch", "watch_suggestion", "Decline a suggested watch", _decline_watch),
    # Workflows
    Action("turn_on_workflow", "workflow", "Turn a workflow on so it runs on its schedule",
           _fixed(_turn_workflow, True), confirm=True, card="Turn on the workflow {title}"),
    Action("turn_off_workflow", "workflow", "Turn a workflow off", _fixed(_turn_workflow, False)),
    Action("delete_workflow", "workflow", "Delete a workflow for good", _delete_workflow, confirm=True,
           card="Delete the workflow {title}"),
    Action("rerun_workflow_run", "workflow_run", "Run a failed scheduled run again", _rerun),
    Action("approve_workflow_step", "workflow_run", "Approve a run's step that is waiting for a yes",
           _fixed(_decide_run, "approve"), confirm=True, card="Approve the waiting step in {title}"),
    Action("reject_workflow_step", "workflow_run", "Turn down a run's step that is waiting for a yes",
           _fixed(_decide_run, "reject")),
    Action("delete_workflow_run", "workflow_run", "Delete a run and what it made", _delete_run, confirm=True,
           card="Delete the run {title}"),
    # Memory
    Action("edit_memory", "memory", "Change what a past decision, initiative or advice says", _edit_memory,
           value="what it should say now"),
    Action("delete_memory", "memory", "Delete a past decision, initiative or advice from memory", _delete_memory,
           confirm=True, card="Delete from memory: {title}"),
    Action("approve_fact", "suggested_fact", "Approve a fact a teammate suggested", _fixed(_review_fact, True)),
    Action("decline_fact", "suggested_fact", "Decline a fact a teammate suggested", _fixed(_review_fact, False)),
    # Departments
    Action("add_department", "new_department", "Add a department (use id \"new\")", _add_department,
           value="its name"),
    Action("rename_department", "department", "Rename a department", _rename_department, value="the new name"),
    Action("delete_department", "department", "Delete a department", _delete_department, confirm=True,
           card="Delete the department {title}"),
    # Skills
    Action("delete_skill", "skill", "Delete a company skill, undo a change to one, or hide a built-in one",
           _delete_skill, confirm=True, card="Remove the skill {title}"),
    Action("restore_skill", "skill", "Bring back a hidden built-in skill", _restore_skill),
    # Settings
    Action("change_setting", "setting",
           "Change one of the person's own settings: quiet_hours (start-end or off), delivery "
           "(instant or digest), urgent_through, act_as_me, inbox_drafts, handle_it, take_the_lead (on or off)",
           _change_setting, confirm=_setting_needs_tap, value="the new value", card="Set {title} to {value}"),
    Action("check_inbox_now", "inbox", "Check the person's inbox for emails to draft replies to now (use id \"inbox\")",
           _check_inbox),
    Action("add_lead_rule", "new_lead_rule",
           "Add a rule that makes Take the lead ask first (use id \"new\")", _add_lead_rule,
           value="kind: value, kinds person, domain, words, amount"),
    Action("remove_lead_rule", "lead_rule", "Remove a Take the lead rule, so more goes ahead on its own",
           _remove_lead_rule, confirm=True, card="Remove the Take the lead rule {title}"),
)

# Where a link from chat can go (the web app's own paths).
_SCREENS = (
    ("today", "/today", "Today"),
    ("chats", "/chats", "all chats"),
    ("settings", "/settings", "Settings"),
    ("notifications", "/settings/notifications", "Settings, Notifications"),
    ("act_as_me", "/settings/act-as-me", "Settings, Act as me"),
    ("memory_settings", "/settings/memory", "Settings, Memory"),
    ("people", "/people", "People"),
    ("departments", "/departments", "Departments"),
    ("goals", "/goals", "Goals"),
    ("watchlist", "/watchlist", "Watch list"),
    ("documents", "/artifacts", "Documents"),
    ("knowledge", "/knowledge", "Knowledge (uploaded files)"),
    ("memories", "/memories", "Memory"),
    ("skills", "/skills", "Skills"),
    ("workflows", "/jobs", "Workflows"),
    ("guide", "/guide", "User guide"),
)


def install() -> None:
    """Add this repo's kinds, actions and screens (once)."""
    from openexecutive.orchestrator.app_control import CATALOG

    if "chat" in CATALOG.kinds:
        return
    for kind in _KINDS:
        register_kind(kind)
    for action in _ACTIONS:
        register_action(action)
    for name, path, label in _SCREENS:
        register_screen(name, path, label)


install()
