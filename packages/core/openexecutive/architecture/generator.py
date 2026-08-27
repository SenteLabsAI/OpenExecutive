"""Prompt Claude to write one architecture section.

The generator is intentionally narrow: it takes a `SectionSpec` plus a
`FactsBundle`, and returns a `SectionContent` (Markdown prose + an
optional Mermaid string). All persistence/caching lives in
`architecture.cache`; all fact gathering lives in `architecture.facts`.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

import anthropic
import httpx

from openexecutive.architecture.cache import SectionContent, utc_now_iso
from openexecutive.architecture.facts import FactsBundle
from openexecutive.architecture.sections import SectionSpec
from openexecutive.providers import get_provider

logger = logging.getLogger(__name__)

# Cheaper than Opus, plenty for prose + diagram synthesis.
_MODEL = "claude-sonnet-5"
_MAX_TOKENS = 2048

_SYSTEM_PROMPT = """\
You are a senior technical writer documenting the **Open Executive** system — a multi-agent AI executive built on the Anthropic Claude API.

Your job: produce ONE architecture-page section. The page lives at /architecture in the Open Executive web UI. You will be told which section to write, given the section's spec, and given a bundle of authoritative facts about the live system. **Ground every claim in the provided facts. Do not invent components, files, or behaviours.** If a fact you would need is missing, write around it.

# Output contract — STRICT

Respond with **exactly one** JSON object and nothing else. No prose before or after, no Markdown code fences. Schema:

```
{
  "markdown": "<the section body in GitHub-flavoured Markdown — DO NOT include the section title or heading; the UI renders that separately>",
  "mermaid": "<a valid Mermaid diagram, or null if the section spec sets wants_mermaid=false>"
}
```

# Markdown rules

- Open with one short paragraph (1–3 sentences) that frames the section.
- Use `###` for sub-headings if you need them. Never use `#` or `##`.
- Prefer compact bullet lists and short tables over long paragraphs.
- When you name a code symbol, render it as inline `code`. Use full module paths when helpful: `openexecutive.orchestrator.router.route_parallel`.
- Length budget: ~150–300 words. Be dense, not wordy.

# Mermaid rules (when wants_mermaid=true)

- Pick the dialect from the section's `diagram_kind`: `flowchart` (use `flowchart TD` or `flowchart LR`) or `sequence` (use `sequenceDiagram`).
- For flowcharts, apply role tags using `class NodeId roleName;` at the bottom, where roleName is one of: `entry`, `compute`, `storage`, `cache`, `external`, `hot`. The UI appends shared `classDef` rules — do NOT redefine the classes.
- Sequence diagrams must NOT include `classDef` or `class …` lines (Mermaid rejects them on sequence).
- Keep diagrams readable: ≤10 nodes / ≤8 lifelines.

## Strict node-ID and label syntax (parser is unforgiving)

- **Node IDs must be a single token: letters, digits, underscores only.** No spaces, no slashes, no dashes inside the ID. The human-readable text goes in the label.
  - WRONG: `Claude Exec --> Router` (space in ID)
  - WRONG: `Knowledge/RAG --> Executive` (slash in ID)
  - CORRECT: `Claude["Claude Exec"] --> Router`
  - CORRECT: `KB["Knowledge / RAG"] --> Exec`
- **Edge labels go BETWEEN the arrow markers, not after the target node.** Use one of:
  - `A -->|"decisions / advice"| B`     ← preferred when the label contains punctuation
  - `A -- decisions --> B`               ← short labels only, no quotes, no punctuation
  - NEVER `A --> B |"decisions"|`         ← Mermaid rejects label-after-target
- **Quote any label that contains** `:` `(` `)` `/` `,` `&` `#` or starts with a number. Single forward slashes inside a quoted label are fine.
- Use `<br/>` (not `\\n`) inside labels for line breaks.
- One statement per line. End each line with a newline, not a semicolon.

## Minimal valid examples (study these — your diagram must match this shape)

flowchart TD
    User["User"] --> API["FastAPI<br/>port 8000"]
    API --> Exec["Executive"]
    Exec -->|"consult_specialist"| CFO["CFO Agent"]
    Exec -->|"queries"| KB[("ChromaDB")]
    class User entry
    class API,Exec compute
    class CFO compute
    class KB storage

