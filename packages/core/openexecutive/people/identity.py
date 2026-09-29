"""Who an email address belongs to: aliases and the company's own domains.

A Person has one primary address (``Person.email``, the one they sign in
with) and any number of aliases (``person_emails``). Mail is matched to its
sender here, in this order:

1. Exact: the address is someone's primary or one of their aliases
   (case-insensitive; the team row wins over a contact).
2. Company domain, team only: on one of the company's own domains, the
   address's local part (lowercased, ``+tag`` dropped) matches the local part
   of a teammate's address on any company domain — anna+invoices@acme.io is
   the Anna whose address is anna@acme.com. A domain alone never proves who
   someone is: an unknown local part on the company domain matches nobody
   (it becomes a roster request, pre-filled as a teammate). When two
   teammates would match, nobody does (fail closed).

The company's domains are the ``company_domains`` workspace setting, or,
when that is unset, the domains of the principal's own addresses — never a
free-mail provider's (gmail.com and the like), where a local part says
nothing about who someone is.

Sign-in is not matched here: ``people.store.find_person_by_email`` (primary
only) is the sign-in lookup, so an alias never grants web access.

The outbound gates use ``RosterAllow``, built from the same rule, so the
Executive may email exactly the addresses inbound mail would match.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openexecutive.people.models import Person

logger = logging.getLogger(__name__)

# Providers where anyone can register any local part. Never a company domain.
FREEMAIL_DOMAINS: frozenset[str] = frozenset({
    "gmail.com", "googlemail.com",
    "outlook.com", "hotmail.com", "hotmail.co.uk", "live.com", "msn.com",
    "yahoo.com", "yahoo.co.uk", "ymail.com", "rocketmail.com",
    "icloud.com", "me.com", "mac.com",
    "aol.com", "aim.com",
    "proton.me", "protonmail.com", "protonmail.ch", "pm.me",
    "gmx.com", "gmx.net", "gmx.de", "web.de", "mail.com", "email.com",
    "zoho.com", "zohomail.com", "yandex.com", "yandex.ru", "mail.ru",
    "qq.com", "163.com", "126.com", "sina.com",
    "fastmail.com", "fastmail.fm", "hey.com", "tutanota.com", "tuta.io",
    "hushmail.com", "mailbox.org", "posteo.de", "duck.com",
})


def normalize_address(addr: str | None) -> str:
    """Trimmed and lowercased — how every match compares an address."""
    return (addr or "").strip().lower()


def split_address(addr: str | None) -> tuple[str, str] | None:
    """(local part, domain) of a normalised address, or None when it has no
    usable ``@``."""
    norm = normalize_address(addr)
    local, sep, domain = norm.rpartition("@")
    if not sep or not local or not domain:
        return None
    return local, domain


def company_local(addr: str | None, domains: Iterable[str]) -> str | None:
    """The address's local part with any ``+tag`` removed, when it is on one
    of ``domains``; else None."""
    parts = split_address(addr)
    if parts is None:
        return None
    local, domain = parts
    if domain not in set(domains):
        return None
    base = local.split("+", 1)[0]
    return base or None


def derive_company_domains(addresses: Iterable[str | None]) -> frozenset[str]:
    """The non-free-mail domains of ``addresses``."""
    out: set[str] = set()
    for addr in addresses:
        parts = split_address(addr)
        if parts is not None and parts[1] not in FREEMAIL_DOMAINS:
            out.add(parts[1])
    return frozenset(out)


def company_domains() -> frozenset[str]:
    """The company's own email domains: the ``company_domains`` workspace
    setting, else derived from the principal's addresses. Empty when neither
    gives one (a principal on gmail.com with no setting). Never raises."""
    try:
        from openexecutive.memory.workspace_settings import get_workspace

        stored = get_workspace().company_domains
        if stored:
            return frozenset(stored)
        from openexecutive.people.store import find_principal_person

        principal = find_principal_person()
    except Exception:
        logger.warning("identity: could not read the company domains", exc_info=True)
        return frozenset()
    if principal is None:
        return frozenset()
    return derive_company_domains([principal.email, *principal.email_aliases])


def addresses_of(person: Person) -> list[str]:
    """Every address a person writes from: their primary and their aliases."""
    return [a for a in [person.email, *person.email_aliases] if a]


def _company_match(
    addr: str, people: Iterable[Person], domains: frozenset[str]
) -> Person | None:
    local = company_local(addr, domains)
    if local is None:
        return None
    matches: dict[int, Person] = {}
    for person in people:
        if person.kind != "team" or person.id is None:
            continue
        if any(company_local(a, domains) == local for a in addresses_of(person)):
            matches[person.id] = person
    if len(matches) > 1:
        logger.warning(
            "identity: a company-domain address matches %d teammates — matching nobody",
            len(matches),
        )
        return None
    return next(iter(matches.values()), None)


def resolve_email_sender(addr: str | None, *, include_contacts: bool = False) -> Person | None:
    """The non-archived Person ``addr`` belongs to (see the module docstring),
    team only unless ``include_contacts``. None for an unknown address."""
    from openexecutive.people.store import find_person_by_address, list_people

    norm = normalize_address(addr)
    if not norm:
        return None
    exact = find_person_by_address(norm, include_contacts=include_contacts)
    if exact is not None:
        return exact
    domains = company_domains()
    if not domains or company_local(norm, domains) is None:
        return None
    return _company_match(norm, list_people(), domains)


def is_company_address(addr: str | None) -> bool:
    """Whether ``addr`` is on one of the company's own domains."""
    return company_local(addr, company_domains()) is not None


class RosterAllow:
    """The outbound allow-list as a set: ``addr in allow`` is True for an
    exact address of someone allowed, or — for a teammate — any address on a
    company domain whose local part matches theirs (``resolve_email_sender``'s
    rule). Used by the Gmail, Calendar and Drive egress gates."""

    def __init__(
        self,
        people: Iterable[Person],
        extra: Iterable[str] = (),
        domains: frozenset[str] | None = None,
    ) -> None:
        self.domains = company_domains() if domains is None else domains
        self.exact: set[str] = {normalize_address(a) for a in extra if a}
        self.company_locals: set[str] = set()
        for person in people:
            for addr in addresses_of(person):
                self.exact.add(normalize_address(addr))
                if person.kind == "team" and self.domains:
                    local = company_local(addr, self.domains)
                    if local:
                        self.company_locals.add(local)

    def __contains__(self, addr: object) -> bool:
        if not isinstance(addr, str):
            return False
        norm = normalize_address(addr)
        if norm in self.exact:
            return True
        local = company_local(norm, self.domains) if self.domains else None
        return local is not None and local in self.company_locals

    def add(self, addr: str) -> None:
        self.exact.add(normalize_address(addr))
