"""Whether the API knows a caller is the person at an address: the rule for
anything done as that person on their own say-so (Send on a reply card,
turning Handle it for me on), kept apart so routes can check it without
importing the send path (``reply_send``)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

NOT_YOURS = "not_yours"
SIGNING_REQUIRED = "caller_signing_required"


@dataclass(frozen=True)
class ChatTap:
    """A tap on a card's button in a chat app, by the person at ``email``:
    made only by ``integrations.channel_cards`` once the chat app proved who
    tapped (a private chat that is theirs, on a connection only that app can
    call)."""

    email: str


def caller_refusal(caller: Any, email: str) -> str | None:
    """None when the API knows ``caller`` is the person at ``email``; else
    ``NOT_YOURS`` or ``SIGNING_REQUIRED``.

    Signed callers on (``CALLER_ASSERTION_PUBLIC_KEYS``): that signed-in
    person, or the local operator under local login. Signing off: only an API
    that answers this computer alone (local login), where the person at the
    keyboard is the owner. Anything else could be whoever holds the shared
    secret. A ``ChatTap`` is that person when it names their address.
    """
    from openexecutive.api.caller import signing_on
    from openexecutive.utils.deployment import is_local_login

    if isinstance(caller, ChatTap):
        return None if caller.email and caller.email == email else NOT_YOURS
    if signing_on():
        if caller.kind == "user" and caller.email == email:
            return None
        if caller.kind == "operator" and is_local_login():
            return None
        return NOT_YOURS
    if is_local_login():
        # The API answers this computer only (api.main), and the web app
        # sends no caller: the person at the keyboard is the owner.
        if caller.kind == "open" and caller.email in ("", email):
            return None
        return NOT_YOURS
    return SIGNING_REQUIRED
