"""What tools a workflow action step can use, and how to reach them.

Two sources, one shape (:class:`ToolInfo`):

* **MCP tools** — everything the MCP gateway exposes (Google Workspace, fetch,
  any server in ``mcp_servers.json``). The gateway (extensible-mcp) only lets a
  session call a tool that one of its ``search_tools`` results has returned,
  so :func:`resolve` doubles as that per-session discovery: every tool a step
  names is searched for (exact-name match) before the step runs.
* **Built-ins** — a small fixed set prefixed ``oe__`` so they can never collide
  with an MCP name (those are always ``server__tool``). They cover what no MCP
  server does for us: reading a file another tool downloaded, and reaching a
  person or the alert queue through Open Executive's own guarded paths.

The step's tool allowlist is the user's approval (they see it on the review
card before clicking Create), so this module never widens it: it only answers
"does this exact name exist, and what is its schema?".
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openexecutive.config import get_settings

if TYPE_CHECKING:
    from openexecutive.orchestrator.mcp_gateway import MCPGateway
    from openexecutive.workflows.dynamic_models import DynamicWorkflowDef

logger = logging.getLogger(__name__)

BUILTIN_PREFIX = "oe__"

# How many gateway results to ask for when resolving one exact name. The
# gateway ranks by embedding similarity, so the exact tool is normally first;
# a wider net keeps a near-duplicate name from pushing it out.
_RESOLVE_TOP_K = 10
_SEARCH_TOP_K = 8
_MAX_READ_FILE_BYTES = 25 * 1024 * 1024
_READABLE_SUFFIXES = frozenset({".pdf", ".docx", ".doc", ".xlsx", ".xlsm", ".csv", ".md", ".txt"})

# Verbs that mark a tool as read-only for the review card's "writes" badge.
# The gateway reports no read-only hints, so this is a LABEL for the human,
# never a security control — the allowlist and the gateway's own gates are.
_READ_VERBS = (
    "get_", "list_", "search_", "read_", "query_", "find_",
    "check_", "describe_", "inspect_", "debug_",
)
# A tool that takes a URL can carry data OUT (the URL itself, or a request
# body), whatever its name says — e.g. `fetch__fetch`. Never label one as
# reads-only: the review card must show the user it can reach the outside.
_EGRESS_PARAM_HINTS = ("url", "uri", "endpoint", "webhook")


@dataclass(frozen=True)
class ToolInfo:
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    # True: only reads. None: may change something (unknown). Never False —
    # we can't prove a tool writes, only that its name says it reads.
    read_only: bool | None = None
    source: str = "mcp"  # "mcp" | "builtin"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "read_only": self.read_only,
            "source": self.source,
        }

    def as_anthropic_tool(self) -> dict[str, Any]:
        schema = self.input_schema or {"type": "object", "properties": {}}
        if schema.get("type") != "object":
            schema = {"type": "object", "properties": {}}
        return {"name": self.name, "description": self.description[:1024], "input_schema": schema}


class ToolCatalogError(RuntimeError):
    """A tool lookup could not be completed (e.g. no gateway). Fixed-string."""


def _read_only_label(name: str, input_schema: dict[str, Any] | None = None) -> bool | None:
    props = (input_schema or {}).get("properties")
    if isinstance(props, dict) and any(
        hint in str(key).lower() for key in props for hint in _EGRESS_PARAM_HINTS
    ):
        return None
    bare = name.split("__", 1)[-1].lower()
    return True if bare.startswith(_READ_VERBS) else None


# ── MCP: parse the gateway's search_tools output ────────────────────────────
#
# extensible-mcp (pinned in mcp_gateway.py) returns Markdown, one block per
# tool:
#
#   ## google_workspace__read_sheet_values
#   **Description:** Reads values from a range…
#   **Parameters:**
#   ```json
#   { …JSON schema… }
#   ```
#   **Similarity:** 0.812
#
# or "No matching tools found…" when nothing matches.

# A block starts at a "## name" line IMMEDIATELY followed by the
# "**Description:**" line — a bare "## Heading" inside a multi-line tool
# description is not a new tool.
_BLOCK_RE = re.compile(
    r"^## (?P<name>\S+)[ \t]*\n\*\*Description:\*\*[ \t]?(?P<desc>[^\n]*)", re.MULTILINE
)
# The schema is the fenced JSON right after the LAST "**Parameters:**" line of
# the block, so a ```json example inside a description is never taken for it.
_PARAMS_MARKER = "\n**Parameters:**\n"
_SCHEMA_RE = re.compile(r"\A```json[ \t]*\n(?P<schema>.*?)\n```", re.DOTALL)


def parse_search_results(text: str) -> list[ToolInfo]:
    """Parse extensible-mcp's ``search_tools`` Markdown into ToolInfo records."""
    if not text:
        return []
    matches = list(_BLOCK_RE.finditer(text))
    tools: list[ToolInfo] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[m.end():end]
        name = m.group("name")
        schema: dict[str, Any] = {}
        cut = block.rfind(_PARAMS_MARKER)
        schema_match = (
            _SCHEMA_RE.match(block[cut + len(_PARAMS_MARKER):]) if cut >= 0 else None
        )
        if schema_match:
            try:
                parsed = json.loads(schema_match.group("schema"))
                if isinstance(parsed, dict):
                    schema = parsed
            except ValueError:
                logger.warning("tool_catalog: unparseable schema for %s", name)
        tools.append(
            ToolInfo(
                name=name,
                description=m.group("desc").strip(),
                input_schema=schema,
                read_only=_read_only_label(name, schema),
                source="mcp",
            )
        )
    return tools