sequenceDiagram
    participant U as User
    participant E as Executive
    participant S as Specialist
    U->>E: ask question
    E->>S: consult_specialist
    S-->>E: analysis
    E-->>U: synthesized reply

# Tone

Technical, concise, direct. No marketing voice. No bullet that just restates the heading. Assume the reader is a software engineer skimming for understanding.
"""


_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# Mermaid is rendered with securityLevel='loose' (required for the <br/>
# line-breaks in node labels). That mode also allows `click` callbacks
# and raw HTML inside labels — both are attacker-reachable now that the
# diagram string comes from the LLM (a hostile KB doc could prompt-inject
# a `click` directive or an `<img onerror=...>` label, which would then
# be cached in SQLite and served to every viewer). Strip both as
# defence-in-depth at generation time.
_CLICK_DIRECTIVE_RE = re.compile(r"^\s*click\s+\S.*$", re.MULTILINE | re.IGNORECASE)
# Allow <br/> and <br> only — they are the documented label line-break
# Mermaid supports. Everything else (img, script, iframe, event handlers
# masquerading as tags) is stripped.
_HTML_TAG_RE = re.compile(r"<(?!/?br\s*/?>)[^>]*>", re.IGNORECASE)


def _sanitise_mermaid(diagram: str) -> str:
    cleaned = _CLICK_DIRECTIVE_RE.sub("", diagram)
    cleaned = _HTML_TAG_RE.sub("", cleaned)
    return cleaned.strip()


def _build_user_payload(spec: SectionSpec, bundle: FactsBundle) -> str:
    """Render the user-turn payload. Compact JSON keeps token cost down."""
    section_dump = spec.model_dump()
    facts_dump = {
        "curated": bundle.curated,
        "agents": [a.model_dump() for a in bundle.agents],
        "workflows": [w.model_dump() for w in bundle.workflows],
        "api_endpoints": [e.model_dump() for e in bundle.api_endpoints],
        "health": bundle.health,
        "code_excerpts": [c.model_dump() for c in bundle.code_excerpts],
        "retrieved_kb": bundle.kb_chunks.get(spec.id, ""),
    }
    return (
        f"<section_spec>\n{json.dumps(section_dump, indent=2)}\n</section_spec>\n\n"
        f"<facts>\n{json.dumps(facts_dump, indent=2, default=str)}\n</facts>\n\n"
        "Now produce the JSON object for this section. Remember: no prose around the JSON."
    )


def _strip_code_fence(text: str) -> str:
    """Defence in depth: even though the prompt forbids fences, peel them
    off if the model wraps the JSON anyway."""
    stripped = text.strip()
    if stripped.startswith("```"):
        first_nl = stripped.find("\n")
        if first_nl != -1:
            stripped = stripped[first_nl + 1 :]
        if stripped.endswith("```"):
            stripped = stripped[: -3]
    return stripped.strip()


def _parse_response(text: str) -> dict[str, Any]:
    cleaned = _strip_code_fence(text)
    cleaned = _CONTROL_CHARS_RE.sub("", cleaned)
    return json.loads(cleaned)


def _validate_mermaid(mermaid: str, spec: SectionSpec) -> bool:
    """Cheap shape check — does the string look like a Mermaid diagram of
    the requested dialect? Real validation happens client-side."""
    head = mermaid.lstrip().splitlines()[0] if mermaid.strip() else ""
    if spec.diagram_kind == "sequence":
        return head.startswith("sequenceDiagram")
    # flowchart
    return head.startswith("flowchart") or head.startswith("graph")


_ANTHROPIC_TIMEOUT_S = 120.0
_BAD_OUTPUT_PREVIEW_CHARS = 1200


def _bad_output_content(spec: SectionSpec, bundle: FactsBundle, raw: str) -> SectionContent:
    """Fallback SectionContent surfaced when the model output is unparseable."""
    return SectionContent(
        section_id=spec.id,
        markdown=(
            "_Generation returned non-JSON output._\n\n"
            f"```\n{raw[:_BAD_OUTPUT_PREVIEW_CHARS]}\n```"
        ),
        mermaid=None,
        facts_hash=bundle.core_hash(),
        generated_at=utc_now_iso(),
    )


def _build_section_content(
    spec: SectionSpec, bundle: FactsBundle, parsed: dict[str, Any]
) -> SectionContent:
    """Shared parse → SectionContent path. Used by both the one-shot and
    streaming generators so they can't drift."""
    markdown = str(parsed.get("markdown") or "").strip()
    mermaid_raw = parsed.get("mermaid")
    mermaid = str(mermaid_raw).strip() if mermaid_raw else None

    if not spec.wants_mermaid or not mermaid or not _validate_mermaid(mermaid, spec):
        mermaid = None
    else:
        mermaid = _sanitise_mermaid(mermaid)

    return SectionContent(
        section_id=spec.id,
        markdown=markdown or "_No content generated._",
        mermaid=mermaid,
        facts_hash=bundle.core_hash(),
        generated_at=utc_now_iso(),
    )


