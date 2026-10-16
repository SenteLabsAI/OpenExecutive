"""Who reads a brief, and keeping them "you" in it.

Every brief surface (the /today header, the morning brief, the end-of-day
digest) is written for one person, but its inputs name that person like
anyone else: their own chat ("Inbound google_chat from <their name>"), the
Executive's replies to their address, a chat titled with their name, the
activity log. A model handed those lines wrote about its reader in the third
person ("<their name> has been messaging you nonstop").

So the reader is fixed in code, not left to the model:

  • ``mask`` rewrites the reader's full name and email addresses in the
    rendered context to "the reader", so no input line names them;
  • ``reader_line`` heads the context with who that is;
  • ``slips`` finds the name, an address, "the reader" or "the principal" in
    what the model wrote, and ``fix_slips`` turns any left after one retry
    into "you" / "your".
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

READER = "the reader"

# What the model must never write about its reader: the context's own label
# for them. "The principal" is never written for anyone: it is the reader on
# the owner's own brief and the owner by name on a teammate's.
_READER_LABELS = ("the reader",)
_OWNER_LABEL = "the principal"
# The Executive writes every brief, so it is "I" / "me" / "my" in it.
# Not a title ("the Executive Director") or a noun phrase ("the Executive
# team"), where "I" would not read.
_EXECUTIVE = re.compile(
    r"\b[Tt]he Executive(['’]s)?\b(?! [A-Z])(?! (?:team|teams|office|offices|committee|"
    r"board|role|roles|suite|level|summary|summaries|session|sessions|assistant|"
    r"function|branch|order|orders|member|members|leadership|staff)\b)"
)
# Words after which the Executive is the object: "emailed the Executive" → "me".
_OBJECT_BEFORE = re.compile(
    r"(?:\b(?:to|from|with|for|by|of|on|at|about|than|asked|told|emailed|"
    r"messaged|pinged|sent|gave|asking|telling|messaging|emailing|pinging)|,)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Reader:
    """The person a brief is written for."""

    name: str
    emails: tuple[str, ...] = ()
    # The owner's name when the reader is a teammate (None: the reader is
    # the owner), for "the principal" in a teammate's brief.
    owner: str | None = None

    def terms(self) -> list[str]:
        """The reader's full name and addresses, longest first. A bare first
        name is left out: on a team it names someone else just as often."""
        name = " ".join(self.name.split())
        found = [name] if len(name.split()) > 1 else []
        found += [e.strip().lower() for e in self.emails if e and "@" in e]
        return sorted(set(found), key=len, reverse=True)


def _from_person(person: Any) -> Reader | None:
    if person is None or not (person.full_name or "").strip():
        return None
    return Reader(
        name=person.full_name,
        emails=tuple(a for a in [person.email or "", *person.email_aliases] if a),
    )


def principal_reader() -> Reader | None:
    """The principal as a Reader, or None (also on a failed lookup)."""
    from openexecutive.people.store import find_principal_person

    try:
        return _from_person(find_principal_person())
    except Exception:
        logger.exception("reader: principal lookup failed")
        return None


def person_reader(person_id: int) -> Reader | None:
    """A teammate as a Reader, or None (also on a failed lookup)."""
    from openexecutive.people.store import get_person

    try:
        reader = _from_person(get_person(person_id))
    except Exception:
        logger.exception("reader: person lookup failed")
        return None
    owner = principal_reader()
    if reader is None or owner is None:
        return reader
    return Reader(name=reader.name, emails=reader.emails, owner=owner.name)


def _pattern(terms: list[str]) -> re.Pattern[str] | None:
    if not terms:
        return None
    alts = "|".join(re.escape(t) for t in terms)
    # Whole words only, and the possessive with it: "<name>'s" → "the reader's".
    return re.compile(rf"(?<![\w@.])(?:{alts})(?![\w@])", re.IGNORECASE)


def reader_line(reader: Reader) -> str:
    """THE READER line: who "the reader" in the context is."""
    from openexecutive.briefing.live_signals import _one_line

    also = ", ".join(e for e in reader.terms() if "@" in e)
    who = _one_line(reader.name, 80) + (f" ({_one_line(also, 160)})" if also else "")
    return (
        f"THE READER (the person this is written for; every line below that "
        f"says 'the reader' means them): {who}\n"
    )


def mask(text: str, reader: Reader | None) -> str:
    """``text`` with the reader's full name and addresses as "the reader"."""
    if reader is None:
        return text
    pattern = _pattern(reader.terms())
    return pattern.sub(READER, text) if pattern else text


