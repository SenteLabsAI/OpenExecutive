"use client";

import {
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import {
  DynamicWorkflowDef,
  WorkflowMeta,
  WorkflowRunSummary,
  WorkflowSection,
  deleteCustomWorkflow,
  deleteWorkflowRun,
  listCustomWorkflows,
  listWorkflowRuns,
  listSkills,
  listWorkflows,
} from "@/lib/api";
import PlaybooksBrowser from "@/components/jobs/PlaybooksBrowser";
import StartHere from "@/components/jobs/StartHere";
import {
  RunBucket,
  runStatusBadgeColor,
  runStatusBucket,
  runStatusLabel,
} from "@/lib/runStatus";

const SECTION_ORDER: WorkflowSection[] = [
  "Board",
  "Capital & Investors",
  "Growth & GTM",
  "Product",
  "People",
  "Risk, Legal & Crisis",
  "Operating Cadence",
];

// Chip labels for the ready-made list. "All" hides the background
// (system-run) jobs; they get their own chip so the default view is only what
// a user starts by hand. Custom workflows have their own list above it.
type SectionFilter = "all" | "system" | WorkflowSection;

const SECTION_CHIP_LABEL: Record<WorkflowSection, string> = {
  Board: "Board",
  "Capital & Investors": "Capital",
  "Growth & GTM": "Growth & GTM",
  Product: "Product",
  People: "People",
  "Risk, Legal & Crisis": "Risk & Legal",
  "Operating Cadence": "Operating",
};

function isSectionFilter(v: string | null): v is SectionFilter {
  return (
    v === "all" ||
    v === "system" ||
    (SECTION_ORDER as string[]).includes(v ?? "")
  );
}

/** Which chip a workflow belongs to. Background jobs only ever show under "System". */
function inSectionFilter(w: WorkflowMeta, f: SectionFilter): boolean {
  if (f === "system") return !!w.background;
  if (w.background) return false;
  if (f === "all") return true;
  return w.section === f;
}

// Ready-made workflows shown up front, each with a plain line on when to
// use it. The full list sits behind "Browse all".
const STARTER_PICKS: { name: string; useFor: string }[] = [
  {
    name: "investor_update",
    useFor: "Send investors a short monthly email: highlights, numbers, asks.",
  },
  {
    name: "board_prep",
    useFor: "Get a full board deck drafted before your next meeting.",
  },
  {
    name: "mbr",
    useFor: "Review last month against plan and flag what needs a decision.",
  },
  {
    name: "competitive_teardown",
    useFor: "Size up a competitor and get battle-card material for sales.",
  },
];

// Runs shown per workflow group before "Show more".
const RUNS_PER_GROUP = 5;

type Tab = "catalog" | "runs" | "playbooks";
type RunStatus = RunBucket;

function isTab(v: string | null): v is Tab {
  return v === "catalog" || v === "runs" || v === "playbooks";
}
function isStatus(v: string | null): v is RunStatus {
  return (
    v === "active" || v === "awaiting" || v === "done" || v === "error"
  );
}

function formatRelativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

function statusBadge(status: string) {
  return (
    <span
      className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium ring-1 ${runStatusBadgeColor(status)}`}
    >
      {runStatusLabel(status)}
    </span>
  );
}

function matchesQuery(q: string, ...fields: (string | undefined)[]): boolean {
  if (!q) return true;
  const needle = q.toLowerCase();
  return fields.some((f) => (f ?? "").toLowerCase().includes(needle));
}

function groupBy<T, K extends string>(
  arr: T[],
  keyFn: (item: T) => K
): Map<K, T[]> {
  const out = new Map<K, T[]>();
  for (const item of arr) {
    const k = keyFn(item);
    const bucket = out.get(k);
    if (bucket) bucket.push(item);
    else out.set(k, [item]);
  }
  return out;
}

function JobsPageInner() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  const tab: Tab = isTab(searchParams.get("tab"))
    ? (searchParams.get("tab") as Tab)
    : "catalog";
  const statusParam = searchParams.get("status");
  const status: RunStatus | null = isStatus(statusParam) ? statusParam : null;
  const sectionParam = searchParams.get("section");
  const section: SectionFilter = isSectionFilter(sectionParam) ? sectionParam : "all";
  // The full ready-made list is folded behind "Browse all"; a chip in the URL
  // means someone was already browsing it.
  const browsing = searchParams.get("browse") === "1" || section !== "all";

  const [workflows, setWorkflows] = useState<WorkflowMeta[]>([]);
  // Custom workflows that are switched off (e.g. saved from chat with tool
  // steps): not runnable, so absent from `workflows` until someone turns them on.
  const [offWorkflows, setOffWorkflows] = useState<DynamicWorkflowDef[]>([]);
  const [runs, setRuns] = useState<WorkflowRunSummary[]>([]);
  const [playbookCount, setPlaybookCount] = useState<number | undefined>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [catalogQuery, setCatalogQuery] = useState("");
  const [runsQuery, setRunsQuery] = useState("");
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const readyMadeRef = useRef<HTMLElement>(null);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const [wfs, rs, custom, playbooks] = await Promise.all([
        listWorkflows(),
        listWorkflowRuns(),
        // Optional strip — never fail the whole page over it.
        listCustomWorkflows().catch(() => [] as DynamicWorkflowDef[]),
        // Tab count only; the Playbooks tab loads its own list.
        listSkills().catch(() => undefined),
      ]);
      setWorkflows(wfs);
      // The Playbooks tab reports its own, fresher count once mounted.
      setPlaybookCount((current) => current ?? playbooks?.length);
      setRuns(rs);
      setOffWorkflows(custom.filter((d) => !d.is_active));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const setParam = useCallback(
    (updates: Record<string, string | null>) => {
      const sp = new URLSearchParams(searchParams.toString());
      for (const [k, v] of Object.entries(updates)) {
        if (v === null) sp.delete(k);
        else sp.set(k, v);
      }
      const qs = sp.toString();
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    },
    [pathname, router, searchParams]
  );

  const browseReadyMade = useCallback(() => {
    setParam({ browse: "1" });
    readyMadeRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [setParam]);

  const runCounts = useMemo(() => {
    const c = { active: 0, awaiting: 0, done: 0, error: 0 };
    for (const r of runs) c[runStatusBucket(r.status)]++;
    return c;
  }, [runs]);

  // Default the runs sub-tab once data is loaded and ?status is missing/invalid.
  // Ref guard ensures we only auto-set on the first eligible render, so a user
  // who explicitly navigates back to ?tab=runs without ?status isn't overridden
  // mid-interaction and StrictMode double-invocation doesn't double-write.
  const defaultedStatusRef = useRef(false);
  useEffect(() => {
    if (tab !== "runs" || loading) return;
    if (status !== null) return;
    if (defaultedStatusRef.current) return;
    defaultedStatusRef.current = true;
    // Awaiting first: a run blocked on the viewer outranks one that is simply
    // still working.
    const next: RunStatus =
      runCounts.awaiting > 0 ? "awaiting" : runCounts.active > 0 ? "active" : "done";
    setParam({ status: next });
  }, [tab, loading, status, runCounts.active, runCounts.awaiting, setParam]);

  const workflowTitleMap = useMemo(
    () => new Map(workflows.map((w) => [w.name, w.title] as const)),
    [workflows]
  );

  const handleDelete = useCallback(
    async (runId: string) => {
      if (!confirm("Delete this run?")) return;
      await deleteWorkflowRun(runId);
      refresh();
    },
    [refresh]
  );

  const handleDeleteCustom = useCallback(
    async (name: string) => {
      if (!confirm(`Delete the custom workflow “${name}”? This cannot be undone.`))
        return;
      await deleteCustomWorkflow(name);
      refresh();
    },
    [refresh]
  );

  return (
    <>
      <div className="flex items-center gap-1 border-b border-line mb-4">
            <TabButton
              active={tab === "catalog"}
              onClick={() => setParam({ tab: "catalog" })}
              label="Build"
            />
            <TabButton
              active={tab === "runs"}
              onClick={() => setParam({ tab: "runs" })}
              label="Runs"
              count={runs.length}
            />
            <TabButton
              active={tab === "playbooks"}
              onClick={() => setParam({ tab: "playbooks", status: null, section: null })}
              label="Playbooks"
              count={playbookCount}
            />
          </div>

          {loading && tab !== "playbooks" && (
            <div className="text-sm text-fg-muted">Loading workflows…</div>
          )}
          {error && (
            <div className="text-sm text-red-400 mb-4">Error: {error}</div>
          )}

          {!loading && tab === "catalog" && offWorkflows.length > 0 && (
            <OffWorkflowsStrip items={offWorkflows} onDelete={handleDeleteCustom} />
          )}

          {tab === "catalog" && (
            <StartHere onBrowseReadyMade={browseReadyMade} />
          )}

          {!loading && tab === "catalog" && (
            <>
              <YourWorkflows
                workflows={workflows.filter((w) => w.is_custom && !w.background)}
                onDeleteCustom={handleDeleteCustom}
              />
              <section ref={readyMadeRef} className="scroll-mt-4">
                <h2 className="text-sm font-semibold text-fg">Ready-made workflows</h2>
                <p className="mb-3 text-xs text-fg-muted">
                  Templates you can run as they are: fill in a few details and go.
                </p>
                {browsing ? (
                  <>
                    <CatalogView
                      workflows={workflows.filter((w) => !w.is_custom)}
                      query={catalogQuery}
                      onQueryChange={setCatalogQuery}
                      section={section}
                      onSectionChange={(s) => setParam({ section: s === "all" ? null : s })}
                    />
                    <button
                      type="button"
                      onClick={() => setParam({ browse: null, section: null })}
                      className="mt-4 text-xs text-indigo-400 hover:text-indigo-300"
                    >
                      Show fewer
                    </button>
                  </>
                ) : (
                  <StarterPicks
                    workflows={workflows}
                    onBrowseAll={() => setParam({ browse: "1" })}
                  />
                )}
              </section>
            </>
          )}

          {tab === "playbooks" && (
            <PlaybooksBrowser
              onCountChange={setPlaybookCount}
              initialPlaybook={searchParams.get("playbook") ?? undefined}
              initialDraft={searchParams.get("draft") ?? undefined}
            />
          )}

          {!loading && tab === "runs" && (
            <RunsView
              runs={runs}
              counts={runCounts}
              status={status ?? "active"}
              onStatusChange={(s) => setParam({ status: s })}
              query={runsQuery}
              onQueryChange={setRunsQuery}
              workflowTitleMap={workflowTitleMap}
              collapsed={collapsed}
              onToggleCollapsed={(key) =>
                setCollapsed((c) => ({ ...c, [key]: !c[key] }))
              }
              expanded={expanded}
              onExpand={(key) => setExpanded((c) => ({ ...c, [key]: true }))}
              onDelete={handleDelete}
            />
          )}
    </>
  );
}

function TabButton({
  active,
  onClick,
  label,
  count,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  count?: number;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`-mb-px px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
        active
          ? "border-indigo-500 text-fg"
          : "border-transparent text-fg-muted hover:text-fg"
      }`}
    >
      {label}
      {count !== undefined && <span className="ml-2 text-xs text-fg-muted">{count}</span>}
    </button>
  );
}

function SearchInput({
  value,
  onChange,
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder: string;
}) {
  return (
    <input
      type="search"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      className="w-full sm:w-80 px-3 py-1.5 text-sm rounded-md bg-surface/60 border border-line text-fg placeholder:text-fg-subtle focus:outline-none focus:ring-1 focus:ring-indigo-500/40 focus:border-line-strong"
    />
  );
}

/** Compact list of switched-off custom workflows, each linking to its review card. */
function OffWorkflowsStrip({
  items,
  onDelete,
}: {
  items: DynamicWorkflowDef[];
  onDelete: (name: string) => void;
}) {
  return (
    <div className="mb-4 rounded-md border border-indigo-500/30 bg-indigo-500/5 px-3 py-2">
      <p className="text-[11px] uppercase tracking-wide text-indigo-300 mb-1">
        Waiting for your approval
      </p>
      <ul className="divide-y divide-line/60">
        {items.map((d) => {
          const tools = new Set(d.steps.flatMap((s) => (s.kind === "action" ? s.tools : [])));
          return (
            <li key={d.name} className="flex items-center gap-3 py-1.5 text-sm min-w-0">
              <span className="truncate text-fg min-w-0 flex-1">{d.title}</span>
              <span className="shrink-0 text-[11px] text-fg-subtle">
                off
                {tools.size > 0 && ` · ${tools.size} ${tools.size === 1 ? "tool" : "tools"}`}
              </span>
              <Link
                href={`/jobs/${encodeURIComponent(d.name)}`}
                className="shrink-0 text-[11px] text-indigo-400 hover:text-indigo-300"
              >
                Review
              </Link>
              <button
                type="button"
                onClick={() => onDelete(d.name)}
                className="shrink-0 text-[11px] text-fg-muted hover:text-red-400 transition"
              >
                Delete
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function WorkflowCard({
  workflow: w,
  onDeleteCustom,
}: {
  workflow: WorkflowMeta;
  onDeleteCustom?: (name: string) => void;
}) {
  return (
    <div className="group relative min-w-0 rounded-md border border-line bg-surface/40 hover:border-line-strong hover:bg-surface-elevated/40 transition">
      <Link
        href={`/jobs/${encodeURIComponent(w.name)}`}
        className="block px-3 py-2.5"
        title={w.description}
      >
        <h3 className="text-sm font-medium text-fg truncate pr-16">{w.title}</h3>
        <p className="text-xs text-fg-muted truncate mt-0.5">{w.description}</p>
        <p className="text-[11px] text-fg-subtle mt-1">
          {w.steps.length} steps · ~{w.estimated_minutes} min
        </p>
      </Link>
      {w.is_custom && onDeleteCustom && (
        <div className="absolute top-2 right-2 flex items-center gap-2 text-[11px]">
          <Link
            href={`/jobs/new?edit=${encodeURIComponent(w.name)}`}
            className="text-indigo-400 hover:text-indigo-300"
          >
            Edit
          </Link>
          <button
            type="button"
            onClick={() => onDeleteCustom(w.name)}
            className="text-fg-muted hover:text-red-400 transition"
          >
            Delete
          </button>
        </div>
      )}
    </div>
  );
}

function WorkflowGrid({
  items,
  onDeleteCustom,
}: {
  items: WorkflowMeta[];
  onDeleteCustom?: (name: string) => void;
}) {
  return (
    <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-2">
      {items.map((w) => (
        <WorkflowCard key={w.name} workflow={w} onDeleteCustom={onDeleteCustom} />
      ))}
    </div>
  );
}

/** The user's own workflows, above the ready-made list. Hidden until they have one. */
function YourWorkflows({
  workflows,
  onDeleteCustom,
}: {
  workflows: WorkflowMeta[];
  onDeleteCustom: (name: string) => void;
}) {
  if (workflows.length === 0) return null;
  return (
    <section className="mb-6">
      <h2 className="mb-2 text-sm font-semibold text-fg">
        Your workflows
        <span className="ml-2 text-xs font-normal text-fg-subtle">{workflows.length}</span>
      </h2>
      <WorkflowGrid items={workflows} onDeleteCustom={onDeleteCustom} />
    </section>
  );
}

/** A few ready-made workflows with a plain "use this to" line, and the way into the rest. */
function StarterPicks({
  workflows,
  onBrowseAll,
}: {
  workflows: WorkflowMeta[];
  onBrowseAll: () => void;
}) {
  const byName = new Map(workflows.map((w) => [w.name, w] as const));
  const picks = STARTER_PICKS.flatMap((p) => {
    const w = byName.get(p.name);
    return w && !w.is_custom ? [{ workflow: w, useFor: p.useFor }] : [];
  });
  const total = workflows.filter((w) => !w.is_custom && !w.background).length;
  return (
    <div>
      {picks.length > 0 && (
        <div className="grid gap-2 sm:grid-cols-2">
          {picks.map(({ workflow: w, useFor }) => (
            <Link
              key={w.name}
              href={`/jobs/${encodeURIComponent(w.name)}`}
              className="rounded-md border border-line bg-surface/40 px-3 py-2.5 hover:border-line-strong hover:bg-surface-elevated/40 transition"
            >
              <span className="block text-sm font-medium text-fg">{w.title}</span>
              <span className="mt-0.5 block text-xs text-fg-muted">{useFor}</span>
              <span className="mt-1 block text-[11px] text-fg-subtle">
                ~{w.estimated_minutes} min
              </span>
            </Link>
          ))}
        </div>
      )}
      {total > 0 ? (
        <button
          type="button"
          onClick={onBrowseAll}
          className="mt-3 text-sm text-indigo-400 hover:text-indigo-300"
        >
          Browse all {total} ready-made workflows →
        </button>
      ) : (
        <div className="text-sm text-fg-muted">No ready-made workflows available.</div>
      )}
    </div>
  );
}

/** The full ready-made list: search, section chips, and grouped cards. */
function CatalogView({
  workflows,
  query,
  onQueryChange,
  section,
  onSectionChange,
}: {
  workflows: WorkflowMeta[];
  query: string;
  onQueryChange: (v: string) => void;
  section: SectionFilter;
  onSectionChange: (s: SectionFilter) => void;
}) {
  const matching = workflows.filter((w) =>
    matchesQuery(query, w.title, w.description)
  );
  const known = new Set<string>(SECTION_ORDER);
  const chips: { key: SectionFilter; label: string; count: number }[] = [
    { key: "all" as SectionFilter, label: "All", count: 0 },
    ...SECTION_ORDER.map((s) => ({
      key: s as SectionFilter,
      label: SECTION_CHIP_LABEL[s],
      count: 0,
    })),
    { key: "system" as SectionFilter, label: "System", count: 0 },
  ]
    .map((c) => ({ ...c, count: matching.filter((w) => inSectionFilter(w, c.key)).length }))
    // Keep the active chip even at zero so the selection stays visible.
    .filter((c) => c.key === "all" || c.key === section || c.count > 0);

  const visible = matching.filter((w) => inSectionFilter(w, section));

  // Under "All", keep light section headings so the list stays scannable;
  // any single chip is already one group, so it renders as a flat grid.
  const grouped: { label: string; items: WorkflowMeta[] }[] =
    section === "all"
      ? [
          ...SECTION_ORDER.map((s) => ({
            label: s as string,
            items: visible.filter((w) => w.section === s),
          })),
          {
            label: "Other",
            items: visible.filter((w) => !known.has(w.section)),
          },
        ].filter((g) => g.items.length > 0)
      : [];

  return (
    <div>
      <div className="mb-3">
        <SearchInput
          value={query}
          onChange={onQueryChange}
          placeholder="Search ready-made workflows…"
        />
      </div>
      <div className="mb-4 flex flex-wrap gap-1.5">
        {chips.map((c) => (
          <button
            key={c.key}
            type="button"
            onClick={() => onSectionChange(c.key)}
            className={`rounded-full px-2.5 py-1 text-xs ring-1 transition ${
              section === c.key
                ? "bg-surface-elevated ring-indigo-500/50 text-fg"
                : "ring-line text-fg-muted hover:text-fg hover:ring-line-strong"
            }`}
          >
            {c.label}
            <span className="ml-1 text-fg-subtle">{c.count}</span>
          </button>
        ))}
      </div>

      {workflows.length === 0 ? (
        <div className="text-sm text-fg-muted">No workflows yet.</div>
      ) : visible.length === 0 ? (
        <div className="text-sm text-fg-muted">
          {query ? <>No workflows match &ldquo;{query}&rdquo;.</> : "Nothing here yet."}
        </div>
      ) : section === "all" ? (
        <div className="space-y-5">
          {grouped.map((g) => (
            <div key={g.label}>
              <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-fg-subtle">
                {g.label}
                <span className="ml-2 font-normal normal-case tracking-normal">
                  {g.items.length}
                </span>
              </h2>
              <WorkflowGrid items={g.items} />
            </div>
          ))}
        </div>
      ) : (
        <WorkflowGrid items={visible} />
      )}
    </div>
  );
}

function RunsView({
  runs,
  counts,
  status,
  onStatusChange,
  query,
  onQueryChange,
  workflowTitleMap,
  collapsed,
  onToggleCollapsed,
  expanded,
  onExpand,
  onDelete,
}: {
  runs: WorkflowRunSummary[];
  counts: Record<RunStatus, number>;
  status: RunStatus;
  onStatusChange: (s: RunStatus) => void;
  query: string;
  onQueryChange: (v: string) => void;
  workflowTitleMap: Map<string, string>;
  collapsed: Record<string, boolean>;
  onToggleCollapsed: (key: string) => void;
  expanded: Record<string, boolean>;
  onExpand: (key: string) => void;
  onDelete: (runId: string) => void;
}) {
  const visible = runs.filter(
    (r) =>
      runStatusBucket(r.status) === status &&
      matchesQuery(query, r.title, r.workflow_name)
  );

  const groups = useMemo(
    () => groupBy(visible, (r) => r.workflow_name),
    [visible]
  );

  const emptyMsg =
    status === "active"
      ? "No active runs."
      : status === "awaiting"
      ? "Nothing waiting on a sign-off."
      : status === "done"
      ? "No completed runs yet."
      : "No errors.";

  return (
    <div>
      <div className="flex flex-wrap items-center gap-1 mb-4">
        <StatusSegment
          active={status === "active"}
          onClick={() => onStatusChange("active")}
          label="Active"
          count={counts.active}
          tone="amber"
        />
        <StatusSegment
          active={status === "awaiting"}
          onClick={() => onStatusChange("awaiting")}
          label="Awaiting sign-off"
          count={counts.awaiting}
          tone="amber"
        />
        <StatusSegment
          active={status === "done"}
          onClick={() => onStatusChange("done")}
          label="Done"
          count={counts.done}
          tone="emerald"
        />
        <StatusSegment
          active={status === "error"}
          onClick={() => onStatusChange("error")}
          label="Error"
          count={counts.error}
          tone="red"
        />
        <div className="w-full sm:w-auto sm:ml-auto mt-2 sm:mt-0">
          <SearchInput
            value={query}
            onChange={onQueryChange}
            placeholder="Search runs…"
          />
        </div>
      </div>

      {visible.length === 0 ? (
        <div className="text-sm text-fg-muted">
          {query ? `No runs match “${query}”.` : emptyMsg}
        </div>
      ) : (
        <div className="space-y-4">
          {Array.from(groups.entries()).map(([workflowName, items]) => {
            const title = workflowTitleMap.get(workflowName) ?? workflowName;
            const isCollapsed = !!collapsed[workflowName];
            const shown = expanded[workflowName] ? items : items.slice(0, RUNS_PER_GROUP);
            const hidden = items.length - shown.length;
            return (
              <div key={workflowName}>
                <button
                  type="button"
                  onClick={() => onToggleCollapsed(workflowName)}
                  className="w-full flex items-center gap-2 mb-2 text-left"
                >
                  <span
                    className={`text-fg-muted text-xs transition-transform ${
                      isCollapsed ? "" : "rotate-90"
                    }`}
                  >
                    ▶
                  </span>
                  <span className="text-sm font-semibold text-fg">
                    {title}
                  </span>
                  <span className="text-xs text-fg-muted">
                    {items.length} {items.length === 1 ? "run" : "runs"}
                  </span>
                </button>
                {!isCollapsed && (
                  <div className="divide-y divide-line rounded-md border border-line bg-surface/30">
                    {shown.map((r) => (
                      <div
                        key={r.run_id}
                        className="flex items-center gap-3 px-3 py-2"
                      >
                        <Link
                          href={`/jobs/runs/${encodeURIComponent(r.run_id)}`}
                          className="flex flex-1 min-w-0 items-center gap-2"
                        >
                          <span className="text-sm text-fg truncate">{r.title}</span>
                          {statusBadge(r.status)}
                          <span className="ml-auto shrink-0 text-xs text-fg-subtle">
                            {formatRelativeTime(r.updated_at)}
                          </span>
                        </Link>
                        <button
                          type="button"
                          onClick={() => onDelete(r.run_id)}
                          className="shrink-0 text-xs text-fg-muted hover:text-red-400 transition"
                          aria-label="Delete run"
                        >
                          Delete
                        </button>
                      </div>
                    ))}
                    {hidden > 0 && (
                      <button
                        type="button"
                        onClick={() => onExpand(workflowName)}
                        className="w-full px-3 py-1.5 text-left text-xs text-indigo-400 hover:text-indigo-300"
                      >
                        Show {hidden} more
                      </button>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function StatusSegment({
  active,
  onClick,
  label,
  count,
  tone,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  count: number;
  tone: "amber" | "emerald" | "red";
}) {
  const toneRing =
    tone === "amber"
      ? "ring-amber-500/40 text-amber-300"
      : tone === "emerald"
      ? "ring-emerald-500/40 text-emerald-300"
      : "ring-red-500/40 text-red-300";
  return (
    <button
      type="button"
      onClick={onClick}
      className={`px-3 py-1.5 text-xs rounded-md ring-1 transition ${
        active
          ? `bg-surface-elevated ${toneRing}`
          : "ring-line text-fg-muted hover:text-fg hover:ring-line-strong"
      }`}
    >
      {label}
      <span className="ml-1.5 text-fg-muted">{count}</span>
    </button>
  );
}

export default function JobsPage() {
  return (
    <div className="flex flex-col h-full bg-surface text-fg">
      <main className="flex-1 overflow-y-auto px-6 py-6">
        <div className="max-w-6xl mx-auto">
          <div className="mb-4 flex items-start justify-between gap-4">
            <div className="min-w-0">
              <h1 className="text-xl font-semibold text-fg">Workflows</h1>
              <p className="text-sm text-fg-muted">
                Jobs the Executive runs for you, start to finish — each one
                ends in a finished document or a task done.
              </p>
            </div>
            <Link
              href="/jobs/new"
              className="shrink-0 rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500 transition"
            >
              + New workflow
            </Link>
          </div>
          <Suspense fallback={null}>
            <JobsPageInner />
          </Suspense>
        </div>
      </main>
    </div>
  );
}