async def generate_section(spec: SectionSpec, bundle: FactsBundle) -> SectionContent:
    """One-shot generation. Retries once if the JSON or Mermaid is invalid."""
    provider = get_provider(_MODEL)
    user_payload = _build_user_payload(spec, bundle)

    for attempt in range(2):
        try:
            resp = await provider.messages_create(
                model=_MODEL,
                max_tokens=_MAX_TOKENS,
                timeout=_ANTHROPIC_TIMEOUT_S,
                system=[
                    {
                        "type": "text",
                        "text": _SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_payload}],
            )
        except (anthropic.APIError, httpx.HTTPError) as exc:
            logger.warning(
                "architecture.generate.api_error section=%s attempt=%d: %s",
                spec.id, attempt, exc,
            )
            if attempt == 1:
                raise
            continue

        text = "".join(
            getattr(block, "text", "")
            for block in resp.content
            if getattr(block, "type", None) == "text"
        )

        try:
            parsed = _parse_response(text)
        except json.JSONDecodeError:
            logger.warning(
                "architecture.generate.bad_json section=%s attempt=%d", spec.id, attempt,
            )
            if attempt == 1:
                return _bad_output_content(spec, bundle, text)
            continue

        mermaid_raw = parsed.get("mermaid")
        mermaid_str = str(mermaid_raw).strip() if mermaid_raw else None
        if (
            spec.wants_mermaid
            and mermaid_str
            and not _validate_mermaid(mermaid_str, spec)
            and attempt == 0
        ):
            logger.warning(
                "architecture.generate.bad_mermaid section=%s attempt=%d", spec.id, attempt,
            )
            user_payload += (
                "\n\nYour previous reply contained a Mermaid block that did not "
                "start with the expected dialect header. Regenerate, ensuring the "
                "first line of `mermaid` is `flowchart TD/LR` or `sequenceDiagram` "
                "as appropriate."
            )
            continue

        return _build_section_content(spec, bundle, parsed)

    # Unreachable — both attempts either return or raise.
    raise RuntimeError("architecture.generate: exhausted retries without resolving")


async def stream_generate_section(
    spec: SectionSpec, bundle: FactsBundle
) -> AsyncIterator[dict[str, Any]]:
    """Streaming variant for the SSE regenerate endpoint.

    Yields:
      - {"type": "delta", "text": "<raw chunk from the model>"}
      - {"type": "done", "content": <SectionContent.model_dump()>}

    On a malformed response the final content falls back to the
    one-shot generator's error path."""
    provider = get_provider(_MODEL)
    user_payload = _build_user_payload(spec, bundle)

    buf: list[str] = []
    async with provider.messages_stream(
        model=_MODEL,
        max_tokens=_MAX_TOKENS,
        timeout=_ANTHROPIC_TIMEOUT_S,
        system=[
            {
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_payload}],
    ) as stream:
        # Consume provider-neutral Anthropic-shape events (the Executive's
        # streaming pattern) rather than the SDK-only `stream.text_stream`
        # convenience — the latter does not exist on the OpenRouter stream,
        # so this path must not depend on it.
        async for event in stream:
            if (
                getattr(event, "type", None) == "content_block_delta"
                and getattr(getattr(event, "delta", None), "type", None) == "text_delta"
            ):
                chunk = event.delta.text
                buf.append(chunk)
                yield {"type": "delta", "text": chunk}

    raw = "".join(buf)
    try:
        parsed = _parse_response(raw)
        content = _build_section_content(spec, bundle, parsed)
    except json.JSONDecodeError:
        content = _bad_output_content(spec, bundle, raw)

    yield {"type": "done", "content": content.model_dump()}
