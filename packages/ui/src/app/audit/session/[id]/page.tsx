"use client";

import "@xyflow/react/dist/style.css";

import Dagre from "@dagrejs/dagre";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Background,
  Controls,
  type Edge,
  Handle,
  MarkerType,
  MiniMap,
  type Node,
  type NodeProps,
  Position,
  ReactFlow,
} from "@xyflow/react";

import Icon from "@/components/Icon";
import {
  getAuditLog,
  getAuditSession,
  type AuditEvent,
  type AuditEventDetail,
  type AuditGraph,
  type AuditGraphNode,
  type AuditSessionResponse,
} from "@/lib/api";

// ---------------------------------------------------------------------------
// Visual tokens — color per node kind. Matches and extends the palette
// already used by /audit (TYPE_COLORS). Tailwind needs literal class strings
// so they're enumerated here; the lookup falls back gracefully for unknown
// kinds (future event types still render, just with neutral styling).
// ---------------------------------------------------------------------------

const KIND_COLORS: Record<string, { ring: string; bg: string; text: string; label: string }> = {
  inbound: {
    ring: "ring-emerald-500/40",
    bg: "bg-emerald-500/20",
    text: "text-emerald-200",
    label: "Inbound",
  },
  memory: {
    ring: "ring-rose-500/40",
    bg: "bg-rose-500/15",
    text: "text-rose-200",
    label: "Memory",
  },
  knowledge: {
    ring: "ring-sky-500/40",
    bg: "bg-sky-500/15",
    text: "text-sky-200",
    label: "Knowledge",
  },
  specialist: {
    ring: "ring-violet-500/40",
    bg: "bg-violet-500/20",
    text: "text-violet-200",
    label: "Specialist",
  },
  tool: {
    ring: "ring-amber-500/40",
    bg: "bg-amber-500/20",
    text: "text-amber-200",
    label: "Tool",
  },
  cache: {
    ring: "ring-slate-500/40",
    bg: "bg-slate-500/20",
    text: "text-slate-200",
    label: "Cache",
  },
  committee: {
    ring: "ring-fuchsia-500/40",
    bg: "bg-fuchsia-500/15",
    text: "text-fuchsia-200",
    label: "Committee",
  },
  response: {
    ring: "ring-indigo-500/40",
    bg: "bg-indigo-500/20",
    text: "text-indigo-200",
    label: "Response",
  },
  alert: {
    ring: "ring-rose-500/40",
    bg: "bg-rose-500/20",
    text: "text-rose-300",
    label: "Alert",
  },
  scheduled: {
    ring: "ring-sky-500/40",
    bg: "bg-sky-500/15",
    text: "text-sky-200",
    label: "Scheduled",
  },
};

function kindStyle(kind: string) {
  return (
    KIND_COLORS[kind] ?? {
      ring: "ring-line-strong/40",
      bg: "bg-surface-input/40",
      text: "text-fg",
      label: kind,
    }
  );
}

// MiniMap can't read Tailwind classes — needs literal CSS colors per kind.
// Kept in sync with KIND_COLORS so the minimap reads as a faithful zoom-out
// preview of the canvas.
const MINIMAP_COLORS: Record<string, string> = {
  inbound: "rgb(52 211 153)",     // emerald-400
  memory: "rgb(251 113 133)",     // rose-400
  knowledge: "rgb(56 189 248)",   // sky-400
  specialist: "rgb(167 139 250)", // violet-400
  tool: "rgb(251 191 36)",        // amber-400
  cache: "rgb(148 163 184)",      // slate-400
  committee: "rgb(232 121 249)",  // fuchsia-400
  response: "rgb(129 140 248)",   // indigo-400
  alert: "rgb(251 113 133)",      // rose-400
  scheduled: "rgb(56 189 248)",   // sky-400
};

function formatTs(ts: string): string {
  try {
    return new Date(ts).toLocaleTimeString();
  } catch {
    return ts;
  }
}

// ---------------------------------------------------------------------------
// React Flow custom node — handles only on top + bottom because dagre lays
// out top-to-bottom by default. Pass-through props from React Flow include
// `data` (the AuditGraphNode payload) and `selected` (current selection).
// ---------------------------------------------------------------------------

// React Flow's Node<T> requires T to extend Record<string, unknown>, so we
// keep the AuditGraphNode shape but widen the index signature to satisfy it.
type FlowNodeData = AuditGraphNode & {
  selected?: boolean;
  [key: string]: unknown;
};

