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
  listWorkflows,
} from "@/lib/api";
import StartHere from "@/components/jobs/StartHere";
import { buttonClass } from "@/components/ui/Button";
import OverflowMenu, { type OverflowItem } from "@/components/ui/OverflowMenu";
import SidePanel from "@/components/ui/SidePanel";
import { formatRelativeTime } from "@/lib/relativeTime";
import {
  RunBucket,
  runStatusBadgeColor,
  runStatusBucket,
  runStatusLabel,
} from "@/lib/runStatus";
import { cadenceLabel, cardAction, lastRunAt } from "@/lib/workflowCards";

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
// a user starts by hand. Custom workflows have their own list on the page.
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

// Ready-made workflows shown first in the ready-made panel, each with a plain
// line on when to use it. The full list sits under them.
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

type Tab = "catalog" | "runs";
type RunStatus = RunBucket;

function isTab(v: string | null): v is Tab {
  return v === "catalog" || v === "runs";
}
function isStatus(v: string | null): v is RunStatus {
  return (
    v === "active" || v === "awaiting" || v === "done" || v === "error"
  );
}

function statusBadge(status: string) {
  return (
    <span
      className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ring-1 ${runStatusBadgeColor(status)}`}
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
  // The ready-made list opens in a side panel; a chip in the URL means
  // someone was already browsing it.
  const browsing = searchParams.get("browse") === "1" || section !== "all";

  const [workflows, setWorkflows] = useState<WorkflowMeta[]>([]);
  // All custom workflows, for their schedule and on/off state. Switched-off
  // ones (e.g. saved from chat with tool steps) aren't runnable, so they are
  // absent from `workflows` until someone turns them on.
  const [customDefs, setCustomDefs] = useState<DynamicWorkflowDef[]>([]);
  const [runs, setRuns] = useState<WorkflowRunSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [catalogQuery, setCatalogQuery] = useState("");
  const [runsQuery, setRunsQuery] = useState("");
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const [wfs, rs, custom] = await Promise.all([
        listWorkflows(),
        listWorkflowRuns(),
        // Schedules and the switched-off list — never fail the whole page over it.
        listCustomWorkflows().catch(() => [] as DynamicWorkflowDef[]),
      ]);
      setWorkflows(wfs);
      setRuns(rs);
      setCustomDefs(custom);
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

  const runCounts = useMemo(() => {
    const c = { active: 0, awaiting: 0, done: 0, error: 0 };
    for (const r of runs) c[runStatusBucket(r.status)]++;
    return c;
  }, [runs]);

  // Default the runs status once data is loaded and ?status is missing/invalid.
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

  const readyMade = workflows.filter((w) => !w.is_custom);
  const readyMadeCount = readyMade.filter((w) => !w.background).length;
  const offWorkflows = customDefs.filter((d) => !d.is_active);
  const ownWorkflows = workflows.filter((w) => w.is_custom && !w.background);
  const openRuns = runCounts.active + runCounts.awaiting;

  return (
    <>
      {tab === "catalog" && (
        <StartHere
          readyMadeCount={readyMadeCount}
          onBrowseReadyMade={() => setParam({ browse: "1" })}
        />
      )}

      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-xl font-bold tracking-tight text-fg">
          {tab === "runs" ? "Runs" : "Your workflows"}
        </h2>
        <ViewSwitch
          tab={tab}
          runsLabel={openRuns > 0 ? `Runs (${openRuns} open)` : `Runs (${runs.length})`}
          onChange={(t) => setParam({ tab: t === "catalog" ? null : t })}
        />
      </div>

      {loading && (
        <div className="text-[15px] text-fg-muted">Loading workflows…</div>
      )}
      {error && (
        <div className="mb-4 text-[15px] text-red-400">Error: {error}</div>
      )}

      {!loading && tab === "catalog" && (
        <YourWorkflows
          workflows={ownWorkflows}
          offWorkflows={offWorkflows}
          customDefs={customDefs}
          runs={runs}
          onDeleteCustom={handleDeleteCustom}
          onBrowse={() => setParam({ browse: "1" })}
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

      <SidePanel
        open={browsing && !loading}
        onClose={() => setParam({ browse: null, section: null })}
        title="Ready-made workflows"
        subtitle="Templates you can run as they are: fill in a few details and go."
        width="lg"
      >
        <CatalogView
          workflows={readyMade}
          query={catalogQuery}
          onQueryChange={setCatalogQuery}
          section={section}
          onSectionChange={(s) =>
            setParam({ browse: "1", section: s === "all" ? null : s })
          }
        />
      </SidePanel>
    </>
  );
}

/** Your workflows · Runs: one control, beside the list's heading. */
function ViewSwitch({
  tab,
  runsLabel,
  onChange,
}: {
  tab: Tab;
  runsLabel: string;
  onChange: (t: Tab) => void;
}) {
  const items: { key: Tab; label: string }[] = [
    { key: "catalog", label: "Your workflows" },
    { key: "runs", label: runsLabel },
  ];
  return (
    <div
      role="group"
      aria-label="Show"
      className="inline-flex max-w-full overflow-x-auto rounded-xl border border-line bg-surface-elevated p-1"
    >
      {items.map((it) => (
        <button
          key={it.key}
          type="button"
          aria-pressed={tab === it.key}
          onClick={() => onChange(it.key)}
          className={`min-h-10 flex-shrink-0 rounded-lg px-3.5 text-sm font-medium transition-colors ${
            tab === it.key
              ? "bg-accent/10 text-accent"
              : "text-fg-muted hover:text-fg hover:bg-surface-overlay"
          }`}
        >
          {it.label}
        </button>
      ))}
    </div>
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
      aria-label={placeholder.replace(/…$/, "")}
      className="h-11 w-full sm:w-80 px-4 text-[15px] rounded-xl bg-surface-elevated border border-line text-fg placeholder:text-fg-subtle focus:outline-none focus:ring-2 focus:ring-accent/40"
    />
  );
}

const cardCls =
  "flex min-w-0 flex-col rounded-2xl border border-line bg-surface-elevated p-5 shadow-sm";

/**
 * The user's own workflows as cards, one primary button each. Ones waiting
 * for approval (switched off) come first.
 */
function YourWorkflows({
  workflows,
  offWorkflows,
  customDefs,
  runs,
  onDeleteCustom,
  onBrowse,
}: {
  workflows: WorkflowMeta[];
  offWorkflows: DynamicWorkflowDef[];
  customDefs: DynamicWorkflowDef[];
  runs: WorkflowRunSummary[];
  onDeleteCustom: (name: string) => void;
  onBrowse: () => void;
}) {
  const cadenceByName = new Map(customDefs.map((d) => [d.name, d.cadence] as const));

  if (workflows.length === 0 && offWorkflows.length === 0) {
    return (
      <div className="rounded-2xl border border-dashed border-line px-5 py-8 text-center">
        <p className="text-[15px] text-fg-muted">
          No workflows of your own yet. Describe one above, or start from a
          ready-made one.
        </p>
        <button type="button" onClick={onBrowse} className={buttonClass("secondary", "md", "mt-4")}>
          Browse ready-made
        </button>
      </div>
    );
  }

  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
      {offWorkflows.map((d) => {
        const tools = new Set(d.steps.flatMap((s) => (s.kind === "action" ? s.tools : [])));
        const href = `/jobs/${encodeURIComponent(d.name)}`;
        return (
          <div key={`off:${d.name}`} className={`${cardCls} border-amber-500/40`}>
            <h3 className="text-lg font-semibold leading-snug text-fg">{d.title}</h3>
            <div className="mt-2 flex flex-wrap items-center gap-2 text-sm text-fg-muted">
              <span className="rounded-full bg-amber-500/15 px-2.5 py-0.5 text-xs font-medium text-amber-600 dark:text-amber-300">
                Waiting for your approval
              </span>
              <span>
                Off
                {tools.size > 0 && ` · ${tools.size} ${tools.size === 1 ? "tool" : "tools"}`}
              </span>
            </div>
            <div className="mt-auto flex items-center gap-2 pt-5">
              <Link href={href} className={buttonClass("primary", "md")}>
                Review
              </Link>
              <OverflowMenu
                label={`More for ${d.title}`}
                items={[
                  { label: "Edit", href: `/jobs/new?edit=${encodeURIComponent(d.name)}` },
                  { label: "Delete", danger: true, onSelect: () => onDeleteCustom(d.name) },
                ]}
              />
            </div>
          </div>
        );
      })}
      {workflows.map((w) => {
        const href = `/jobs/${encodeURIComponent(w.name)}`;
        const action = cardAction(w.name, true, runs);
        const last = lastRunAt(w.name, runs);
        const cadence = cadenceLabel(cadenceByName.get(w.name));
        const meta = [
          cadence || `${w.steps.length} steps`,
          last ? `last run ${formatRelativeTime(last)}` : "not run yet",
        ].join(" · ");
        const more: OverflowItem[] = [
          ...(action.kind === "signoff" ? [{ label: "Run again", href }] : []),
          { label: "Edit", href: `/jobs/new?edit=${encodeURIComponent(w.name)}` },
          { label: "Delete", danger: true, onSelect: () => onDeleteCustom(w.name) },
        ];
        return (
          <div key={w.name} className={cardCls}>
            <Link href={href} className="min-w-0" title={w.description}>
              <h3 className="text-lg font-semibold leading-snug text-fg hover:text-accent transition-colors">
                {w.title}
              </h3>
            </Link>
            {action.kind === "signoff" ? (
              <span className="mt-2 inline-flex w-fit rounded-full bg-amber-500/15 px-2.5 py-0.5 text-xs font-medium text-amber-600 dark:text-amber-300">
                Waiting for sign-off
              </span>
            ) : (
              <p className="mt-1.5 text-sm text-fg-muted">{meta}</p>
            )}
            {w.description && (
              <p className="mt-2 line-clamp-2 text-sm text-fg-subtle">{w.description}</p>
            )}
            <div className="mt-auto flex items-center gap-2 pt-5">
              {action.kind === "signoff" ? (
                <Link
                  href={`/jobs/runs/${encodeURIComponent(action.runId)}`}
                  className={buttonClass("primary", "md")}
                >
                  Review
                </Link>
              ) : (
                <Link href={href} className={buttonClass("primary", "md")}>
                  Run
                </Link>
              )}
              <OverflowMenu label={`More for ${w.title}`} items={more} />
            </div>
          </div>
        );
      })}
    </div>
  );
}

function CatalogCard({ workflow: w, useFor }: { workflow: WorkflowMeta; useFor?: string }) {
  return (
    <Link
      href={`/jobs/${encodeURIComponent(w.name)}`}
      title={w.description}
      className="block min-w-0 rounded-xl border border-line bg-surface-elevated px-4 py-3.5 hover:border-line-strong hover:bg-surface-overlay transition-colors"
    >
      <span className="block text-[15px] font-semibold text-fg">{w.title}</span>
      <span className="mt-1 block line-clamp-2 text-sm text-fg-muted">
        {useFor ?? w.description}
      </span>
      <span className="mt-1.5 block text-xs text-fg-subtle">
        {w.quick ? (
          <span className="mr-1.5 rounded-full bg-emerald-500/10 px-2 py-0.5 font-medium text-emerald-500">
            Quick
          </span>
        ) : null}
        {w.quick ? "One question, one document" : `${w.steps.length} steps`} · ~
        {w.estimated_minutes} min
      </span>
    </Link>
  );
}

function CatalogGrid({ items }: { items: WorkflowMeta[] }) {
  return (
    <div className="grid gap-2.5 sm:grid-cols-2">
      {items.map((w) => (
        <CatalogCard key={w.name} workflow={w} />
      ))}
    </div>
  );
}

/**
 * The ready-made list in its panel: a few good first picks, then search,
 * section chips and every template grouped by section.
 */
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
  const byName = new Map(workflows.map((w) => [w.name, w] as const));
  const picks = STARTER_PICKS.flatMap((p) => {
    const w = byName.get(p.name);
    return w ? [{ workflow: w, useFor: p.useFor }] : [];
  });
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
    <div className="space-y-5">
      {picks.length > 0 && !query && section === "all" && (
        <div>
          <h3 className="mb-2 text-sm font-semibold text-fg">Good places to start</h3>
          <div className="grid gap-2.5 sm:grid-cols-2">
            {picks.map(({ workflow, useFor }) => (
              <CatalogCard key={workflow.name} workflow={workflow} useFor={useFor} />
            ))}
          </div>
        </div>
      )}

      <div className="space-y-3">
        <SearchInput
          value={query}
          onChange={onQueryChange}
          placeholder="Search ready-made workflows…"
        />
        <div className="flex flex-wrap gap-2">
          {chips.map((c) => (
            <button
              key={c.key}
              type="button"
              aria-pressed={section === c.key}
              onClick={() => onSectionChange(c.key)}
              className={`min-h-10 rounded-full border px-3.5 text-sm transition-colors ${
                section === c.key
                  ? "border-accent/50 bg-accent/10 text-accent font-medium"
                  : "border-line text-fg-muted hover:text-fg hover:border-line-strong"
              }`}
            >
              {c.label}
              <span className="ml-1.5 text-fg-subtle">{c.count}</span>
            </button>
          ))}
        </div>
      </div>

      {workflows.length === 0 ? (
        <div className="text-[15px] text-fg-muted">No ready-made workflows available.</div>
      ) : visible.length === 0 ? (
        <div className="text-[15px] text-fg-muted">
          {query ? <>No workflows match &ldquo;{query}&rdquo;.</> : "Nothing here yet."}
        </div>
      ) : section === "all" ? (
        <div className="space-y-5">
          {grouped.map((g) => (
            <div key={g.label}>
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-fg-subtle">
                {g.label}
                <span className="ml-2 font-normal normal-case tracking-normal">
                  {g.items.length}
                </span>
              </h3>
              <CatalogGrid items={g.items} />
            </div>
          ))}
        </div>
      ) : (
        <CatalogGrid items={visible} />
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
      <div className="flex flex-wrap items-center gap-2 mb-5">
        <StatusSegment
          active={status === "active"}
          onClick={() => onStatusChange("active")}
          label="Active"
          count={counts.active}
        />
        <StatusSegment
          active={status === "awaiting"}
          onClick={() => onStatusChange("awaiting")}
          label="Awaiting sign-off"
          count={counts.awaiting}
        />
        <StatusSegment
          active={status === "done"}
          onClick={() => onStatusChange("done")}
          label="Done"
          count={counts.done}
        />
        <StatusSegment
          active={status === "error"}
          onClick={() => onStatusChange("error")}
          label="Error"
          count={counts.error}
        />
        <div className="w-full sm:w-auto sm:ml-auto">
          <SearchInput
            value={query}
            onChange={onQueryChange}
            placeholder="Search runs…"
          />
        </div>
      </div>

      {visible.length === 0 ? (
        <div className="text-[15px] text-fg-muted">
          {query ? `No runs match “${query}”.` : emptyMsg}
        </div>
      ) : (
        <div className="space-y-6">
          {Array.from(groups.entries()).map(([workflowName, items]) => {
            const title = workflowTitleMap.get(workflowName) ?? workflowName;
            const isCollapsed = !!collapsed[workflowName];
            const shown = expanded[workflowName] ? items : items.slice(0, RUNS_PER_GROUP);
            const hidden = items.length - shown.length;
            return (
              <div key={workflowName}>
                <button
                  type="button"
                  aria-expanded={!isCollapsed}
                  onClick={() => onToggleCollapsed(workflowName)}
                  className="mb-2 flex min-h-10 w-full items-center gap-2 text-left"
                >
                  <span
                    aria-hidden="true"
                    className={`text-fg-muted text-xs transition-transform ${
                      isCollapsed ? "" : "rotate-90"
                    }`}
                  >
                    ▶
                  </span>
                  <span className="text-base font-semibold text-fg">
                    {title}
                  </span>
                  <span className="text-sm text-fg-muted">
                    {items.length} {items.length === 1 ? "run" : "runs"}
                  </span>
                </button>
                {!isCollapsed && (
                  <div className="divide-y divide-line rounded-2xl border border-line bg-surface-elevated">
                    {shown.map((r) => (
                      <div
                        key={r.run_id}
                        className="flex items-center gap-2 py-1.5 pl-4 pr-2"
                      >
                        <Link
                          href={`/jobs/runs/${encodeURIComponent(r.run_id)}`}
                          className="flex min-h-11 min-w-0 flex-1 flex-wrap items-center gap-x-3 gap-y-1"
                        >
                          <span className="min-w-0 truncate text-[15px] text-fg">{r.title}</span>
                          {statusBadge(r.status)}
                          <span className="ml-auto shrink-0 text-sm text-fg-subtle">
                            {formatRelativeTime(r.updated_at)}
                          </span>
                        </Link>
                        <OverflowMenu
                          label={`More for ${r.title}`}
                          items={[
                            { label: "Delete run", danger: true, onSelect: () => onDelete(r.run_id) },
                          ]}
                        />
                      </div>
                    ))}
                    {hidden > 0 && (
                      <button
                        type="button"
                        onClick={() => onExpand(workflowName)}
                        className="min-h-11 w-full px-4 text-left text-sm font-medium text-accent hover:underline"
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
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  count: number;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={`min-h-10 rounded-full border px-4 text-sm transition-colors ${
        active
          ? "border-accent/50 bg-accent/10 text-accent font-medium"
          : "border-line text-fg-muted hover:text-fg hover:border-line-strong"
      }`}
    >
      {label}
      <span className="ml-1.5 text-fg-subtle">{count}</span>
    </button>
  );
}

export default function JobsPage() {
  return (
    <div className="flex flex-col h-full bg-surface text-fg">
      <main className="flex-1 overflow-y-auto px-4 sm:px-6 py-6 sm:py-8">
        <div className="max-w-6xl mx-auto">
          <Suspense fallback={null}>
            <JobsPageInner />
          </Suspense>
        </div>
      </main>
    </div>
  );
}
