"""Whether the API knows a caller is the person at an address: the rule for
anything done as that person on their own say-so (Send on a reply card,
turning YOLO mode on), kept apart so routes can check it without
importing the send path (``reply_send``)."""
from __future__ import annotations

from typing import Any

NOT_YOURS = "not_yours"
SIGNING_REQUIRED = "caller_signing_required"


def caller_refusal(caller: Any, email: str) -> str | None:
    """None when the API knows ``caller`` is the person at ``email``; else
    ``NOT_YOURS`` or ``SIGNING_REQUIRED``.

    Signed callers on (``CALLER_ASSERTION_PUBLIC_KEYS``): that signed-in
    person, or the local operator under local login. Signing off: only an API
    that answers this computer alone (local login), where the person at the
    keyboard is the owner. Anything else could be whoever holds the shared
    secret."""
    from openexecutive.api.caller import signing_on
    from openexecutive.utils.deployment import is_local_login

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