// Fixed node footprint — dagre needs deterministic dimensions to compute a
// non-overlapping layout. Picking explicit values (rather than min-/max-)
// means same-depth siblings won't collide because dagre allocates rank
// columns based on the largest node it sees.
const NODE_WIDTH = 240;
const NODE_HEIGHT = 92;

function AuditNode({ data, selected }: NodeProps<Node<FlowNodeData>>) {
  const style = kindStyle(data.kind);
  return (
    <div
      className={[
        "group relative rounded-xl border text-xs cursor-pointer overflow-hidden",
        "transition-all duration-150",
        // Always-visible borders so cards stay distinct against the
        // canvas grid; selection takes over the ring slot when active.
        "border-line",
        style.bg,
        style.text,
        // Selection ring uses a brighter color to read at-a-glance over
        // the kind-specific border; hover gets a softer ring so users
        // can preview targets without committing.
        selected
          ? "ring-2 ring-indigo-400 shadow-lg shadow-indigo-500/20"
          : "ring-1 ring-line-strong/30 hover:ring-2 hover:ring-fg-muted/40 shadow-md hover:shadow-lg",
      ].join(" ")}
      style={{ width: NODE_WIDTH, height: NODE_HEIGHT }}
    >
      <Handle
        type="target"
        position={Position.Top}
        className="!w-2 !h-2 !bg-line-strong !border-line-strong"
      />
      <div className="flex flex-col h-full px-3 py-2">
        <div className="flex items-center justify-between gap-2 mb-1.5 flex-shrink-0">
          <span
            className={[
              "inline-block px-1.5 py-[1px] rounded text-[9px] font-bold tracking-widest uppercase",
              style.bg,
              style.text,
              "ring-1 ring-inset ring-current/20",
            ].join(" ")}
          >
            {style.label}
          </span>
          <span className="font-mono text-[10px] opacity-50">
            {formatTs(data.ts)}
          </span>
        </div>
        <div className="font-medium leading-tight text-[12px] line-clamp-2 flex-1 break-words">
          {data.label}
        </div>
        {data.actor && (
          <div className="mt-1 font-mono text-[10px] opacity-60 truncate flex-shrink-0">
            @{data.actor}
          </div>
        )}
      </div>
      <Handle
        type="source"
        position={Position.Bottom}
        className="!w-2 !h-2 !bg-line-strong !border-line-strong"
      />
    </div>
  );
}

const nodeTypes = { audit: AuditNode };

// ---------------------------------------------------------------------------
// Auto-layout via @dagrejs/dagre.
//
// The previous hand-rolled longest-path algorithm collided when multiple
// nodes shared a depth — same-depth siblings stacked along a single row
// without accounting for node width. Dagre solves the layered-graph
// coordinate assignment problem properly: it allocates rank columns based
// on node footprint and inserts gaps to avoid overlap.
//
// Direction "TB" (top→bottom) matches how a chat turn reads — inbound at
// top, response at bottom. nodesep is the gap between siblings at the
// same rank; ranksep is the vertical gap between layers.
// ---------------------------------------------------------------------------

