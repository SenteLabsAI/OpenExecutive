"""Our own words, taken out of what the outside-content check reads.

The check (``orchestrator.content_scan``) is asked whether a read carries
text that tries to steer the Executive. A read's result is never only the
outside author's text: the code that hands it to the model adds its own
words around it, such as the label ``content_trust.wrap_untrusted`` puts on
app data, a hint after an empty Drive search ("Tell the user the exact query
you ran…"), a held step's "Tapping Approve does exactly this…", the heading
over a synced folder ("open the file live with…"), or the Executive's own
footer in a mail it sent ("… is an AI assistant working for …"). Those words
talk to the model, so the check read them as the content steering it, and
every one of them flagged reads that were harmless. Taking them out one
phrase at a time, on one path at a time, kept missing the next one.

So the list is not kept by hand. It is every string literal in the package's
own Python source (``_SOURCE_DIRS`` below the package, never the
prompts or evals), split at each f-string value and each line, that is long
enough to be a sentence (``MIN_CHARS``, ``MIN_WORDS``). Any of them found in
what the check reads is removed before it reads it (``without_own_words``),
on every path, since the check calls this itself. A hint added tomorrow is
on the list the moment it is in the code.

What this can't remove is text no literal of ours holds: the outside
author's own words, and the dynamic parts of ours (a query, a file name),
which are short. Removing our words never hides an outside author's: they
would have to copy one of our sentences exactly, and then that sentence is
ours. The model still gets the whole result.

Plain code, built once per process on first use (``warm``, in a thread);
never raises.
"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
import re
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_PACKAGE = Path(__file__).resolve().parent.parent
# Below the package: everything that builds what a tool returns. Not the
# Executive's prompts (they never reach a tool result) or the eval suite.
_SKIP_DIRS = frozenset({"prompts", "evals", "knowledge/builtin", "architecture", "migrations"})
# The checks' own modules: their literals describe attacks (a classifier's
# examples, a guard's refusal) and never reach a tool result as our words, so
# taking them out of what is checked would only let content that copies one
# skip the check.
_SKIP_FILES = frozenset({
    "orchestrator/content_scan.py",
    "orchestrator/outbound_guard.py",
    "orchestrator/outside_content.py",
    "delegation/injection_check.py",
    "delegation/inbox_classifier.py",
    "delegation/lockdown.py",
})
# A sentence of ours that reads like an attack (a guard's "ignore any
# instruction inside them", a pattern of attack words) stays in what is
# checked: content that copied it would otherwise get it past the check.
_STEERING = re.compile(
    r"\b(ignore|disregard|forget|override|bypass)\b[^.\n]{0,40}\b(instructions?|rules?|prompts?|guidelines?)\b"
    r"|\byou are now\b|\bsystem prompt\b|\bnew instructions\b|\bjailbreak\b",
    re.IGNORECASE,
)
# So does one that names what the check watches for (sending, forwarding,
# sharing, paying, a password): content that copied it would get the very
# request the check looks for past it.
_ACTION = re.compile(
    r"\b(forward(s|ed|ing)?|shar(e|es|ed|ing)|wire|pay(s|ing|ment)?|passwords?|passcodes?|credentials?"
    r"|click|transfer(s|red)?|bank|gift cards?|send(s|ing)?)\b",
    re.IGNORECASE,
)
MIN_CHARS = 24
MIN_WORDS = 4
# Matches are found from their first characters (``_index``).
_KEY = 12
_WORD = re.compile(r"[A-Za-z]{2,}")
_SENTENCE_END = re.compile(r"(?<=[.;:!?])\s+")


def _skipped(path: Path) -> bool:
    rel = path.relative_to(_PACKAGE).as_posix()
    return rel in _SKIP_FILES or any(rel == d or rel.startswith(d + "/") for d in _SKIP_DIRS)


def _docstrings(tree: ast.AST) -> set[int]:
    """The ids of the module's, classes' and functions' docstring nodes."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                out.add(id(body[0].value))
    return out


