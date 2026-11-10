"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { usePageCrumb } from "@/components/shell/PageCrumb";

import Switch from "@/components/Switch";
import Button from "@/components/ui/Button";
import OverflowMenu from "@/components/ui/OverflowMenu";
import {
  deleteWatchlistItem,
  getWatchlistItem,
  getWatchlistSignals,
  listGoals,
  listPeople,
  patchWatchlistItem,
  type GoalView,
  type Person,
  type WatchlistItem,
  type WatchlistSignal,
} from "@/lib/api";
import {
  EDGAR_DEFAULT_FORMS,
  FILING_CHOICES,
  KIND_LABELS,
  STOCK_DEFAULT_PCT,
  raiseTheBar,
  triggerInWords,
} from "@/lib/watchWords";
import { PageSpinner } from "@/components/ui/Spinner";

const fieldCls =
  "w-full px-3.5 py-2.5 text-[15px] rounded-xl bg-surface border border-line text-fg placeholder:text-fg-subtle focus:outline-none focus:ring-2 focus:ring-accent/40";
const KEYWORD_KINDS = new Set(["rss", "vendor_status", "page_watch", "query"]);

/** Relative time for display, e.g. "3h ago", "just now". */
function formatRelTime(iso: string | null): string {
  if (!iso) return "never";
  const diff = new Date(iso).getTime() - Date.now();
  // new Date("garbage") yields NaN rather than throwing.
  if (!Number.isFinite(diff)) return "a while ago";
  // The source sets a post's date. One well ahead of this clock is said as
  // such, never dressed up as recent; a little drift reads as "just now".
  if (diff > 300_000) return "in the future";
  if (diff > -60_000) return "just now";
  const abs = Math.abs(diff);
  if (abs < 3_600_000) return `${Math.round(abs / 60_000)} min ago`;
  if (abs < 86_400_000) return `${Math.round(abs / 3_600_000)}h ago`;
  return `${Math.round(abs / 86_400_000)}d ago`;
}

function whenExactly(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

// A signal's summary without the "[label]" its source leads with: it is
// shown under the watch's own name.
function cleanSummary(text: string): string {
  const line = text.replace(/^\[[^\]]{1,80}\]\s+/, "");
  return line.charAt(0).toUpperCase() + line.slice(1);
}