def slips(text: str, reader: Reader | None) -> list[str]:
    """The ways ``text`` names its reader instead of saying "you", or calls
    anyone "the principal", or the Executive instead of saying "I"."""
    if reader is None:
        return []
    pattern = _pattern([*reader.terms(), *_READER_LABELS, _OWNER_LABEL])
    found = {m.group(0).lower() for m in pattern.finditer(text)} if pattern else set()
    if _EXECUTIVE.search(text):
        found.add("the executive")
    return sorted(found)


def rewrite_request(named: list[str], reader: Reader | None) -> str:
    """The note that sends a slipping draft back: each kind of slip gets its
    own replacement ("you" for the reader, the owner's name for "the
    principal" on a teammate's brief, "I" / "me" / "my" for the Executive)."""
    executive = "the executive" in named
    owner = reader.owner if reader is not None and _OWNER_LABEL in named else None
    as_you = [n for n in named if n != "the executive" and not (owner and n == _OWNER_LABEL)]
    asks: list[str] = []
    if as_you:
        asks.append(
            "write " + ", ".join(f"'{n}'" for n in as_you)
            + " as 'you' or 'your' (that is the person reading it)"
        )
    if owner:
        asks.append(f"write 'the principal' as {owner}")
    if executive:
        asks.append("write 'the Executive' as 'I', 'me' or 'my' (that is you)")
    return (
        "Rewrite that: " + "; ".join(asks)
        + ". Change nothing else, and output only the rewrite."
    )


def _cased(word: str, start: int, source: str) -> str:
    before = source[:start].rstrip("*_ -")
    return word[0].upper() + word[1:] if not before or before[-1] in ".!?:\n" else word


def _possessive(pattern: re.Pattern[str]) -> re.Pattern[str]:
    return re.compile(pattern.pattern + r"['’]s\b", re.IGNORECASE)


def fix_slips(text: str, reader: Reader | None) -> str:
    """Turn every way ``text`` names its reader into "you" / "your", "the
    principal" into the owner's name on a teammate's brief, and the Executive
    into "I" / "me" / "my"."""
    if reader is None:
        return text
    if reader.owner:
        owner = _pattern([_OWNER_LABEL])
        assert owner is not None
        text = _possessive(owner).sub(lambda m: f"{reader.owner}'s", text)
        text = owner.sub(lambda m: reader.owner or "", text)
    pattern = _pattern([*reader.terms(), *_READER_LABELS, *([] if reader.owner else [_OWNER_LABEL])])
    if pattern is not None:
        text = _possessive(pattern).sub(lambda m: _cased("your", m.start(), m.string), text)
        text = pattern.sub(lambda m: _cased("you", m.start(), m.string), text)
        # The verb after a third-person name: "you has been" → "you have been".
        text = _AGREE.sub(lambda m: m.group(1) + _AGREE_WITH[m.group(2).lower()], text)

    def _me(m: re.Match[str]) -> str:
        if m.group(1):
            return _cased("my", m.start(), m.string)
        if _OBJECT_BEFORE.search(m.string[: m.start()]):
            return "me"
        return "I"

    text = _EXECUTIVE.sub(_me, text)
    return _I_AGREE.sub(lambda m: "I " + _I_AGREE_WITH[m.group(1)], text)


_AGREE_WITH = {"has": "have", "is": "are", "was": "were", "hasn't": "haven't",
               "isn't": "aren't", "wasn't": "weren't", "doesn't": "don't", "does": "do"}
_AGREE = re.compile(r"\b([Yy]ou )(" + "|".join(re.escape(k) for k in _AGREE_WITH) + r")\b")
_I_AGREE_WITH = {"has": "have", "is": "am", "was": "was", "hasn't": "haven't",
                 "isn't": "am not", "doesn't": "don't", "does": "do"}
_I_AGREE = re.compile(r"\bI (" + "|".join(re.escape(k) for k in _I_AGREE_WITH) + r")\b")


__all__ = [
    "READER",
    "Reader",
    "fix_slips",
    "mask",
    "person_reader",
    "principal_reader",
    "reader_line",
    "rewrite_request",
    "slips",
]