def _gateway() -> MCPGateway | None:
    from openexecutive.orchestrator.mcp_gateway import get_active_gateway

    return get_active_gateway()


async def _gateway_search(gateway: MCPGateway, query: str, top_k: int) -> list[ToolInfo]:
    try:
        text = await gateway.search_tools({"query": query, "top_k": top_k})
    except Exception as exc:
        logger.warning("tool_catalog: gateway search failed (%s)", type(exc).__name__)
        return []
    return parse_search_results(text)


# ── Built-ins ────────────────────────────────────────────────────────────────

Handler = Callable[[dict[str, Any]], Awaitable[str]]


def _allowed_file_dirs() -> list[Path]:
    dirs = list(get_settings().workflow_file_dirs)
    if not dirs:
        env = os.environ.get("WORKSPACE_ATTACHMENT_DIR", "").strip()
        dirs = [env or str(Path("~/.workspace-mcp/attachments"))]
    out: list[Path] = []
    for d in dirs:
        try:
            out.append(Path(d).expanduser().resolve())
        except (OSError, RuntimeError):
            continue
    return out


def _err(message: str) -> str:
    return json.dumps({"error": message})


async def _read_file(tool_input: dict[str, Any]) -> str:
    """Extract text from a file a previous tool call downloaded.

    Confined to the configured download directories: the path is resolved
    (symlinks followed, ``..`` collapsed) BEFORE the containment check, so
    neither can escape it.
    """
    from openexecutive.knowledge.loader import extract_text_from_file

    raw = str(tool_input.get("path") or "").strip()
    if not raw:
        return _err("path is required — pass the file path a previous tool returned")
    try:
        resolved = Path(raw).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return _err("file not found")
    allowed = _allowed_file_dirs()
    if not any(resolved.is_relative_to(d) for d in allowed):
        return _err(
            "that file is outside the folders workflows may read "
            "(downloaded attachments only)"
        )
    if not resolved.is_file():
        return _err("not a file")
    if resolved.suffix.lower() not in _READABLE_SUFFIXES:
        return _err(
            "unsupported file type — readable: " + ", ".join(sorted(_READABLE_SUFFIXES))
        )
    if resolved.stat().st_size > _MAX_READ_FILE_BYTES:
        return _err("file is larger than 25 MB")
    try:
        text = await asyncio.to_thread(extract_text_from_file, resolved)
    except Exception as exc:
        logger.warning("oe__read_file: extraction failed (%s)", type(exc).__name__)
        return _err("could not read that file")
    if not text.strip():
        return _err("no text could be extracted (it may be a scanned image)")
    cap = get_settings().tool_result_max_chars
    return text if len(text) <= cap else text[:cap] + "\n…[truncated]"


async def _message_person(tool_input: dict[str, Any]) -> str:
    from openexecutive.orchestrator.schedule_tools import handle_message_person

    return await handle_message_person(tool_input)


async def _create_alert(tool_input: dict[str, Any]) -> str:
    from openexecutive.orchestrator.alert_tools import handle_create_alert

    return await handle_create_alert(tool_input)


