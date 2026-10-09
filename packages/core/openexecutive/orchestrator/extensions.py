"""Chat tools and document collections an install adds without changing this code.

``OPENEXECUTIVE_EXTENSIONS`` names Python modules, comma-separated. Each has a
``register()`` that calls ``register_tool`` and ``register_collection``; they
are imported once, the first time a turn or the Documents page needs them.

An extension tool is offered only on a turn someone is in:

- never on an unattended run: the scheduler's proactive trigger is refused
  here, and reflection and research build their toolkits from
  ``_ALL_SKILL_TOOLS``, which never holds these;
- never on a turn private to the principal (mail from one of their contacts);
- like every tool Act as me's lockdown has not classified, it is refused once
  the turn has read the owner's mail (``delegation.lockdown`` fails closed);
- and only where the tool's own ``offered(session)`` says so, which should
  depend only on the kind of turn (surface, workspace mode), never on what was
  said, so each kind of turn keeps one cached tool list.

A tool may sit in a ``tool_groups`` group (opened with open_tools) instead of
on the direct list, and may describe its chip. A collection is a kind of
document an extension publishes as a drafted artifact tagged
``collection_tag(name)``: the Documents page gives it its own filter, and the
extension hears when one is archived, restored or deleted, before it happens.
"""
from __future__ import annotations

import importlib
import logging
import re
import threading
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

Change = Literal["archived", "restored", "deleted"]

_TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
# A collection or tool group name.
_COLLECTION_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
_COLLECTION_TAG_PREFIX = "collection:"
_MAX_CHIP_SUMMARY = 120


class ExtensionError(ValueError):
    """A registration this module refuses (bad or taken name, bad definition)."""


@dataclass(frozen=True)
class ExtraTool:
    # The Anthropic tool definition: name, description, input_schema.
    definition: dict[str, Any]
    # Runs the call; returns the tool result text (JSON by convention, with
    # an "error" key on failure, like every other tool here).
    handler: Callable[[dict[str, Any]], Awaitable[str]]
    # A tool_groups group to sit in instead of the direct list. A new group
    # needs ``group_purpose`` (what open_tools says it is for).
    group: str | None = None
    group_purpose: str | None = None
    # Whether a turn on this session is offered the tool (see module doc).
    offered: Callable[[Any], bool] | None = None
    # The chip for a successful call: (tool input, parsed JSON result or
    # None) -> {"summary", "target", "link"} or None for no chip. A link must
    # be a path inside the app ("/artifacts/alert:12").
    chip: Callable[[dict[str, Any], dict[str, Any] | None], dict[str, Any] | None] | None = None

    @property
    def name(self) -> str:
        return str(self.definition.get("name", ""))


@dataclass(frozen=True)
class Collection:
    name: str
    # The Documents filter's label, e.g. "Reports".
    label: str
    # Told before one of its documents is archived, restored or deleted:
    # (artifact id, change). If it raises, the change does not happen.
    on_change: Callable[[str, Change], Awaitable[None]] | None = None


_tools: dict[str, ExtraTool] = {}
_collections: dict[str, Collection] = {}
_loaded = False
_load_lock = threading.Lock()


def _builtin_tool_names() -> frozenset[str]:
    from openexecutive.orchestrator.executive import builtin_tool_names

    return builtin_tool_names()


def register_tool(tool: ExtraTool) -> None:
    """Add a chat tool. Refuses a bad or taken name and a definition without a
    description or an object input schema."""
    name = tool.name
    if not _TOOL_NAME.fullmatch(name):
        raise ExtensionError(f"tool name {name!r} must be lowercase letters, digits and _")
    if name in _tools or name in _builtin_tool_names():
        raise ExtensionError(f"tool name {name!r} is already taken")
    if not str(tool.definition.get("description") or "").strip():
        raise ExtensionError(f"tool {name!r} needs a description")
    schema = tool.definition.get("input_schema")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ExtensionError(f"tool {name!r} needs an object input_schema")
    if tool.group is not None:
        from openexecutive.orchestrator import tool_groups

        if not _COLLECTION_NAME.fullmatch(tool.group):
            raise ExtensionError(f"group name {tool.group!r} must be lowercase letters, digits and _")

        tool_groups.add_to_group(tool.group, tool.group_purpose, name)
    _tools[name] = tool