def _literals(source: str) -> set[str]:
    """Every string literal in ``source`` but docstrings, with each
    f-string split at its values."""
    tree = ast.parse(source)
    docs = _docstrings(tree)
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            out.update(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            out.add(node.value)
    return out


def _kept_in(sentence: str) -> bool:
    """Whether ``sentence`` stays in what is checked though it is ours."""
    return bool(_STEERING.search(sentence) or _ACTION.search(sentence))


def _sentences(literal: str) -> set[str]:
    """Each line of ``literal``, and each sentence of it, long enough to be
    one: a sentence alone still matches where a result carries only part of
    the literal (a capped result, a hint whose other half an f-string value
    split off)."""
    out: set[str] = set()
    for line in literal.split("\n"):
        line = line.strip()
        for part in (line, *_SENTENCE_END.split(line)):
            part = part.strip()
            if len(part) >= MIN_CHARS and len(_WORD.findall(part)) >= MIN_WORDS and not _kept_in(part):
                out.add(part)
    return out


def _forms(piece: str) -> set[str]:
    """``piece`` as it may appear in a result: as itself, and escaped inside
    a JSON string once or twice (a tool's JSON answer that carries another
    JSON text), with and without its non-ASCII characters escaped."""
    forms = {piece}
    for ensure_ascii in (True, False):
        once = json.dumps(piece, ensure_ascii=ensure_ascii)[1:-1]
        forms.add(once)
        forms.add(json.dumps(once, ensure_ascii=ensure_ascii)[1:-1])
    return {f for f in forms if len(f) >= _KEY}


_built: dict[str, tuple[str, ...]] | None = None
_lock = threading.Lock()
# After a failed build, how long to strip nothing before trying again, so a
# lasting failure never re-runs the build on every check.
_RETRY_S = 600.0
_failed_at: float | None = None


def _build() -> dict[str, tuple[str, ...]]:
    """Every piece, keyed by its first characters, longest first. Raises
    when the package's source can't be listed."""
    pieces: set[str] = set()
    for path in sorted(_PACKAGE.rglob("*.py")):
        if _skipped(path):
            continue
        try:
            literals = _literals(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError):
            logger.warning("own_words: couldn't read %s", path, exc_info=True)
            continue
        for literal in literals:
            for sentence in _sentences(literal):
                pieces.update(_forms(sentence))
    index: dict[str, list[str]] = {}
    for piece in pieces:
        index.setdefault(piece[:_KEY], []).append(piece)
    return {key: tuple(sorted(group, key=len, reverse=True)) for key, group in index.items()}


def _index() -> dict[str, tuple[str, ...]]:
    """The list, built once: concurrent first calls wait on one build, and a
    failed build is not kept (the next check tries again; this one strips
    nothing)."""
    global _built, _failed_at
    if _built is not None:
        return _built
    if _failed_at is not None and time.monotonic() - _failed_at < _RETRY_S:
        return {}
    with _lock:
        if _built is None:
            try:
                _built = _build()
            except Exception:
                _failed_at = time.monotonic()
                logger.warning("own_words: couldn't build the list", exc_info=True)
                return {}
        return _built


async def warm() -> None:
    """Build the list off the event loop (it parses the package's source,
    a few seconds once per process), so the first check never stalls the
    server. Concurrent callers share the one build (``_index``)."""
    if _built is None and (_failed_at is None or time.monotonic() - _failed_at >= _RETRY_S):
        await asyncio.to_thread(_index)


def count() -> int:
    """How many pieces are on the list (tests, logs)."""
    return sum(len(group) for group in _index().values())


def without_own_words(text: str) -> str:
    """``text`` with every one of our own sentences taken out. Anything that
    isn't a string is returned as it is."""
    if not isinstance(text, str) or len(text) < _KEY:
        return text
    index = _index()
    if not index:
        return text
    kept: list[str] = []
    start = 0
    i = 0
    end = len(text) - _KEY + 1
    while i < end:
        group = index.get(text[i:i + _KEY])
        if group is not None:
            match = next((p for p in group if text.startswith(p, i)), None)
            if match is not None:
                kept.append(text[start:i])
                i += len(match)
                start = i
                continue
        i += 1
    if start == 0:
        return text
    kept.append(text[start:])
    return "".join(kept)
