"""Handle it for me, in training: Send + allow on a reply card.

With Handle it for me's dial on In training (``handle_it.MODE_TRAINING``),
every reply waits on its card. The card adds **Send + allow**: it sends that
reply on the person's tap, as Send does, and from then on replies to that
same sender may go on their own, under every check Handle it always makes
(``handle_it.refusal``: the topics that always wait, links and amounts, new
recipients, the daily limit, ...). With "Do it like this next time" ticked,
a draft the person changed in their mailbox before sending is kept as an
example the ghostwriter follows when it next writes to that sender
(``ghostwriter``'s ``<writer_example>``).

What it learns lives in Take the lead's one "What it's learned" list
(``take_the_lead.allow``), as feature ``act_as_me`` with the person's id:
theirs alone to see and remove, and never in anyone else's prompts. Allowing
happens only in ``reply_send`` after a tap the API knows is the person's
(the rule Send itself uses).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from openexecutive.orchestrator.take_the_lead import FEATURE_ACT_AS_ME, Allowed

logger = logging.getLogger(__name__)

# The longest example kept, after the quoted thread is dropped.
EXAMPLE_CHARS = 1000


def _email(address: str) -> str:
    from openexecutive.delegation.gmail import normalize_email

    return normalize_email(address or "")


def reply_key(person_id: int, sender: str) -> str:
    return f"{FEATURE_ACT_AS_ME}|person:{person_id}|reply|{_email(sender)}"


def reply_label(name: str, sender: str) -> str:
    from openexecutive.delegation.ghostwriter import one_line

    shown = one_line(name or "", 80)
    email = _email(sender)
    return f"Reply to {shown} ({email})" if shown else f"Reply to {email}"


def allowed_sender(person_id: int, sender: str, *, db_path: Path | None = None) -> Allowed | None:
    """The allowance for ``person_id``'s replies to ``sender``, if they gave
    one. Never raises: unreadable is not allowed."""
    from openexecutive.orchestrator import take_the_lead

    if not _email(sender):
        return None
    try:
        found = take_the_lead.find_allowed(reply_key(person_id, sender), db_path=db_path)
    except Exception:
        logger.warning("delegation.training: couldn't read what it's learned", exc_info=True)
        return None
    return found if found is not None and found.person_id == person_id else None


def example_for(person_id: int, sender: str, *, db_path: Path | None = None) -> str | None:
    """The reply ``person_id`` sent ``sender`` after changing a draft, kept
    with "Do it like this next time"; None when there isn't one."""
    found = allowed_sender(person_id, sender, db_path=db_path)
    if found is None or not found.example:
        return None
    try:
        text = json.loads(found.example).get("text")
    except (ValueError, AttributeError):
        return None
    return text if isinstance(text, str) and text.strip() else None


def room_for(person_id: int, sender: str, *, db_path: Path | None = None) -> bool:
    from openexecutive.orchestrator import take_the_lead

    return take_the_lead.room_for(reply_key(person_id, sender), person_id=person_id, db_path=db_path)


def allow_sender(
    person_id: int, sender: str, name: str, *, example: str | None, decision_id: int | None,
    db_path: Path | None = None,
) -> Allowed:
    """Let replies to ``sender`` go on their own for ``person_id`` (callers
    check it's their tap first), keeping ``example`` when given. Raises
    ``take_the_lead.RuleError`` when their list is full."""
    from openexecutive.integrations.email_poller import sender_new_text
    from openexecutive.orchestrator import take_the_lead

    kept = sender_new_text(example or "").strip()[:EXAMPLE_CHARS] if example else ""
    return take_the_lead.allow(
        reply_key(person_id, sender), reply_label(name, sender),
        example={"text": kept} if kept else None,
        created_by=f"person:{person_id}", decision_id=decision_id, person_id=person_id, db_path=db_path,
    )


def learned(person_id: int, *, db_path: Path | None = None) -> list[Allowed]:
    """``person_id``'s own Act as me allowances, newest first."""
    from openexecutive.orchestrator import take_the_lead

    return take_the_lead.list_allowed(feature=FEATURE_ACT_AS_ME, person_id=person_id, db_path=db_path)


def forget(person_id: int, allowed_id: int, *, db_path: Path | None = None) -> bool:
    """Ask again for one of ``person_id``'s own; anyone else's stays."""
    from openexecutive.orchestrator import take_the_lead

    return take_the_lead.disallow(allowed_id, person_id=person_id, shared=False, db_path=db_path)


def used(allowance: Allowed, *, db_path: Path | None = None) -> None:
    from openexecutive.orchestrator import take_the_lead

    take_the_lead._used(allowance.id, db_path=db_path)