def _builtin_specs() -> dict[str, tuple[ToolInfo, Handler]]:
    from openexecutive.orchestrator.alert_tools import CREATE_ALERT_TOOL
    from openexecutive.orchestrator.schedule_tools import MESSAGE_PERSON_TOOL

    return {
        "oe__read_file": (
            ToolInfo(
                name="oe__read_file",
                description=(
                    "Read the text of a PDF, Word, Excel, CSV, Markdown or text "
                    "file that another tool downloaded (e.g. an email "
                    "attachment). Pass the file path that tool returned."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute file path."}
                    },
                    "required": ["path"],
                },
                read_only=True,
                source="builtin",
            ),
            _read_file,
        ),
        "oe__message_person": (
            ToolInfo(
                name="oe__message_person",
                description=MESSAGE_PERSON_TOOL["description"],
                input_schema=MESSAGE_PERSON_TOOL["input_schema"],
                read_only=None,
                source="builtin",
            ),
            _message_person,
        ),
        "oe__create_alert": (
            ToolInfo(
                name="oe__create_alert",
                description=CREATE_ALERT_TOOL["description"],
                input_schema=CREATE_ALERT_TOOL["input_schema"],
                read_only=None,
                source="builtin",
            ),
            _create_alert,
        ),
    }


def builtin_tools() -> list[ToolInfo]:
    return [info for info, _ in _builtin_specs().values()]


def builtin_handler(name: str) -> Handler | None:
    spec = _builtin_specs().get(name)
    return spec[1] if spec else None


# ── Public API ───────────────────────────────────────────────────────────────


async def search(query: str) -> list[ToolInfo]:
    """Tools matching a plain-language query: built-ins first, then the gateway."""
    words = [w for w in re.split(r"\W+", query.lower()) if len(w) > 2]
    builtins = [
        t for t in builtin_tools()
        if not words or any(w in f"{t.name} {t.description}".lower() for w in words)
    ]
    gateway = _gateway()
    mcp = await _gateway_search(gateway, query, _SEARCH_TOP_K) if gateway else []
    return [*builtins, *mcp]


async def resolve(names: list[str]) -> dict[str, ToolInfo]:
    """Exact-name lookup. Returns only names that exist; the caller diffs.

    Searching for each MCP name also registers it with the gateway's
    discovered-tools filter, which ``call_tool`` requires.
    """
    found: dict[str, ToolInfo] = {}
    builtins = _builtin_specs()
    mcp_names = []
    for name in names:
        if name in builtins:
            found[name] = builtins[name][0]
        elif not name.startswith(BUILTIN_PREFIX):
            mcp_names.append(name)
    if not mcp_names:
        return found
    gateway = _gateway()
    if gateway is None:
        return found
    for name in mcp_names:
        # "google_workspace__append_table_rows" -> "google workspace append table rows"
        query = re.sub(r"[_\-]+", " ", name).strip()
        for info in await _gateway_search(gateway, query, _RESOLVE_TOP_K):
            if info.name == name:
                found[name] = info
                break
    return found


def gateway_available() -> bool:
    return _gateway() is not None


async def validate_definition_and_tools(defn: DynamicWorkflowDef) -> list[str]:
    """Every save-time check: structural rules, then (if those pass) tool
    availability. The one entry point for every path that stores a
    definition, so none can forget the async half."""
    from openexecutive.workflows.dynamic_models import validate_definition

    return validate_definition(defn) or await validate_tools_available(defn)


async def unavailable_step_tools(defn: DynamicWorkflowDef) -> list[str]:
    """Names of action-step tools that do not resolve right now (sorted)."""
    from openexecutive.workflows.dynamic_models import ActionStepSpec

    wanted = sorted({t for s in defn.steps if isinstance(s, ActionStepSpec) for t in s.tools})
    if not wanted:
        return []
    found = await resolve(wanted)
    return [t for t in wanted if t not in found]


async def validate_tools_available(defn: DynamicWorkflowDef) -> list[str]:
    """Errors for every action-step tool that does not resolve (empty == OK).

    Runs at save time (and on designer drafts) so a workflow can't be stored
    with a tool the system can't reach — a typo, a hallucinated name, or a
    tool the gateway's deny-list filters out.
    """
    missing = await unavailable_step_tools(defn)
    if not missing:
        return []
    needs_gateway = [t for t in missing if not t.startswith(BUILTIN_PREFIX)]
    if needs_gateway and not gateway_available():
        return [
            "these tools need the MCP tool gateway, which is not running: "
            + ", ".join(needs_gateway)
        ]
    return ["unknown or unavailable tools: " + ", ".join(missing)]