function layoutNodes(graph: AuditGraph): { nodes: Node<FlowNodeData>[]; edges: Edge[] } {
  const g = new Dagre.graphlib.Graph({ multigraph: false, compound: false });
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({
    rankdir: "TB",
    nodesep: 48,   // horizontal gap between sibling nodes
    ranksep: 80,   // vertical gap between ranks
    marginx: 24,
    marginy: 24,
  });

  for (const n of graph.nodes) {
    g.setNode(n.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  }
  for (const e of graph.edges) {
    // Skip self-loops or edges pointing at unknown nodes (defensive: server
    // shouldn't produce these but it would crash dagre if it ever did).
    if (e.source === e.target) continue;
    if (!g.hasNode(e.source) || !g.hasNode(e.target)) continue;
    g.setEdge(e.source, e.target);
  }

  Dagre.layout(g);

  const flowNodes: Node<FlowNodeData>[] = graph.nodes.map((n) => {
    const pos = g.node(n.id);
    // dagre returns center coordinates; React Flow expects top-left.
    const x = (pos?.x ?? 0) - NODE_WIDTH / 2;
    const y = (pos?.y ?? 0) - NODE_HEIGHT / 2;
    const data: FlowNodeData = { ...n };
    return {
      id: n.id,
      type: "audit",
      position: { x, y },
      data,
      // Pin width/height so dagre's layout reflects what's painted.
      width: NODE_WIDTH,
      height: NODE_HEIGHT,
    };
  });

  // Edge styling: causal edges read as the primary spine of a turn
  // (specialist → tool, inbound → memory). Order edges are secondary —
  // they show "this happened next" but don't carry causal meaning, so
  // they're rendered subtler.
  const flowEdges: Edge[] = graph.edges.map((e, i) => {
    const isCause = e.relation === "cause";
    return {
      id: `e${i}`,
      source: e.source,
      target: e.target,
      type: "smoothstep",
      animated: isCause,
      style: {
        stroke: isCause ? "rgb(165 180 252)" : "rgb(100 116 139)",
        strokeWidth: isCause ? 1.75 : 1.25,
        opacity: isCause ? 0.9 : 0.55,
      },
      markerEnd: {
        type: MarkerType.ArrowClosed,
        width: 14,
        height: 14,
        color: isCause ? "rgb(165 180 252)" : "rgb(100 116 139)",
      },
    };
  });

  return { nodes: flowNodes, edges: flowEdges };
}

// ---------------------------------------------------------------------------
// Event detail panel — type-specific renderers. Falls back to a JSON dump for
// unknown event types so new instrumentation surfaces without code changes.
// ---------------------------------------------------------------------------

function EventDetailPanel({
  event,
  detail,
  onOpenDrawer,
}: {
  event: AuditEvent;
  detail: AuditEventDetail | null;
  onOpenDrawer: () => void;
}) {
  const d = (event.details ?? {}) as Record<string, unknown>;
  const full = detail?.full ?? null;

  // ---- Header ----
  const style = kindStyle(deriveKind(event.event_type));
  const header = (
    <div className="border-b border-line pb-3 mb-3">
      <div className="flex items-center gap-2 mb-1">
        <span
          className={`inline-block px-2 py-0.5 rounded-full border border-line text-[10px] font-medium ${style.bg} ${style.text}`}
        >
          {event.event_type}
        </span>
        <span className="text-xs text-fg-muted">@ {formatTs(event.ts)}</span>
        <span className="text-xs text-fg-muted">·</span>
        <span className="text-xs text-fg-muted">actor: {event.actor ?? "—"}</span>
      </div>
      <div className="text-sm text-fg break-words">{event.summary}</div>
    </div>
  );

  // ---- Type-specific body ----
  let body: React.ReactNode = null;
  switch (event.event_type) {
    case "memory_snapshot":
      body = <MemorySnapshotBody details={d} full={full} />;
      break;
    case "knowledge_retrieval":
      body = <KnowledgeBody details={d} full={full} />;
      break;
    case "specialist_consult":
      body = <SpecialistBody details={d} full={full} />;
      break;
    case "tool_invocation":
      body = <ToolBody details={d} full={full} />;
      break;
    case "cache_event":
      body = <CacheBody details={d} />;
      break;
    case "committee_review":
      body = <CommitteeBody details={d} />;
      break;
    default:
      body = (
        <div className="text-xs text-fg">
          <KVList obj={d} />
        </div>
      );
  }

  return (
    <div className="h-full overflow-y-auto p-4 text-fg">
      {header}
      {body}
      <button
        type="button"
        onClick={onOpenDrawer}
        className="mt-4 w-full px-3 py-1.5 rounded-lg bg-surface-overlay hover:bg-surface-input text-xs border border-line-strong"
      >
        View full payload →
      </button>
    </div>
  );
}

function deriveKind(eventType: string): string {
  const map: Record<string, string> = {
    integration_inbound: "inbound",
    chat_turn: "response",
    specialist_consult: "specialist",
    tool_invocation: "tool",
    knowledge_retrieval: "knowledge",
    cache_event: "cache",
    memory_snapshot: "memory",
    committee_review: "committee",
  };
  return map[eventType] ?? eventType;
}

function KVList({ obj }: { obj: Record<string, unknown> }) {
  // Filter out null/undefined so the panel doesn't pad with empty rows.
  const entries = Object.entries(obj).filter(([, v]) => v !== undefined && v !== null);
  if (entries.length === 0) return <div className="text-fg-muted italic">no details</div>;
  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1">
      {entries.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="font-mono text-fg-muted">{k}</dt>
          <dd className="font-mono break-all">
            {typeof v === "string" || typeof v === "number" || typeof v === "boolean"
              ? String(v)
              : JSON.stringify(v)}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function MemorySnapshotBody({
  details,
  full,
}: {
  details: Record<string, unknown>;
  full: Record<string, unknown> | null;
}) {
  return (
    <div className="space-y-3 text-xs">
      <Section title="Summary">
        <KVList
          obj={{
            model: details.model,
            committee_review: details.committee_review,
            history_len: details.history_len,
            episodic_chars: details.episodic_chars,
            retrieved_chars: details.retrieved_chars,
            company_profile_hash: details.company_profile_hash,
          }}
        />
      </Section>
      <Section title="System prompt blocks (live this turn)">
        {Array.isArray(details.system_blocks) ? (
          <ul className="space-y-1">
            {(details.system_blocks as string[]).map((b, i) => (
              <li key={i} className="font-mono text-fg">
                · {b}
              </li>
            ))}
          </ul>
        ) : (
          <div className="text-fg-muted italic">none</div>
        )}
      </Section>
      {full?.episodic_context ? (
        <Section title="Episodic context (what the agent remembered)">
          <pre className="whitespace-pre-wrap break-words bg-black/30 p-2 rounded text-[11px] max-h-48 overflow-y-auto">
            {String(full.episodic_context)}
          </pre>
        </Section>
      ) : null}
      {full?.retrieved_context ? (
        <Section title="RAG context (chunks injected into the user turn)">
          <pre className="whitespace-pre-wrap break-words bg-black/30 p-2 rounded text-[11px] max-h-48 overflow-y-auto">
            {String(full.retrieved_context)}
          </pre>
        </Section>
      ) : null}
      {/* company profile is hashed-only by design — see _emit_memory_snapshot.
          The hash is shown in the summary KVList above. */}
    </div>
  );
}

function KnowledgeBody({
  details,
  full,
}: {
  details: Record<string, unknown>;
  full: Record<string, unknown> | null;
}) {
  type Chunk = {
    source?: string;
    domain?: string;
    distance?: number;
    text_preview?: string;
  };
  const builtin = (full?.builtin_chunks as Chunk[] | undefined) ?? [];
  const company = (full?.company_chunks as Chunk[] | undefined) ?? [];
  return (
    <div className="space-y-3 text-xs">
      <Section title="Query">
        <pre className="whitespace-pre-wrap break-words bg-black/30 p-2 rounded text-[11px]">
          {String(details.query ?? full?.query ?? "")}
        </pre>
      </Section>
      <Section title={`Built-in chunks (${builtin.length})`}>
        {builtin.length === 0 ? (
          <div className="text-fg-muted italic">none</div>
        ) : (
          <ul className="space-y-2">
            {builtin.map((c, i) => (
              <ChunkRow key={i} chunk={c} />
            ))}
          </ul>
        )}
      </Section>
      <Section title={`Company chunks (${company.length})`}>
        {company.length === 0 ? (
          <div className="text-fg-muted italic">none</div>
        ) : (
          <ul className="space-y-2">
            {company.map((c, i) => (
              <ChunkRow key={i} chunk={c} />
            ))}
          </ul>
        )}
      </Section>
    </div>
  );
}

function ChunkRow({ chunk }: { chunk: { source?: string; domain?: string; distance?: number; text_preview?: string } }) {
  return (
    <li className="bg-black/30 p-2 rounded">
      <div className="flex items-center gap-2 mb-1 text-[10px] text-fg-muted">
        <span className="font-mono text-fg">{chunk.source ?? "?"}</span>
        {chunk.domain && <span>· {chunk.domain}</span>}
        {typeof chunk.distance === "number" && (
          <span>· dist={chunk.distance.toFixed(3)}</span>
        )}
      </div>
      <div className="text-[11px] text-fg whitespace-pre-wrap break-words">
        {chunk.text_preview ?? ""}
      </div>
    </li>
  );
}

function SpecialistBody({
  details,
  full,
}: {
  details: Record<string, unknown>;
  full: Record<string, unknown> | null;
}) {
  return (
    <div className="space-y-3 text-xs">
      <Section title="Routing">
        <KVList
          obj={{
            iteration: details.iteration,
            duration_ms: details.duration_ms,
            context_preview: details.context_preview,
          }}
        />
      </Section>
      {full?.query ? (
        <Section title="Query sent to specialist">
          <pre className="whitespace-pre-wrap break-words bg-black/30 p-2 rounded text-[11px]">
            {String(full.query)}
          </pre>
        </Section>
      ) : null}
      {full?.context ? (
        <Section title="Routing context">
          <pre className="whitespace-pre-wrap break-words bg-black/30 p-2 rounded text-[11px]">
            {String(full.context)}
          </pre>
        </Section>
      ) : null}
      {full?.response ? (
        <Section title="Specialist response">
          <pre className="whitespace-pre-wrap break-words bg-black/30 p-2 rounded text-[11px] max-h-72 overflow-y-auto">
            {String(full.response)}
          </pre>
        </Section>
      ) : null}
      {Array.isArray(full?.active_prompt_blocks) && (
        <Section title="Active system blocks">
          <ul className="text-fg font-mono space-y-1">
            {(full.active_prompt_blocks as string[]).map((b, i) => (
              <li key={i}>· {b}</li>
            ))}
          </ul>
        </Section>
      )}
    </div>
  );
}

function ToolBody({
  details,
  full,
}: {
  details: Record<string, unknown>;
  full: Record<string, unknown> | null;
}) {
  return (
    <div className="space-y-3 text-xs">
      <Section title="Tool">
        <KVList
          obj={{
            tool: details.tool,
            kind: details.kind,
            iteration: details.iteration,
          }}
        />
      </Section>
      {full?.input !== undefined && (
        <Section title="Input">
          <pre className="whitespace-pre-wrap break-words bg-black/30 p-2 rounded text-[11px] max-h-48 overflow-y-auto">
            {JSON.stringify(full.input, null, 2)}
          </pre>
        </Section>
      )}
      {full?.result !== undefined && (
        <Section title="Result">
          <pre className="whitespace-pre-wrap break-words bg-black/30 p-2 rounded text-[11px] max-h-72 overflow-y-auto">
            {typeof full.result === "string"
              ? full.result
              : JSON.stringify(full.result, null, 2)}
          </pre>
        </Section>
      )}
      {Array.isArray(full?.active_prompt_blocks) && (
        <Section title="Active system blocks">
          <ul className="text-fg font-mono space-y-1">
            {(full.active_prompt_blocks as string[]).map((b, i) => (
              <li key={i}>· {b}</li>
            ))}
          </ul>
        </Section>
      )}
    </div>
  );
}

function CacheBody({ details }: { details: Record<string, unknown> }) {
  const cr = Number(details.cache_read_input_tokens ?? 0);
  const cc = Number(details.cache_creation_input_tokens ?? 0);
  const inp = Number(details.input_tokens ?? 0);
  const out = Number(details.output_tokens ?? 0);
  const total = cr + cc + inp;
  const pct = (n: number) => (total > 0 ? Math.round((n / total) * 100) : 0);
  return (
    <div className="space-y-3 text-xs">
      <Section title="Token usage">
        <KVList obj={{ model: details.model, iteration: details.iteration, stop_reason: details.stop_reason }} />
      </Section>
      <Section title="Input token breakdown">
        <div className="space-y-1">
          <TokenBar label="cache read" value={cr} pct={pct(cr)} color="bg-emerald-500" />
          <TokenBar label="cache create" value={cc} pct={pct(cc)} color="bg-amber-500" />
          <TokenBar label="fresh input" value={inp} pct={pct(inp)} color="bg-sky-500" />
        </div>
        <div className="text-[10px] text-fg-muted mt-2">
          {cr > 0
            ? `Cache hit — ${cr} tokens served from cache (saved ~${Math.round((cr / Math.max(total, 1)) * 100)}% of input cost)`
            : "Cache miss — no tokens served from cache this iteration"}
        </div>
      </Section>
      <Section title="Output">
        <div className="font-mono text-fg">{out} tokens</div>
      </Section>
    </div>
  );
}

function TokenBar({ label, value, pct, color }: { label: string; value: number; pct: number; color: string }) {
  return (
    <div className="text-[10px]">
      <div className="flex justify-between">
        <span className="text-fg-muted">{label}</span>
        <span className="font-mono">
          {value} ({pct}%)
        </span>
      </div>
      <div className="h-1.5 rounded bg-surface-input mt-0.5 overflow-hidden">
        <div className={`${color} h-full`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

function CommitteeBody({ details }: { details: Record<string, unknown> }) {
  type Critique = {
    reviewer?: string;
    severity?: string;
    critique?: string;
    suggested_edits?: string;
  };
  const critiques = (details.critiques as Critique[] | undefined) ?? [];
  return (
    <div className="space-y-3 text-xs">
      <Section title="Committee">
        <KVList
          obj={{
            consulted: details.consulted,
            draft_length: details.draft_length,
            final_length: details.final_length,
            review_ms: details.review_ms,
            revision_ms: details.revision_ms,
          }}
        />
      </Section>
      <Section title={`Critiques (${critiques.length})`}>
        {critiques.length === 0 ? (
          <div className="text-fg-muted italic">none</div>
        ) : (
          <ul className="space-y-2">
            {critiques.map((c, i) => (
              <li key={i} className="bg-black/30 p-2 rounded">
                <div className="flex gap-2 mb-1 text-[10px]">
                  <span className="font-mono text-fg">@{c.reviewer ?? "?"}</span>
                  <span className="text-fg-muted">· {c.severity ?? "?"}</span>
                </div>
                {c.critique && (
                  <div className="text-[11px] text-fg whitespace-pre-wrap break-words">
                    {c.critique}
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </Section>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wide text-fg-muted mb-1">{title}</div>
      {children}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page component — three panes: flow chart (left), event detail (center),
// JSON drawer (right, expands edge-to-edge when "expanded" is true).
// ---------------------------------------------------------------------------

export default function AuditSessionPage() {
  const params = useParams<{ id: string }>();
  const sessionId = useMemo(() => {
    if (!params?.id) return "";
    return decodeURIComponent(String(params.id));
  }, [params]);

  const [data, setData] = useState<AuditSessionResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // selected event_id (drives center panel + right drawer fetch)
  const [selectedEventId, setSelectedEventId] = useState<number | null>(null);
  const [detail, setDetail] = useState<AuditEventDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [drawerExpanded, setDrawerExpanded] = useState(false);
  const [copyState, setCopyState] = useState<"idle" | "ok" | "fail">("idle");

  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    getAuditSession(sessionId)
      .then((res) => {
        if (cancelled) return;
        setData(res);
        // Default selection: the inbound node, or the OLDEST event. The
        // API returns events newest-first (id DESC); the canvas reads
        // top-to-bottom from oldest, so pick the last entry to anchor the
        // user at the causal start of the session.
        const first =
          res.events.find((e) => e.event_type === "integration_inbound") ??
          res.events[res.events.length - 1];
        if (first) setSelectedEventId(first.id);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "Failed to load session");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  // Lazy-fetch full payload for the currently selected event (cached by id).
  const [detailsCache, setDetailsCache] = useState<Record<number, AuditEventDetail>>({});
  useEffect(() => {
    if (selectedEventId === null) {
      setDetail(null);
      return;
    }
    const cached = detailsCache[selectedEventId];
    if (cached) {
      setDetail(cached);
      return;
    }
    let cancelled = false;
    setDetailLoading(true);
    getAuditLog(selectedEventId)
      .then((d) => {
        if (cancelled) return;
        setDetailsCache((prev) => ({ ...prev, [d.id]: d }));
        setDetail(d);
      })
      .catch(() => {
        // Detail fetch failure is non-fatal — the center pane still shows the
        // summary `details` from the list payload, just without `full`.
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedEventId, detailsCache]);

  const flow = useMemo(() => {
    if (!data) return null;
    return layoutNodes(data.graph);
  }, [data]);

  const selectedEvent = useMemo<AuditEvent | null>(() => {
    if (!data || selectedEventId === null) return null;
    return data.events.find((e) => e.id === selectedEventId) ?? null;
  }, [data, selectedEventId]);

  const handleNodeClick = useCallback(
    (_evt: React.MouseEvent, node: Node) => {
      const eventId = (node.data as FlowNodeData | undefined)?.event_id;
      if (typeof eventId === "number") setSelectedEventId(eventId);
    },
    [],
  );

  return (
    <div className="flex flex-col h-full bg-surface text-fg">
      {/* Title row: the way back to the log, and which session this is. */}
      <div className="flex-shrink-0 border-b border-line px-4 sm:px-6 py-3 flex flex-wrap items-center gap-x-4 gap-y-1">
        <Link
          href="/audit"
          className="-ml-2 inline-flex min-h-touch items-center gap-1.5 rounded-lg px-2 text-[15px] text-fg-muted hover:text-fg hover:bg-surface-overlay transition-colors"
        >
          <Icon name="arrow-left" size="w-4 h-4" />
          Audit log
        </Link>
        <h1 className="min-w-0 text-xl sm:text-2xl font-bold tracking-tight text-fg">Session flow</h1>
        {sessionId && (
          <span className="min-w-0 truncate font-mono text-xs text-fg-subtle" title={sessionId}>
            {sessionId}
          </span>
        )}
      </div>
      <main className="flex-1 min-h-0 flex">
        {loading && (
          <div className="flex-1 flex items-center justify-center text-fg-muted">
            Loading session…
          </div>
        )}
        {error && !loading && (
          <div className="flex-1 flex items-center justify-center p-6 text-rose-300 text-sm">
            {error}
          </div>
        )}
        {!loading && !error && flow && data && (
          <>
            {/* Left: flow chart canvas. When the drawer is expanded the
                canvas hides on narrow screens (full-screen JSON) and shrinks
                on wide ones (so the drawer + canvas can coexist). */}
            <div
              className={[
                "relative border-r border-line bg-surface-elevated/30 transition-all",
                drawerExpanded ? "hidden xl:block xl:flex-1" : "flex-1 md:flex-[3]",
              ].join(" ")}
            >
              {/* Stat strip — sits above the canvas as a sticky chip so the
                  user sees graph shape at a glance (turn count, event count,
                  channel) without scanning the whole tree. */}
              <div className="absolute top-3 left-3 z-10 flex items-center gap-2 px-3 py-1.5 rounded-full bg-surface-elevated/90 backdrop-blur border border-line text-[11px] text-fg-muted font-mono">
                <span className="text-fg">{flow.nodes.length}</span>
                <span>events</span>
                <span className="text-fg-subtle">·</span>
                <span className="text-fg">{flow.edges.length}</span>
                <span>edges</span>
                {data.channel && (
                  <>
                    <span className="text-fg-subtle">·</span>
                    <span>{data.channel}</span>
                  </>
                )}
                {data.cost_summary &&
                  (() => {
                    const cs = data.cost_summary;
                    const totalIn =
                      cs.input_tokens +
                      cs.cache_read_input_tokens +
                      cs.cache_creation_input_tokens;
                    const cachedPct =
                      totalIn > 0
                        ? Math.round((cs.cache_read_input_tokens / totalIn) * 100)
                        : 0;
                    return (
                      <>
                        <span className="text-fg-subtle">·</span>
                        <span className="text-fg">{cs.calls}</span>
                        <span>calls</span>
                        <span className="text-fg-subtle">·</span>
                        <span className="text-fg">{totalIn.toLocaleString()}</span>
                        <span>in</span>
                        <span className="text-fg-subtle">·</span>
                        <span className="text-fg">
                          {cs.output_tokens.toLocaleString()}
                        </span>
                        <span>out</span>
                        <span className="text-fg-subtle">·</span>
                        <span
                          className="text-fg"
                          title="Share of prompt input served from the cache (≈10x cheaper than fresh input)."
                        >
                          {cachedPct}%
                        </span>
                        <span>cached</span>
                      </>
                    );
                  })()}
                {data.degradations.length > 0 && (
                  <>
                    <span className="text-fg-subtle">·</span>
                    <span
                      className="text-amber-400"
                      title={data.degradations
                        .map(
                          (d) =>
                            `${d.kind} ${d.reason} ×${d.count}` +
                            (d.detail ? ` (${d.detail})` : ""),
                        )
                        .join("; ")}
                    >
                      ⚠ {data.degradations.reduce((n, d) => n + d.count, 0)}{" "}
                      degraded
                    </span>
                  </>
                )}
              </div>
              <ReactFlow
                nodes={flow.nodes.map((n) => ({
                  ...n,
                  selected: n.data.event_id === selectedEventId,
                }))}
                edges={flow.edges}
                nodeTypes={nodeTypes}
                onNodeClick={handleNodeClick}
                fitView
                fitViewOptions={{ padding: 0.25, maxZoom: 1.1, minZoom: 0.15 }}
                minZoom={0.15}
                maxZoom={1.5}
                proOptions={{ hideAttribution: true }}
                nodesDraggable
                elementsSelectable
                defaultEdgeOptions={{ type: "smoothstep" }}
              >
                {/* Dotted background grid — same 16px rhythm as dagre's
                    spacing constants so node edges align to the grid. */}
                <Background gap={16} size={1.2} color="rgba(255,255,255,0.05)" />
                <Controls
                  className="!bg-surface-elevated/90 !border-line backdrop-blur [&>button]:!bg-transparent [&>button]:!border-line [&>button]:!text-fg-muted [&>button:hover]:!bg-surface-input"
                  showInteractive={false}
                />
                <MiniMap
                  className="!bg-surface-elevated/80 !border-line"
                  pannable
                  zoomable
                  nodeColor={(node) => {
                    const kind = (node.data as FlowNodeData | undefined)?.kind ?? "";
                    return MINIMAP_COLORS[kind] ?? "rgb(100 116 139)";
                  }}
                  maskColor="rgba(0,0,0,0.5)"
                />
              </ReactFlow>
            </div>

            {/* Center: selected event detail. Same coexistence rule —
                stays visible on xl+ when the drawer is open. */}
            <div
              className={[
                "border-r border-line bg-surface",
                drawerExpanded
                  ? "hidden xl:block xl:w-80 xl:flex-shrink-0"
                  : "w-80 lg:w-96 flex-shrink-0",
              ].join(" ")}
            >
              {selectedEvent ? (
                <EventDetailPanel
                  event={selectedEvent}
                  detail={detail}
                  onOpenDrawer={() => setDrawerExpanded(true)}
                />
              ) : (
                <div className="p-4 text-fg-muted text-sm">
                  Click a node to see what the agent saw.
                </div>
              )}
              {detailLoading && (
                <div className="px-4 pb-2 text-[10px] text-fg-muted">loading full payload…</div>
              )}
            </div>

            {/* Right: collapsible JSON drawer */}
            <div
              className={[
                "bg-surface-elevated/60 border-l border-line transition-all flex flex-col",
                drawerExpanded ? "flex-1" : "w-12 flex-shrink-0",
              ].join(" ")}
            >
              <button
                type="button"
                onClick={() => setDrawerExpanded((v) => !v)}
                className="h-10 flex items-center justify-center border-b border-line text-fg-muted hover:text-fg text-xs"
                title={drawerExpanded ? "Collapse drawer" : "Expand drawer (full payload)"}
              >
                {drawerExpanded ? "→" : "←"}
              </button>
              {drawerExpanded && (
                <div className="flex-1 min-h-0 overflow-y-auto p-4">
                  <div className="flex items-center justify-between mb-2">
                    <div className="text-[10px] uppercase tracking-wide text-fg-muted">
                      Full payload
                    </div>
                    <button
                      type="button"
                      onClick={async () => {
                        if (!detail) return;
                        const payload = JSON.stringify(detail.full ?? detail, null, 2);
                        // navigator.clipboard requires a secure context;
                        // fall back to a hidden <textarea> for http:// deploys.
                        try {
                          if (
                            typeof navigator !== "undefined" &&
                            navigator.clipboard?.writeText
                          ) {
                            await navigator.clipboard.writeText(payload);
                          } else {
                            const ta = document.createElement("textarea");
                            ta.value = payload;
                            ta.style.position = "fixed";
                            ta.style.opacity = "0";
                            document.body.appendChild(ta);
                            ta.select();
                            const ok = document.execCommand("copy");
                            document.body.removeChild(ta);
                            if (!ok) throw new Error("execCommand returned false");
                          }
                          setCopyState("ok");
                        } catch {
                          setCopyState("fail");
                        }
                        // Brief flash then reset.
                        window.setTimeout(() => setCopyState("idle"), 1500);
                      }}
                      className="text-[10px] px-2 py-0.5 rounded bg-surface-overlay hover:bg-surface-input border border-line-strong"
                    >
                      {copyState === "ok"
                        ? "Copied!"
                        : copyState === "fail"
                          ? "Copy failed"
                          : "Copy"}
                    </button>
                  </div>
                  <pre className="text-[11px] text-fg bg-black/40 rounded-lg p-3 overflow-x-auto whitespace-pre-wrap break-words">
                    {detail
                      ? JSON.stringify(detail.full ?? detail, null, 2)
                      : selectedEvent
                        ? JSON.stringify(selectedEvent.details, null, 2)
                        : "(select a node)"}
                  </pre>
                </div>
              )}
            </div>
          </>
        )}
      </main>
    </div>
  );
}