function lowerFirst(text: string): string {
  return /^[A-Z][a-z' ]/.test(text) ? text.charAt(0).toLowerCase() + text.slice(1) : text;
}

// The research policy's "about" stamp on a watch the Executive added.
function policyEntity(item: WatchlistItem): string | undefined {
  const raw = (item.config_json as Record<string, unknown> | undefined)?._policy;
  const entity = raw && typeof raw === "object" ? (raw as { entity?: unknown }).entity : undefined;
  return typeof entity === "string" && entity ? entity : undefined;
}

/** "The goal “…” · Sam Okafor": what the watch is tied to. */
function forInWords(item: WatchlistItem): string {
  const parts: string[] = [];
  if (item.goal_title) parts.push(`The goal “${item.goal_title}”`);
  parts.push(item.person_name ? `${item.person_name}${item.person_role ? `, ${item.person_role}` : ""}` : "You");
  return parts.join(" · ");
}

/**
 * "You dismissed 6 of the last 8 alerts from this." Offers a quieter watch,
 * stopping, or keeping it; any answer stops the question until a fresh run
 * of alerts is dismissed too (the server remembers it).
 */
function NoisyNudge({
  item,
  onPatched,
  onStop,
}: {
  item: WatchlistItem;
  onPatched: (updated: WatchlistItem, said: string) => void;
  onStop: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const noise = item.noise;
  if (!noise?.noisy) return null;
  const raise = raiseTheBar(item);

  async function answer(patch: Parameters<typeof patchWatchlistItem>[1], said: string) {
    setBusy(true);
    setErr(null);
    try {
      const updated = await patchWatchlistItem(item.slug, { ...patch, quiet_noise_nudge: true });
      onPatched(updated, said);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "That didn't save.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-2xl border border-amber-500/40 bg-amber-500/5 p-4 mb-4">
      <p className="text-[15px] text-fg">
        You dismissed {noise.recent_dismissed} of the last {noise.recent_alerts} things this told you.
        {raise ? " Only tell you about bigger changes?" : " Keep watching it?"}
      </p>
      <div className="mt-3 flex flex-wrap gap-2">
        {raise && (
          <Button variant="primary" disabled={busy} onClick={() => void answer(raise.patch, raise.done)}>
            {raise.label}
          </Button>
        )}
        <Button variant="ghost" disabled={busy} onClick={onStop}>
          Stop watching
        </Button>
        <Button variant="ghost" disabled={busy} onClick={() => void answer({}, "")}>
          Keep as is
        </Button>
      </div>
      {err && <p className="mt-2 text-sm text-rose-500">{err}</p>}
    </div>
  );
}

/** One line of "What it does": a small label, its value, and maybe an action. */
function Row({
  label,
  action,
  children,
}: {
  label: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="py-3.5 first:pt-0 last:pb-0">
      <div className="flex items-center justify-between gap-3">
        <div className="text-sm text-fg-muted">{label}</div>
        {action}
      </div>
      <div className="mt-1 min-w-0 break-words text-[15px] text-fg">{children}</div>
    </div>
  );
}

const linkBtn = "min-h-10 px-1 text-sm font-medium text-accent hover:underline disabled:opacity-60";

/** Change what makes the watch speak. Saves the trigger; the sentence above it is the result. */
function TriggerEditor({
  item,
  onSaved,
  onCancel,
}: {
  item: WatchlistItem;
  onSaved: (updated: WatchlistItem) => void;
  onCancel: () => void;
}) {
  const start = item.trigger_json ?? {};
  const kind = item.signal_type;
  const [pct, setPct] = useState(
    String(typeof start.abs_change_pct_gte === "number" ? start.abs_change_pct_gte : STOCK_DEFAULT_PCT),
  );
  const [words, setWords] = useState(
    Array.isArray(start.keywords) ? start.keywords.filter((k) => typeof k === "string").join(", ") : "",
  );
  const [forms, setForms] = useState<string[]>(
    Array.isArray(start.forms) && start.forms.length
      ? start.forms.filter((f): f is string => typeof f === "string").map((f) => f.toUpperCase())
      : EDGAR_DEFAULT_FORMS,
  );
  const [onlyBig, setOnlyBig] = useState(start.only_big === true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  function build(): Record<string, unknown> | null {
    if (kind === "stock") {
      const value = Number(pct);
      if (!Number.isFinite(value) || value <= 0 || value > 100) return null;
      return { abs_change_pct_gte: value };
    }
    const next: Record<string, unknown> = {};
    if (kind === "edgar") {
      if (!forms.length) return null;
      next.forms = forms;
    } else if (KEYWORD_KINDS.has(kind)) {
      const list = words.split(",").map((w) => w.trim()).filter(Boolean).slice(0, 20);
      if (list.length) next.keywords = list;
    }
    if (onlyBig) next.only_big = true;
    return next;
  }

  const draft = build();

  async function save() {
    if (!draft) return;
    setBusy(true);
    setErr(null);
    try {
      onSaved(await patchWatchlistItem(item.slug, { trigger: draft }));
    } catch (e) {
      setErr(e instanceof Error ? e.message : "That didn't save.");
      setBusy(false);
    }
  }

  return (
    <div className="mt-3 space-y-3 rounded-xl border border-line bg-surface p-4">
      {kind === "stock" && (
        <label className="block text-sm text-fg-muted">
          Tell me when it moves at least this much in a day (%)
          <input
            type="number"
            min={0.1}
            max={100}
            step="any"
            value={pct}
            onChange={(e) => setPct(e.target.value)}
            className={`${fieldCls} mt-1.5 max-w-[10rem]`}
          />
        </label>
      )}
      {kind === "edgar" && (
        <fieldset>
          <legend className="text-sm text-fg-muted mb-1">Tell me when they file a</legend>
          {FILING_CHOICES.map((choice) => (
            <label key={choice.value} className="flex min-h-10 items-center gap-2.5 text-[15px] text-fg">
              <input
                type="checkbox"
                checked={forms.includes(choice.value)}
                onChange={(e) =>
                  setForms((prev) =>
                    e.target.checked ? [...prev, choice.value] : prev.filter((f) => f !== choice.value),
                  )
                }
              />
              {choice.label}
            </label>
          ))}
        </fieldset>
      )}
      {KEYWORD_KINDS.has(kind) && (
        <label className="block text-sm text-fg-muted">
          Only when it mentions one of these words (commas between them; leave empty for everything)
          <input
            value={words}
            onChange={(e) => setWords(e.target.value)}
            maxLength={400}
            placeholder="e.g. pricing, layoffs"
            className={`${fieldCls} mt-1.5`}
          />
        </label>
      )}
      {kind !== "stock" && (
        <label className="flex min-h-10 items-center gap-2.5 text-[15px] text-fg">
          <input type="checkbox" checked={onlyBig} onChange={(e) => setOnlyBig(e.target.checked)} />
          Only big changes
        </label>
      )}
      <p className="text-sm text-fg-subtle">
        {draft ? `It will tell you when ${lowerFirst(triggerInWords(kind, draft))}.` : "Pick at least one, with a number between 0 and 100."}
      </p>
      {err && <p className="text-sm text-rose-500">{err}</p>}
      <div className="flex flex-wrap gap-2">
        <Button variant="primary" disabled={busy || !draft} onClick={() => void save()}>
          {busy ? "Saving…" : "Save"}
        </Button>
        <Button variant="ghost" disabled={busy} onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </div>
  );
}

/** Change the goal and the person a watch is for. */
function ForEditor({
  item,
  onSaved,
  onCancel,
}: {
  item: WatchlistItem;
  onSaved: (updated: WatchlistItem) => void;
  onCancel: () => void;
}) {
  const [goals, setGoals] = useState<GoalView[]>([]);
  const [people, setPeople] = useState<Person[]>([]);
  const [goal, setGoal] = useState<string>(item.goal_id != null ? String(item.goal_id) : "");
  const [person, setPerson] = useState<string>(
    item.route_to_person_id != null ? String(item.route_to_person_id) : "",
  );
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    listGoals().then(setGoals).catch(() => {});
    listPeople()
      .then((all) => setPeople(all.filter((p) => !p.archived && !p.is_principal)))
      .catch(() => {});
  }, []);

  async function save() {
    setBusy(true);
    setErr(null);
    try {
      onSaved(
        await patchWatchlistItem(item.slug, {
          goal_id: goal ? Number(goal) : null,
          person_id: person ? Number(person) : null,
        }),
      );
    } catch (e) {
      setErr(e instanceof Error ? e.message : "That didn't save.");
      setBusy(false);
    }
  }

  return (
    <div className="mt-3 space-y-3 rounded-xl border border-line bg-surface p-4">
      <label className="block text-sm text-fg-muted">
        Goal
        <select value={goal} onChange={(e) => setGoal(e.target.value)} className={`${fieldCls} mt-1.5`}>
          <option value="">No goal</option>
          {goals.map((g) => (
            <option key={g.id} value={String(g.id)}>
              {g.key_result}
            </option>
          ))}
        </select>
      </label>
      <label className="block text-sm text-fg-muted">
        Who it tells
        <select value={person} onChange={(e) => setPerson(e.target.value)} className={`${fieldCls} mt-1.5`}>
          <option value="">You</option>
          {people.map((p) => (
            <option key={p.id} value={String(p.id)}>
              {p.full_name}
              {p.role ? `, ${p.role}` : ""}
            </option>
          ))}
        </select>
      </label>
      {err && <p className="text-sm text-rose-500">{err}</p>}
      <div className="flex flex-wrap gap-2">
        <Button variant="primary" disabled={busy} onClick={() => void save()}>
          {busy ? "Saving…" : "Save"}
        </Button>
        <Button variant="ghost" disabled={busy} onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </div>
  );
}

function ToldRow({ signal }: { signal: WatchlistSignal }) {
  const when = signal.processed_at ?? signal.captured_at;
  return (
    <li className="py-3.5 first:pt-0 last:pb-0">
      <div className="text-[15px] text-fg break-words">{cleanSummary(signal.normalized_summary)}</div>
      <div className="mt-0.5 flex flex-wrap items-center gap-x-3 text-sm text-fg-muted">
        <span title={formatRelTime(when)}>{whenExactly(when)}</span>
        {signal.provenance_url && (
          <a href={signal.provenance_url} target="_blank" rel="noreferrer" className="text-accent hover:underline">
            Open
          </a>
        )}
      </div>
    </li>
  );
}

export default function WatchDetailPage() {
  const params = useParams<{ slug: string }>();
  const router = useRouter();
  // Next.js already decodes dynamic params; no second decode needed.
  const slug = params?.slug ?? "";

  const [item, setItem] = useState<WatchlistItem | null>(null);
  const [signals, setSignals] = useState<WatchlistSignal[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notes, setNotes] = useState("");
  const [savingNotes, setSavingNotes] = useState(false);
  const [confirming, setConfirming] = useState(false);
  // Single in-flight slot per mutation type. Prevents the optimistic
  // toggle-revert race where two clicks land in the wrong final state.
  const [toggling, setToggling] = useState(false);
  const [modeChanging, setModeChanging] = useState(false);
  // What the noisy-watch answer changed, said once it is saved.
  const [nudgeSaid, setNudgeSaid] = useState("");
  const [editing, setEditing] = useState<"trigger" | "for" | null>(null);
  usePageCrumb(item ? item.title : "Watch");

  useEffect(() => {
    if (!slug) return;
    setLoading(true);
    Promise.all([getWatchlistItem(slug), getWatchlistSignals(slug, 50)])
      .then(([w, s]) => {
        setItem(w);
        setNotes(w.notes);
        setSignals(s);
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load"))
      .finally(() => setLoading(false));
  }, [slug]);

  // A PATCH answers with the watch but not its noise block; keep the one loaded.
  function patched(updated: WatchlistItem) {
    setItem((prev) => ({ ...updated, noise: prev?.noise }));
    setEditing(null);
  }

  async function toggleEnabled() {
    if (!item || toggling) return;
    const next = !item.enabled;
    setToggling(true);
    setItem({ ...item, enabled: next });
    try {
      patched(await patchWatchlistItem(slug, { enabled: next }));
    } catch (e) {
      setItem({ ...item, enabled: !next });
      setError(e instanceof Error ? e.message : "That didn't save.");
    } finally {
      setToggling(false);
    }
  }

  async function setModeTo(nextMode: "active" | "dry_run") {
    if (!item || modeChanging || item.mode === nextMode) return;
    setModeChanging(true);
    try {
      patched(await patchWatchlistItem(slug, { mode: nextMode }));
    } catch (e) {
      setError(e instanceof Error ? e.message : "That didn't save.");
    } finally {
      setModeChanging(false);
    }
  }

  async function saveNotes() {
    if (!item || notes === item.notes) return;
    setSavingNotes(true);
    try {
      patched(await patchWatchlistItem(slug, { notes }));
    } catch (e) {
      setError(e instanceof Error ? e.message : "That didn't save.");
    } finally {
      setSavingNotes(false);
    }
  }

  async function remove() {
    try {
      await deleteWatchlistItem(slug);
      router.push("/watchlist");
    } catch (e) {
      setError(e instanceof Error ? e.message : "That didn't work.");
      setConfirming(false);
    }
  }

  if (loading) {
    return (
      <div className="flex flex-col h-full bg-surface p-6">
        <PageSpinner />
      </div>
    );
  }
  if (error && !item) {
    return (
      <div className="flex flex-col h-full bg-surface p-6">
        <Link href="/watchlist" className="text-sm text-fg-muted hover:text-fg">
          ← Watch list
        </Link>
        <div className="mt-4 px-4 py-3 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-500 text-[15px]">
          {error}
        </div>
      </div>
    );
  }
  if (!item) return null;

  // A suggestion waits for Approve on the list; it has no "Alert me" choice yet.
  const isSuggestion = item.origin === "research_proposed" && item.mode === "dry_run";
  const addedByExecutive = item.origin === "research" || item.origin === "research_proposed";
  const dismissed =
    item.dismiss_count === 0 ? "none" : item.dismiss_count >= item.fired_count ? "all of them" : String(item.dismiss_count);

  return (
    <div className="flex flex-col h-full bg-surface">
      <main className="flex-1 overflow-y-auto">
        <div className="max-w-3xl mx-auto px-4 sm:px-6 py-6">
          <Link
            href="/watchlist"
            className="text-sm text-fg-muted hover:text-fg inline-block mb-4"
          >
            ← Watch list
          </Link>

          <div className="flex items-start justify-between mb-6 gap-4">
            <div className="min-w-0">
              <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-fg break-words">
                {item.title}
              </h1>
              <p className="text-[15px] text-fg-muted mt-1 truncate" title={item.target}>
                {KIND_LABELS[item.signal_type] ?? "Watch"} · {item.target}
              </p>
            </div>
            <OverflowMenu
              label="More actions"
              items={[{ label: "Stop watching", danger: true, onSelect: () => setConfirming(true) }]}
            />
          </div>

          {confirming && (
            <div className="rounded-2xl border border-rose-500/30 bg-rose-500/5 p-4 mb-4 flex flex-wrap items-center gap-3">
              <span className="text-[15px] text-fg flex-1 min-w-[12rem]">
                Stop watching this? It comes off your list. What it already told you stays where you saw it.
              </span>
              <Button variant="danger" onClick={remove}>
                Stop watching
              </Button>
              <Button variant="ghost" onClick={() => setConfirming(false)}>
                Cancel
              </Button>
            </div>
          )}

          <div className="rounded-2xl border border-line bg-surface-elevated p-5 mb-4 divide-y divide-line">
            <div className="flex items-center justify-between gap-4 pb-4">
              <div>
                <div id="watch-enabled-label" className="text-[15px] font-semibold text-fg">
                  {item.enabled ? "Watching" : "Paused"}
                </div>
                <p className="text-sm text-fg-muted">
                  {item.enabled ? item.checks : "It does not check until you switch it back on."}
                </p>
              </div>
              {/* The label pads the small switch out to a 40px tap target. */}
              <label className="inline-flex h-10 w-12 flex-shrink-0 cursor-pointer items-center justify-center">
                <Switch
                  checked={item.enabled}
                  disabled={toggling}
                  labelledBy="watch-enabled-label"
                  onChange={() => void toggleEnabled()}
                />
              </label>
            </div>
            <div className="flex flex-wrap items-center justify-between gap-4 pt-4">
              <div>
                <div className="text-[15px] font-semibold text-fg">When something changes</div>
                <p className="text-sm text-fg-muted">
                  {isSuggestion
                    ? "It waits for your OK. Approve it on the watch list and it starts telling you."
                    : item.mode === "active"
                      ? "It tells you in your briefing."
                      : "It keeps watching quietly and doesn't tell you."}
                </p>
              </div>
              {!isSuggestion && (
                <div
                  role="group"
                  aria-label="When something changes"
                  className="inline-flex rounded-xl border border-line bg-surface p-1"
                >
                  {(
                    [
                      ["active", "Alert me"],
                      ["dry_run", "Watch quietly"],
                    ] as const
                  ).map(([value, label]) => (
                    <button
                      key={value}
                      type="button"
                      aria-pressed={item.mode === value}
                      disabled={modeChanging}
                      onClick={() => void setModeTo(value)}
                      className={`min-h-10 rounded-lg px-3.5 text-sm font-medium transition-colors disabled:opacity-60 ${
                        item.mode === value
                          ? "bg-accent/10 text-accent"
                          : "text-fg-muted hover:text-fg hover:bg-surface-overlay"
                      }`}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>

          <NoisyNudge
            item={item}
            onPatched={(updated, said) => {
              // The PATCH answer has no noise block: the question is answered.
              setItem({ ...updated, noise: undefined });
              setNudgeSaid(said);
            }}
            onStop={() => {
              setConfirming(true);
              window.scrollTo({ top: 0, behavior: "smooth" });
            }}
          />
          {nudgeSaid && (
            <p className="rounded-2xl border border-line bg-surface-elevated p-4 mb-4 text-[15px] text-fg" role="status">
              Done. {nudgeSaid}
            </p>
          )}

          {error && (
            <div className="px-4 py-3 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-500 text-[15px] mb-4">
              {error}
            </div>
          )}

          <div className="rounded-2xl border border-line bg-surface-elevated p-5 mb-4">
            <h2 className="text-base font-semibold text-fg mb-3">What it does</h2>
            <div className="divide-y divide-line">
              <Row
                label="Tells you when"
                action={
                  editing !== "trigger" && (
                    <button type="button" className={linkBtn} onClick={() => setEditing("trigger")}>
                      Change
                    </button>
                  )
                }
              >
                {item.tells_you_when}
                {editing === "trigger" && (
                  <TriggerEditor item={item} onSaved={patched} onCancel={() => setEditing(null)} />
                )}
              </Row>
              <Row
                label="Last told you"
                action={
                  item.last_told?.url && (
                    <a href={item.last_told.url} target="_blank" rel="noreferrer" className={`${linkBtn} inline-flex items-center`}>
                      Open
                    </a>
                  )
                }
              >
                {item.last_told
                  ? `${formatRelTime(item.last_told.at)}: ${lowerFirst(item.last_told.summary)}`
                  : "Has not told you anything yet."}
              </Row>
              <Row label="Last checked">
                {item.last_polled_at
                  ? `${formatRelTime(item.last_polled_at)}${item.last_check_note ? `: ${lowerFirst(item.last_check_note)}` : ""}`
                  : "Not checked yet."}
              </Row>
              <Row
                label="It is for"
                action={
                  editing !== "for" && (
                    <button type="button" className={linkBtn} onClick={() => setEditing("for")}>
                      Change
                    </button>
                  )
                }
              >
                {forInWords(item)}
                {editing === "for" && (
                  <ForEditor item={item} onSaved={patched} onCancel={() => setEditing(null)} />
                )}
              </Row>
              <Row label="Told you">
                {item.fired_count === 0
                  ? "Nothing yet."
                  : `${item.fired_count} time${item.fired_count === 1 ? "" : "s"}. You dismissed ${dismissed}.`}
              </Row>
              {addedByExecutive && (
                <Row label="Added by">
                  The Executive
                  {policyEntity(item) ? `, about ${policyEntity(item)}` : ""}
                  {item.department_title ? `, for ${item.department_title}` : ""}
                </Row>
              )}
            </div>
          </div>

          <div className="rounded-2xl border border-line bg-surface-elevated p-5 mb-4">
            <h2 className="text-base font-semibold text-fg mb-2">Notes</h2>
            <textarea
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              onBlur={saveNotes}
              rows={3}
              maxLength={500}
              placeholder="Why are we watching this?"
              aria-label="Notes"
              className="w-full px-3.5 py-2.5 text-[15px] rounded-xl bg-surface border border-line focus:outline-none focus:ring-2 focus:ring-accent/40"
            />
            {savingNotes && (
              <p className="text-xs text-fg-subtle mt-1">Saving…</p>
            )}
          </div>

          <div className="rounded-2xl border border-line bg-surface-elevated p-5 mb-4">
            <h2 className="text-base font-semibold text-fg mb-3">
              What it told you ({signals.length})
            </h2>
            {signals.length === 0 ? (
              <p className="text-sm text-fg-muted">Nothing yet. {item.enabled ? item.checks : ""}</p>
            ) : (
              <ul className="divide-y divide-line">
                {signals.map((s) => (
                  <ToldRow key={s.id} signal={s} />
                ))}
              </ul>
            )}
          </div>

        </div>
      </main>
    </div>
  );
}
