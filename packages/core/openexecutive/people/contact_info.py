"""A person's contact details besides email and the chat apps
(``Person.details``, table ``person_details``): phone, mobile, website,
LinkedIn, another social profile, postal address. For the reader only, never
a channel the Executive sends on.

They come from someone typing them (People, chat) or from a person's own
email signature (``people.contact_keeper``). ``clean_detail`` is how any of
them is stored. ``detail_from_text`` is the stricter check for a signature:
the model's pick is kept only when the mail itself writes it, a website only
on the domain the person's own address is on, and a profile only on a known
site.
"""
from __future__ import annotations

import re

PHONE_MAX = 40
URL_MAX = 200
ADDRESS_MAX = 160
ADDRESS_WORDS = 14

PHONE_KINDS = frozenset({"phone", "mobile"})
# Profile sites a "social" link may be on (LinkedIn has its own kind).
SOCIAL_HOSTS = frozenset({
    "x.com", "twitter.com", "github.com", "instagram.com", "facebook.com", "bsky.app",
    "youtube.com", "threads.net", "mastodon.social", "tiktok.com",
})
LINKEDIN_HOSTS = frozenset({"linkedin.com"})

# A label written before a number ("c: 256 555 0100", "Direct: (803) 555-0101",
# "Tel. +44 20 7946 0000"): dropped, the number kept.
_PHONE_LABEL = re.compile(r"^[^\W\d_]{1,12}(?:\s+[^\W\d_]{1,12})?\s*[:.\-]\s*(?=[+(\d])", re.UNICODE)
_PHONE_OK = re.compile(r"^\+?[\d\s().\-/]+(?:\s*(?:x|ext\.?)\s*\d{1,6})?$", re.IGNORECASE)
_HOST = re.compile(r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_PATH = re.compile(r"/[\w\-./~%@]*")
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_BANNED = re.compile(r"https?:|www\.|@|<|>|\b(ignore|instruction|prompt|system|assistant)\b", re.IGNORECASE)


def clean_phone(raw: str | None) -> str:
    """A phone number written plainly ("+1 (555) 010-0100", "555 0100 ext 2"),
    or "" when it is not one: 7 to 15 digits, nothing but digits, spaces,
    + ( ) . - / and an extension, after any label like "Mobile:"."""
    text = " ".join((raw or "").split()).strip(" ,;:")
    text = _PHONE_LABEL.sub("", text, count=1)
    if not text or len(text) > PHONE_MAX or not _PHONE_OK.match(text):
        return ""
    main = re.split(r"(?i)\s*(?:x|ext\.?)\s*\d", text)[0]
    digits = re.sub(r"\D", "", main)
    return text if 7 <= len(digits) <= 15 else ""


def url_host(raw: str | None) -> str:
    """The host of a web address as typed ("https://www.northwind.example/team"
    gives "www.northwind.example"), or "" when it is not one."""
    text = (raw or "").strip().lower()
    text = re.sub(r"^https?://", "", text)
    host = re.split(r"[/?#]", text, maxsplit=1)[0].rstrip(".")
    if ":" in host or "@" in host:
        return ""
    return host if _HOST.match(host) else ""


def clean_url(raw: str | None) -> str:
    """A web address as stored: ``https://``, its host and a plain path
    (no query); "" when it is not a plain web address."""
    text = (raw or "").strip()
    if not text or len(text) > URL_MAX or any(c.isspace() for c in text):
        return ""
    lowered = text.lower()
    if "://" in lowered and not lowered.startswith(("http://", "https://")):
        return ""
    host = url_host(text)
    if not host:
        return ""
    rest = re.sub(r"^https?://", "", text, flags=re.IGNORECASE)[len(host):]
    rest = re.split(r"[?#]", rest, maxsplit=1)[0].rstrip("/")
    if rest and not _PATH.fullmatch(rest):
        rest = ""
    return f"https://{host}{rest}"


def _site(host: str) -> str:
    return host.removeprefix("www.").removeprefix("m.")


def _on(host: str, sites: frozenset[str]) -> bool:
    site = _site(host)
    return any(site == s or site.endswith("." + s) for s in sites)


def clean_address(raw: str | None) -> str:
    """A postal address on one line, or "": a few words with a number, no
    link, address or markup."""
    text = ", ".join(p.strip() for p in (raw or "").replace("|", "\n").splitlines() if p.strip())
    text = " ".join(text.split()).strip(" ,;")
    if not text or len(text) > ADDRESS_MAX or _BANNED.search(text):
        return ""
    words = _WORD.findall(text)
    if not 3 <= len(words) <= ADDRESS_WORDS or not any(c.isdigit() for c in text) or not any(c.isalpha() for c in text):
        return ""
    return text


def clean_detail(kind: str, raw: str | None) -> str:
    """``raw`` as a stored detail of ``kind``, or "" when it is not one."""
    if kind in PHONE_KINDS:
        return clean_phone(raw)
    if kind == "address":
        return clean_address(raw)
    url = clean_url(raw)
    host = url_host(url)
    if kind == "website":
        return url if host and not _on(host, LINKEDIN_HOSTS | SOCIAL_HOSTS) else ""
    if kind == "linkedin":
        return url if host and _on(host, LINKEDIN_HOSTS) and len(url.split("/")) > 3 else ""
    if kind == "social":
        return url if host and _on(host, SOCIAL_HOSTS) and len(url.split("/")) > 3 else ""
    return ""


def same_site(host: str, sender_host: str) -> bool:
    """Whether a website's host is the sender's mail domain or one of its
    subdomains. Not a parent of the mail domain (mail from corp.co.uk never
    vouches for co.uk), and never a free mail domain."""
    from openexecutive.people.identity import FREEMAIL_DOMAINS

    site = _site(host)
    if not site or not sender_host or sender_host in FREEMAIL_DOMAINS or site in FREEMAIL_DOMAINS:
        return False
    return site == sender_host or site.endswith("." + sender_host)


def _phone_in(phone: str, text: str) -> bool:
    digits = re.sub(r"\D", "", re.split(r"(?i)\s*(?:x|ext\.?)\s*\d", phone)[0])
    return any(re.sub(r"\D", "", found) == digits for found in re.findall(r"\+?\d[\d\s().\-/]{5,}\d", text))


def detail_from_text(kind: str, candidate: str, text: str, sender: str) -> str:
    """``candidate`` as a stored detail of ``kind``, or "" unless the mail
    writes it: a phone's digits as one number, every word of an address, a
    link's host and path. A website must be on ``sender``'s own domain."""
    value = clean_detail(kind, candidate)
    if not value:
        return ""
    lowered = text.lower()
    if kind in PHONE_KINDS:
        return value if _phone_in(value, text) else ""
    if kind == "address":
        have = {w.casefold() for w in _WORD.findall(text)}
        return value if all(w.casefold() in have for w in _WORD.findall(value)) else ""
    host = url_host(value)
    if kind == "website" and not same_site(host, (sender or "").rpartition("@")[2].lower()):
        return ""
    written = _site(host) + value.split(host, 1)[1]
    return value if written.lower() in lowered else ""