def register_collection(collection: Collection) -> None:
    """Add a kind of document with its own Documents filter."""
    if not _COLLECTION_NAME.fullmatch(collection.name):
        raise ExtensionError(
            f"collection name {collection.name!r} must be lowercase letters, digits and _"
        )
    if collection.name in _collections:
        raise ExtensionError(f"collection {collection.name!r} is already registered")
    if not collection.label.strip():
        raise ExtensionError(f"collection {collection.name!r} needs a label")
    _collections[collection.name] = collection


def load() -> None:
    """Import each module ``OPENEXECUTIVE_EXTENSIONS`` names and call its
    ``register()``, once per process. A module that fails is logged and
    skipped, so a bad extension never stops the install from starting."""
    global _loaded
    if _loaded:
        return
    with _load_lock:
        if _loaded:
            return
        from openexecutive.config import get_settings

        names = [n for n in (get_settings().extensions or "").split(",") if n]
        for module_name in names:
            try:
                module = importlib.import_module(module_name)
                register = getattr(module, "register", None)
                if not callable(register):
                    raise ExtensionError(f"{module_name} has no register()")
                register()
            except Exception:
                logger.exception("extension %s failed to load; skipped", module_name)
        _loaded = True


def offered_tools(session: Any) -> list[ExtraTool]:
    """The extension tools a turn on ``session`` is offered (the caller has
    already excluded unattended and private turns). A failing ``offered``
    counts as not offered."""
    load()
    offered: list[ExtraTool] = []
    for tool in _tools.values():
        if tool.offered is not None:
            try:
                if not tool.offered(session):
                    continue
            except Exception:
                logger.exception("extension tool %s: offered() failed", tool.name)
                continue
        offered.append(tool)
    return offered


def has_chip(tool_name: str) -> bool:
    tool = _tools.get(tool_name)
    return tool is not None and tool.chip is not None


def chip_fields(
    tool_name: str, tool_input: dict[str, Any], parsed: dict[str, Any] | None
) -> dict[str, Any] | None:
    """The tool's chip fields (summary, target, link), checked: None when it
    has no chip, declines, raises or gives no summary. A link that is not a
    path inside the app is dropped."""
    tool = _tools.get(tool_name)
    if tool is None or tool.chip is None:
        return None
    try:
        raw = tool.chip(tool_input, parsed)
    except Exception:
        logger.exception("extension tool %s: chip() failed", tool_name)
        return None
    if not isinstance(raw, dict):
        return None
    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None
    target = raw.get("target")
    link = raw.get("link")
    return {
        "summary": summary.strip()[:_MAX_CHIP_SUMMARY],
        "target": target[:200] if isinstance(target, str) and target else None,
        "link": link if _is_app_path(link) else None,
    }


def _is_app_path(link: Any) -> bool:
    return (
        isinstance(link, str)
        and link.startswith("/")
        and not link.startswith("//")
        and "\\" not in link
        and not any(ch.isspace() or ord(ch) < 32 for ch in link)
        and len(link) <= 500
    )


def collection_tag(name: str) -> str:
    """The topic tag that puts a drafted artifact in collection ``name``."""
    return f"{_COLLECTION_TAG_PREFIX}{name}"


def collection_for_tags(tags: Iterable[str] | None) -> Collection | None:
    """The registered collection a drafted artifact's topic tags name, if any."""
    if not tags:
        return None
    load()
    for tag in tags:
        if isinstance(tag, str) and tag.startswith(_COLLECTION_TAG_PREFIX):
            found = _collections.get(tag[len(_COLLECTION_TAG_PREFIX):])
            if found is not None:
                return found
    return None


def get_collection(name: str | None) -> Collection | None:
    if not name:
        return None
    load()
    return _collections.get(name)


async def notify(collection_name: str | None, artifact_id: str, change: Change) -> None:
    """Tell a document's collection it is about to be archived, restored or
    deleted. Raises whatever the collection raises, so the caller can stop."""
    collection = get_collection(collection_name)
    if collection is not None and collection.on_change is not None:
        await collection.on_change(artifact_id, change)


def _reset_for_tests() -> None:
    """Forget every registration (tests only)."""
    global _loaded
    from openexecutive.orchestrator import tool_groups

    for tool in _tools.values():
        if tool.group is not None:
            tool_groups.remove_from_group(tool.group, tool.name)
    _tools.clear()
    _collections.clear()
    _loaded = False


__all__ = [
    "Change",
    "Collection",
    "ExtensionError",
    "ExtraTool",
    "chip_fields",
    "collection_for_tags",
    "collection_tag",
    "get_collection",
    "has_chip",
    "load",
    "notify",
    "offered_tools",
    "register_collection",
    "register_tool",
]
