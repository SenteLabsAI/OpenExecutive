"use client";

import { useCallback, useEffect, useState } from "react";
import {
  ExternalPeekChunk,
  ReviewAnnotation,
  ReviewItem,
  ReviewPriority,
  ReviewStatus,
  addAnnotation,
  bulkApproveReviewItems,
  curateDomain,
  deleteAnnotation,
  getBuiltinFile,
  getFailureFile,
  getReviewItem,
  getTrustedDefaults,
  listAllAnnotations,
  listItemAnnotations,
  listReviewItems,
  patchAnnotation,
  patchReviewItem,
  peekExternalSource,
  updateBuiltinFile,
} from "@/lib/api";
import StatusPill from "@/components/ReviewStatusPill";
import Button from "@/components/ui/Button";
import OverflowMenu, { type OverflowItem } from "@/components/ui/OverflowMenu";
import SidePanel from "@/components/ui/SidePanel";
import SectionTabs from "@/components/ui/SectionTabs";
import Icon from "@/components/Icon";
import { domainBulkActions, type BulkAction } from "@/lib/reviewBulk";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const PRIORITY_CLASSES: Record<ReviewPriority, string> = {
  high: "text-blue-500",
  normal: "",
  low: "text-fg-subtle",
};

function PriorityPill({ priority }: { priority: ReviewPriority }) {
  if (priority === "normal") return null;
  return (
    <span className={`text-xs font-medium ${PRIORITY_CLASSES[priority]}`}>
      {priority === "high" ? "↑ High priority" : "↓ Low priority"}
    </span>
  );
}

const PRIORITY_LABELS: Record<ReviewPriority, string> = {
  high: "↑ High",
  normal: "Normal",
  low: "↓ Low",
};

/** The status actions an item offers besides Approve, plus its priority
 * choices; shared by a row's ⋯ menu and the item panel's. */
function itemMenu(
  item: ReviewItem,
  actions: {
    onView?: () => void;
    onFlag: () => void;
    onReject: () => void;
    onPriorityChange: (p: ReviewPriority) => void;
  },
): OverflowItem[] {
  const menu: OverflowItem[] = [];
  if (actions.onView) menu.push({ label: "View details", onSelect: actions.onView });
  if (item.status !== "needs_revision") menu.push({ label: "Flag for revision", onSelect: actions.onFlag });
  for (const p of ["high", "normal", "low"] as ReviewPriority[]) {
    menu.push({
      label: `Priority: ${PRIORITY_LABELS[p]}${item.priority === p ? " ✓" : ""}`,
      disabled: item.priority === p,
      onSelect: () => actions.onPriorityChange(p),
    });
  }
  if (item.status !== "rejected") menu.push({ label: "Reject…", danger: true, onSelect: actions.onReject });
  return menu;
}

const SELECT_CLASS =
  "h-10 rounded-xl border border-line-strong bg-surface-elevated px-3 text-sm text-fg focus:outline-none focus:ring-2 focus:ring-accent/50";

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

// ---------------------------------------------------------------------------
// Slide-over detail panel
// ---------------------------------------------------------------------------

function ItemSlideOver({
  itemId,
  onClose,
  onUpdated,
  onApprove,
  onFlag,
  onReject,
  onPriorityChange,
}: {
  itemId: string;
  onClose: () => void;
  onUpdated: (item: ReviewItem) => void;
  onApprove: (item: ReviewItem) => Promise<void>;
  onFlag: (item: ReviewItem) => Promise<ReviewItem>;
  onReject: (item: ReviewItem) => void;
  onPriorityChange: (item: ReviewItem, p: ReviewPriority) => Promise<ReviewItem>;
}) {
  const [detail, setDetail] = useState<{ item: ReviewItem; annotations: ReviewAnnotation[] } | null>(null);
  const [content, setContent] = useState<string | null>(null);
  const [contentLoading, setContentLoading] = useState(false);
  const [contentError, setContentError] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editDraft, setEditDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [newCorrection, setNewCorrection] = useState("");
  const [addingAnnotation, setAddingAnnotation] = useState(false);
  const [notes, setNotes] = useState("");
  const [externalChunks, setExternalChunks] = useState<ExternalPeekChunk[]>([]);
  const [externalChunkLimit, setExternalChunkLimit] = useState(10);
  const [loadingChunks, setLoadingChunks] = useState(false);

  useEffect(() => {
    getReviewItem(itemId).then((d) => {
      setDetail(d);
      setNotes(d.item.reviewer_notes);
    });
  }, [itemId]);

  const itemId_stable = detail?.item.item_id;

  // Reset all content state when the item changes.
  useEffect(() => {
    setContent(null);
    setContentLoading(false);
    setContentError(false);
    setExternalChunks([]);
    setExternalChunkLimit(10);
    setLoadingChunks(false);
  }, [itemId_stable]);

  useEffect(() => {
    // Failure case studies are file-backed too; without this an SME sees no
    // content and is pushed toward approving something they cannot read.
    const fileBacked =
      detail?.item.content_type === "builtin" || detail?.item.content_type === "failure";
    if (!detail || !fileBacked) return;
    const [, domain, filename] = detail.item.item_id.split(":");
    const fetchFile =
      detail.item.content_type === "failure" ? getFailureFile : getBuiltinFile;
    let cancelled = false;
    setContentLoading(true);
    setContentError(false);
    setContent(null);
    fetchFile(domain, filename)
      .then((f) => { if (!cancelled) setContent(f.content); })
      .catch(() => { if (!cancelled) { setContent(null); setContentError(true); } })
      .finally(() => { if (!cancelled) setContentLoading(false); });
    return () => { cancelled = true; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [itemId_stable]);

  useEffect(() => {
    if (!detail || detail.item.content_type !== "external") return;
    let cancelled = false;
    setLoadingChunks(true);
    peekExternalSource(detail.item.filename, externalChunkLimit)
      .then((r) => { if (!cancelled) setExternalChunks(r.chunks); })
      .catch(() => { if (!cancelled) setExternalChunks([]); })
      .finally(() => { if (!cancelled) setLoadingChunks(false); });
    return () => { cancelled = true; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [itemId_stable, externalChunkLimit]);

  const refreshAnnotations = useCallback(() => {
    listItemAnnotations(itemId).then((anns) => {
      setDetail((prev) => prev ? { ...prev, annotations: anns } : prev);
    });
  }, [itemId]);

  const handleSaveEdit = async () => {
    if (!detail || editDraft === content) { setEditing(false); return; }
    setSaving(true);
    try {
      const [, domain, filename] = detail.item.item_id.split(":");
      await updateBuiltinFile(domain, filename, editDraft);
      setContent(editDraft);
      setEditing(false);
      // Reload item — status will have reset to needs_revision server-side
      const updated = await getReviewItem(itemId);
      setDetail(updated);
      onUpdated(updated.item);
    } finally {
      setSaving(false);
    }
  };

  const handleAddAnnotation = async () => {
    if (!newCorrection.trim()) return;
    setAddingAnnotation(true);
    try {
      await addAnnotation(itemId, newCorrection.trim());
      setNewCorrection("");
      refreshAnnotations();
    } finally {
      setAddingAnnotation(false);
    }
  };

  const handleToggleAnnotation = async (ann: ReviewAnnotation) => {
    await patchAnnotation(ann.annotation_id, { is_active: !ann.is_active });
    refreshAnnotations();
  };

  const handleDeleteAnnotation = async (annId: string) => {
    await deleteAnnotation(annId);
    refreshAnnotations();
  };

  const handleSaveNotes = async () => {
    if (!detail) return;
    const updated = await patchReviewItem(itemId, { reviewer_notes: notes });
    setDetail((prev) => prev ? { ...prev, item: updated } : prev);
    onUpdated(updated);
  };

  const item = detail?.item;
  const annotations = detail?.annotations ?? [];
  const isBuiltin = item?.content_type === "builtin" || item?.content_type === "failure";
  const isExternal = item?.content_type === "external";

  return (
    <SidePanel
      open
      onClose={onClose}
      width="lg"
      title={<span className="block break-words">{item ? item.filename : "Loading…"}</span>}
      subtitle={
        item && (
          <span className="flex items-center gap-2 flex-wrap">
            <span className="capitalize">{item.domain}</span>
            <span aria-hidden>·</span>
            <span>{item.content_type}</span>
            <StatusPill
              status={item.status}
              reviewedAt={item.reviewed_at}
              trustedDefault={item.trusted_default}
            />
            <PriorityPill priority={item.priority} />
          </span>
        )
      }
      footer={
        item && (
          <div className="flex items-center gap-2">
            {item.status !== "approved" && (
              <Button
                variant="primary"
                onClick={async () => {
                  await onApprove(item);
                  onClose();
                }}
              >
                Approve
              </Button>
            )}
            <OverflowMenu
              placement="up"
              align="left"
              label="More review actions"
              items={itemMenu(item, {
                onFlag: async () => {
                  const updated = await onFlag(item);
                  setDetail((prev) => (prev ? { ...prev, item: updated } : prev));
                },
                onReject: () => {
                  onReject(item);
                  onClose();
                },
                onPriorityChange: async (p) => {
                  const updated = await onPriorityChange(item, p);
                  setDetail((prev) => (prev ? { ...prev, item: updated } : prev));
                },
              })}
            />
          </div>
        )
      }
    >
      {!item ? (
        <p className="text-[15px] text-fg-muted">Loading…</p>
      ) : (
        <div className="space-y-7">
          {/* Builtin file content */}
          {isBuiltin && (
            <section>
              <div className="flex items-center justify-between mb-2">
                <p className="text-base font-semibold text-fg">Document content</p>
                {!editing && content != null && (
                  <button
                    onClick={() => { setEditDraft(content); setEditing(true); }}
                    className="h-9 px-2 text-sm font-medium text-accent hover:underline"
                  >
                    Edit
                  </button>
                )}
              </div>
              {editing ? (
                <div className="space-y-2">
                  <textarea
                    value={editDraft}
                    onChange={(e) => setEditDraft(e.target.value)}
                    className="w-full h-72 bg-surface-elevated border border-line-strong rounded-xl px-3 py-2 text-sm text-fg font-mono resize-y focus:outline-none focus:ring-2 focus:ring-accent/50"
                  />
                  <div className="flex gap-2">
                    <Button variant="primary" size="sm" className="!h-10" onClick={handleSaveEdit} disabled={saving}>
                      {saving ? "Saving…" : "Save"}
                    </Button>
                    <Button variant="ghost" size="sm" className="!h-10" onClick={() => setEditing(false)}>
                      Cancel
                    </Button>
                  </div>
                </div>
              ) : contentLoading ? (
                <p className="text-sm text-fg-subtle">Loading…</p>
              ) : contentError ? (
                <p className="text-sm text-red-500">Could not load file content.</p>
              ) : content != null ? (
                <pre className="text-sm text-fg bg-surface-overlay/50 border border-line rounded-xl p-3.5 overflow-x-auto whitespace-pre-wrap font-mono max-h-96">{content}</pre>
              ) : null}
            </section>
          )}

          {/* External source chunks */}
          {isExternal && (
            <section>
              <div className="flex items-center justify-between mb-2">
                <p className="text-base font-semibold text-fg">
                  Source content
                </p>
                {loadingChunks && <span className="text-sm text-fg-subtle">Loading…</span>}
              </div>
              <p className="text-sm text-fg-subtle mb-3">
                Indexed chunks from <span className="text-fg-muted">{item.filename}</span> — this is the text the AI retrieves from this source.
              </p>
              {!loadingChunks && externalChunks.length === 0 && (
                <div className="bg-surface-overlay/50 border border-line rounded-xl p-4 text-center">
                  <p className="text-sm text-fg-muted mb-1">No indexed chunks found.</p>
                  <p className="text-sm text-fg-subtle">
                    Run <code className="bg-surface-overlay px-1 rounded font-mono">openexecutive ingest-oer</code> to index this source.
                  </p>
                </div>
              )}
              <div className="space-y-2">
                {externalChunks.map((chunk) => (
                  <div key={`${chunk.filename}:${chunk.chunk_index}`} className="bg-surface-overlay/50 border border-line rounded-xl p-3.5">
                    <div className="flex items-center gap-2 mb-1.5">
                      <span className="text-xs text-fg-muted capitalize">{chunk.domain}</span>
                      <span className="text-xs text-fg-subtle font-mono">{chunk.filename} · chunk {chunk.chunk_index}</span>
                    </div>
                    <p className="text-sm text-fg leading-relaxed">{chunk.text}</p>
                  </div>
                ))}
              </div>
              {externalChunks.length > 0 && externalChunks.length >= externalChunkLimit && (
                <button
                  onClick={() => setExternalChunkLimit((n) => n + 10)}
                  disabled={loadingChunks}
                  className="mt-3 h-10 text-sm font-medium text-accent hover:underline disabled:opacity-40"
                >
                  Load 10 more chunks…
                </button>
              )}
            </section>
          )}

          {/* Notes */}
          <section>
            <p className="text-base font-semibold text-fg mb-2">Reviewer notes</p>
            <textarea
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              onBlur={handleSaveNotes}
              placeholder="Add your review notes here…"
              className="w-full h-24 bg-surface-elevated border border-line-strong rounded-xl px-3 py-2 text-[15px] text-fg resize-none focus:outline-none focus:ring-2 focus:ring-accent/50 placeholder:text-fg-subtle"
            />
          </section>

          {/* Annotations */}
          <section>
            <p className="text-base font-semibold text-fg">SME corrections</p>
            <p className="text-sm text-fg-subtle mb-2">Injected into AI retrieval context.</p>
            <div className="space-y-2">
              {annotations.map((ann) => (
                <div
                  key={ann.annotation_id}
                  className={`flex items-start gap-3 px-3.5 py-2.5 rounded-xl border ${ann.is_active ? "bg-surface-overlay/50 border-line" : "border-line/60 opacity-60"}`}
                >
                  <div className="flex-1 min-w-0">
                    <p className="text-[15px] text-fg">{ann.correction}</p>
                    <p className="mt-1 flex items-center gap-1.5 text-xs text-fg-muted">
                      <span
                        aria-hidden
                        className={`h-2 w-2 rounded-full ${ann.is_active ? "bg-emerald-500" : "bg-fg-subtle"}`}
                      />
                      {ann.is_active ? "Active" : "Inactive"}
                    </p>
                  </div>
                  <OverflowMenu
                    size="sm"
                    label="Correction actions"
                    items={[
                      {
                        label: ann.is_active ? "Turn off" : "Turn on",
                        onSelect: () => void handleToggleAnnotation(ann),
                      },
                      {
                        label: "Delete",
                        danger: true,
                        onSelect: () => void handleDeleteAnnotation(ann.annotation_id),
                      },
                    ]}
                  />
                </div>
              ))}
              <div className="flex gap-2">
                <input
                  value={newCorrection}
                  onChange={(e) => setNewCorrection(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void handleAddAnnotation(); } }}
                  placeholder="Add a correction or clarification…"
                  aria-label="New correction"
                  className="flex-1 min-w-0 h-11 bg-surface-elevated border border-line-strong rounded-xl px-3 text-[15px] text-fg focus:outline-none focus:ring-2 focus:ring-accent/50 placeholder:text-fg-subtle"
                />
                <Button
                  onClick={handleAddAnnotation}
                  disabled={addingAnnotation || !newCorrection.trim()}
                >
                  Add
                </Button>
              </div>
            </div>
          </section>
        </div>
      )}
    </SidePanel>
  );
}

// ---------------------------------------------------------------------------
// Main ReviewQueue component
// ---------------------------------------------------------------------------

type Tab = "queue" | "all" | "annotations";
type RejectModalState = { itemId: string; notes: string } | null;

export default function ReviewQueue() {
  const [tab, setTab] = useState<Tab>("queue");
  const [items, setItems] = useState<ReviewItem[]>([]);
  const [allItems, setAllItems] = useState<ReviewItem[]>([]);
  const [annotations, setAnnotations] = useState<ReviewAnnotation[]>([]);
  const [loading, setLoading] = useState(true);
  const [filterStatus, setFilterStatus] = useState<ReviewStatus | "">("");
  const [filterDomain, setFilterDomain] = useState("");
  const [filterType, setFilterType] = useState<"builtin" | "external" | "">("");
  const [selectedItemId, setSelectedItemId] = useState<string | null>(null);
  const [rejectModal, setRejectModal] = useState<RejectModalState>(null);
  const [trustedDefaults, setTrustedDefaults] = useState<Record<string, number>>({});
  const [pendingItems, setPendingItems] = useState<ReviewItem[]>([]);
  // The domain the "Bulk actions" menu acts on ("" until one is picked).
  const [bulkDomain, setBulkDomain] = useState("");

  const loadQueue = useCallback(async () => {
    setLoading(true);
    try {
      const [pending, needsRevision, defaults] = await Promise.all([
        listReviewItems({ status: "pending", limit: 200 }),
        listReviewItems({ status: "needs_revision", limit: 200 }),
        getTrustedDefaults().catch(() => ({}) as Record<string, number>),
      ]);
      setItems([...pending, ...needsRevision]);
      setTrustedDefaults(defaults);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadAll = useCallback(async () => {
    setLoading(true);
    try {
      // The per-domain action row must reflect what the SERVER would do, so it
      // reads its own unfiltered sources. Deriving those counts from the
      // filtered, 500-capped `allItems` made the buttons vanish whenever a
      // status filter was set, and undercounted past the cap.
      const [result, defaults, pending] = await Promise.all([
        listReviewItems({
          status: filterStatus || undefined,
          domain: filterDomain || undefined,
          content_type: filterType || undefined,
          limit: 500,
        }),
        getTrustedDefaults().catch(() => ({}) as Record<string, number>),
        listReviewItems({ status: "pending", limit: 500 }),
      ]);
      setAllItems(result);
      setTrustedDefaults(defaults);
      setPendingItems(pending);
    } finally {
      setLoading(false);
    }
  }, [filterStatus, filterDomain, filterType]);

  const loadAnnotations = useCallback(async () => {
    setLoading(true);
    try {
      const anns = await listAllAnnotations(true);
      setAnnotations(anns);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (tab === "queue") loadQueue();
    else if (tab === "all") loadAll();
    else loadAnnotations();
  }, [tab, loadQueue, loadAll, loadAnnotations]);

  const handleApprove = async (item: ReviewItem) => {
    const updated = await patchReviewItem(item.item_id, { status: "approved" });
    setItems((prev) => prev.filter((i) => i.item_id !== item.item_id));
    setAllItems((prev) => prev.map((i) => (i.item_id === updated.item_id ? updated : i)));
  };

  const handleFlag = async (item: ReviewItem) => {
    const updated = await patchReviewItem(item.item_id, { status: "needs_revision" });
    setItems((prev) => prev.map((i) => (i.item_id === updated.item_id ? updated : i)));
    setAllItems((prev) => prev.map((i) => (i.item_id === updated.item_id ? updated : i)));
    return updated;
  };

  const handleRejectConfirm = async () => {
    if (!rejectModal) return;
    const updated = await patchReviewItem(rejectModal.itemId, {
      status: "rejected",
      reviewer_notes: rejectModal.notes,
    });
    setItems((prev) => prev.filter((i) => i.item_id !== rejectModal.itemId));
    setAllItems((prev) => prev.map((i) => (i.item_id === updated.item_id ? updated : i)));
    setRejectModal(null);
  };

  const handlePriorityChange = async (item: ReviewItem, priority: ReviewPriority) => {
    const updated = await patchReviewItem(item.item_id, { priority });
    setItems((prev) => prev.map((i) => (i.item_id === updated.item_id ? updated : i)));
    setAllItems((prev) => prev.map((i) => (i.item_id === updated.item_id ? updated : i)));
    return updated;
  };

  const handleBulkApprove = async (domain?: string) => {
    await bulkApproveReviewItems(domain);
    await loadQueue();
    await loadAll();
  };

  const handleCurate = async (domain: string, action: "start" | "stop") => {
    // Starting curation withholds that domain from the Executive, so say so
    // before doing it — this is the one action here that removes knowledge.
    if (action === "start") {
      const n = trustedDefaults[domain] ?? 0;
      const ok = window.confirm(
        `Queue ${n} item${n === 1 ? "" : "s"} in "${domain}" for review?\n\n` +
          `They will be withheld from the Executive until you approve them. ` +
          `You can undo this with "Stop curating".`,
      );
      if (!ok) return;
    }
    await curateDomain(domain, action);
    await loadQueue();
    await loadAll();
  };

  const handleItemUpdated = (updated: ReviewItem) => {
    setItems((prev) => prev.map((i) => (i.item_id === updated.item_id ? updated : i)));
    setAllItems((prev) => prev.map((i) => (i.item_id === updated.item_id ? updated : i)));
  };

  // Unique domains from current list
  // Union of every source, so a domain is never hidden just because the
  // current filter excludes it from `allItems`.
  const domains = Array.from(
    new Set([
      ...allItems.map((i) => i.domain),
      ...pendingItems.map((i) => i.domain),
      ...Object.keys(trustedDefaults),
    ]),
  ).sort();

  const runBulk = (action: BulkAction) => {
    if (action.kind === "approve") void handleBulkApprove(action.domain);
    else void handleCurate(action.domain, action.kind === "curate-start" ? "start" : "stop");
  };

  const rowProps = (item: ReviewItem) => ({
    item,
    onApprove: () => handleApprove(item),
    onReject: () => setRejectModal({ itemId: item.item_id, notes: "" }),
    onFlag: () => void handleFlag(item),
    onView: () => setSelectedItemId(item.item_id),
    onPriorityChange: (p: ReviewPriority) => void handlePriorityChange(item, p),
  });

  // The bulk menu's domain: the picked one, else the list's domain filter,
  // else the first domain.
  const activeBulkDomain =
    (bulkDomain && domains.includes(bulkDomain) && bulkDomain) ||
    (filterDomain && domains.includes(filterDomain) && filterDomain) ||
    domains[0] ||
    "";
  const bulkActions = activeBulkDomain
    ? domainBulkActions(activeBulkDomain, pendingItems, trustedDefaults)
    : [];
  const curatable = Object.keys(trustedDefaults).sort((a, b) => a.localeCompare(b));
  const curateDomainPick = curatable.includes(bulkDomain) ? bulkDomain : curatable[0] ?? "";

  // Rendered inside the Knowledge base workspace, which supplies the padding.
  return (
    <div className="max-w-4xl">
      <div className="mb-5 max-w-2xl">
        <p className="text-[15px] text-fg-muted">
          Approve, reject, or correct knowledge before the Executive relies on it.
          Pending and rejected items are withheld from retrieval.
        </p>
      </div>
      <SectionTabs
        label="Review"
        className="mb-6"
        active={tab}
        onChange={setTab}
        tabs={[
          { id: "queue", label: "To review" },
          { id: "all", label: "All items" },
          { id: "annotations", label: "Annotations" },
        ]}
      />

      {/* Queue tab */}
      {tab === "queue" && (
        <div>
          <div className="flex items-center justify-between gap-3 mb-4">
            <p className="text-[15px] text-fg-muted">
              {loading ? "Loading…" : `${items.length} item${items.length !== 1 ? "s" : ""} need review`}
            </p>
            {items.length > 0 && (
              <Button onClick={() => handleBulkApprove()}>Approve all pending</Button>
            )}
          </div>
          {!loading && items.length === 0 && (
            <div className="rounded-2xl border border-line bg-surface-elevated px-5 py-10 sm:px-8">
              <div className="max-w-xl mx-auto text-center">
                <p className="text-lg font-semibold text-fg">Nothing waiting on you.</p>
                <p className="text-[15px] text-fg-muted mt-2 leading-relaxed">
                  The knowledge that ships with Open Executive is trusted by default — the
                  Executive can use it right away, and it does not sit here waiting for
                  sign-off. Items appear in this queue when you upload something new, edit an
                  existing file, or deliberately send a domain for review.
                </p>
              </div>
              {curatable.length > 0 && (
                <div className="mt-8 max-w-xl mx-auto border-t border-line pt-6">
                  <p className="text-base font-semibold text-fg">Review a domain yourself</p>
                  <p className="text-sm text-fg-muted mt-1 mb-3 leading-relaxed">
                    Sending a domain for review withholds it from the Executive until you
                    work through it.
                  </p>
                  <div className="flex flex-col sm:flex-row gap-2">
                    <select
                      value={curateDomainPick}
                      onChange={(e) => setBulkDomain(e.target.value)}
                      aria-label="Domain to review"
                      className={`${SELECT_CLASS} !h-11 flex-1 capitalize`}
                    >
                      {curatable.map((domain) => (
                        <option key={domain} value={domain}>
                          {domain} ({trustedDefaults[domain]})
                        </option>
                      ))}
                    </select>
                    <Button
                      onClick={() => curateDomainPick && handleCurate(curateDomainPick, "start")}
                      disabled={!curateDomainPick}
                    >
                      Curate {curateDomainPick}
                    </Button>
                  </div>
                </div>
              )}
            </div>
          )}
          <div className="space-y-2">
            {items.map((item) => (
              <ItemRow key={item.item_id} {...rowProps(item)} />
            ))}
          </div>
        </div>
      )}

      {/* All items tab */}
      {tab === "all" && (
        <div>
          {/* Filters */}
          <div className="flex gap-2 mb-4 flex-wrap">
            <select
              value={filterStatus}
              onChange={(e) => setFilterStatus(e.target.value as ReviewStatus | "")}
              aria-label="Status"
              className={SELECT_CLASS}
            >
              <option value="">All statuses</option>
              <option value="pending">Pending</option>
              <option value="approved">Approved</option>
              <option value="rejected">Rejected</option>
              <option value="needs_revision">Needs revision</option>
            </select>
            <select
              value={filterDomain}
              onChange={(e) => setFilterDomain(e.target.value)}
              aria-label="Domain"
              className={SELECT_CLASS}
            >
              <option value="">All domains</option>
              {domains.map((d) => <option key={d} value={d}>{d}</option>)}
            </select>
            <select
              value={filterType}
              onChange={(e) => setFilterType(e.target.value as "builtin" | "external" | "")}
              aria-label="Type"
              className={SELECT_CLASS}
            >
              <option value="">All types</option>
              <option value="builtin">Built-in</option>
              <option value="failure">Failure case studies</option>
              <option value="external">Reference library</option>
            </select>
          </div>

          {/* Bulk actions for one domain at a time: approve what's queued, or
              start / stop curating it. */}
          {domains.length > 0 && (
            <div className="mb-5 flex flex-col sm:flex-row sm:items-center gap-2 rounded-2xl border border-line bg-surface-overlay/50 px-4 py-3">
              <span className="text-sm font-semibold text-fg">Bulk actions</span>
              <select
                value={activeBulkDomain}
                onChange={(e) => setBulkDomain(e.target.value)}
                aria-label="Domain for bulk actions"
                className={`${SELECT_CLASS} capitalize`}
              >
                {domains.map((d) => <option key={d} value={d}>{d}</option>)}
              </select>
              <div className="flex items-center gap-2 flex-wrap sm:ml-auto">
                {bulkActions.length === 0 ? (
                  <span className="text-sm text-fg-subtle">Nothing to do in {activeBulkDomain}.</span>
                ) : (
                  bulkActions.map((a) => (
                    <Button key={a.kind} size="sm" className="!h-10" title={a.detail} onClick={() => runBulk(a)}>
                      {a.label}
                    </Button>
                  ))
                )}
              </div>
            </div>
          )}

          {loading && <p className="text-[15px] text-fg-subtle">Loading…</p>}
          <div className="space-y-2">
            {allItems.map((item) => (
              <ItemRow key={item.item_id} {...rowProps(item)} />
            ))}
            {!loading && allItems.length === 0 && (
              <p className="text-[15px] text-fg-subtle py-8 text-center">No items match the filters.</p>
            )}
          </div>
        </div>
      )}

      {/* Annotations tab */}
      {tab === "annotations" && (
        <div>
          <p className="text-[15px] text-fg-muted mb-4">
            {loading ? "Loading…" : `${annotations.length} active SME correction${annotations.length !== 1 ? "s" : ""}`}
          </p>
          <div className="space-y-2">
            {annotations.map((ann) => (
              <div key={ann.annotation_id} className="bg-surface-elevated border border-line rounded-2xl px-4 py-3.5 flex items-start gap-3">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-1 flex-wrap text-xs text-fg-muted">
                    <span className="capitalize font-medium">{ann.domain}</span>
                    <span className="text-fg-subtle break-all">{ann.item_id}</span>
                  </div>
                  <p className="text-[15px] text-fg">{ann.correction}</p>
                  <p className="mt-1 flex items-center gap-1.5 text-xs text-fg-muted">
                    <span aria-hidden className="h-2 w-2 rounded-full bg-emerald-500" />
                    Active
                  </p>
                </div>
                <OverflowMenu
                  label="Correction actions"
                  items={[
                    {
                      label: "Turn off",
                      onSelect: () => void patchAnnotation(ann.annotation_id, { is_active: false }).then(loadAnnotations),
                    },
                    {
                      label: "Delete",
                      danger: true,
                      onSelect: () => void deleteAnnotation(ann.annotation_id).then(loadAnnotations),
                    },
                  ]}
                />
              </div>
            ))}
            {!loading && annotations.length === 0 && (
              <p className="text-[15px] text-fg-subtle py-8 text-center">No active corrections. Open a review item to add one.</p>
            )}
          </div>
        </div>
      )}

      {/* Reject modal */}
      {rejectModal && (
        <div className="fixed inset-0 z-[60] flex items-center justify-center p-4">
          <div className="absolute inset-0 bg-black/60" onClick={() => setRejectModal(null)} />
          <div
            role="dialog"
            aria-modal="true"
            aria-label="Reject item"
            className="relative bg-surface-elevated border border-line rounded-2xl p-5 w-full max-w-md shadow-2xl"
          >
            <p className="text-lg font-semibold text-fg mb-3">Reject item</p>
            <textarea
              value={rejectModal.notes}
              onChange={(e) => setRejectModal({ ...rejectModal, notes: e.target.value })}
              placeholder="Optional notes about why this is rejected…"
              aria-label="Why it is rejected"
              autoFocus
              className="w-full h-28 bg-surface-elevated border border-line-strong rounded-xl px-3 py-2 text-[15px] text-fg resize-none focus:outline-none focus:ring-2 focus:ring-red-500/40 placeholder:text-fg-subtle mb-4"
            />
            <div className="flex gap-2 justify-end">
              <Button variant="ghost" onClick={() => setRejectModal(null)}>
                Cancel
              </Button>
              <Button variant="danger" onClick={handleRejectConfirm}>
                Reject
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* Item panel */}
      {selectedItemId && (
        <ItemSlideOver
          itemId={selectedItemId}
          onClose={() => setSelectedItemId(null)}
          onUpdated={handleItemUpdated}
          onApprove={handleApprove}
          onFlag={handleFlag}
          onReject={(item) => setRejectModal({ itemId: item.item_id, notes: "" })}
          onPriorityChange={handlePriorityChange}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Item row
// ---------------------------------------------------------------------------

function ItemRow({
  item,
  onApprove,
  onReject,
  onFlag,
  onView,
  onPriorityChange,
}: {
  item: ReviewItem;
  onApprove: () => void;
  onReject: () => void;
  onFlag: () => void;
  onView: () => void;
  onPriorityChange: (p: ReviewPriority) => void;
}) {
  const fileBacked = item.content_type === "builtin" || item.content_type === "failure";
  return (
    <div className="bg-surface-elevated border border-line rounded-2xl px-4 py-3.5 sm:px-5 flex items-center gap-3 sm:gap-4">
      <span className="hidden sm:flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-xl bg-surface-overlay text-fg-muted">
        <Icon name={fileBacked ? "doc" : "book"} size="w-5 h-5" />
      </span>

      <div className="flex-1 min-w-0">
        <button
          onClick={onView}
          className="block max-w-full truncate text-left text-[15px] font-semibold text-fg hover:text-accent transition-colors"
        >
          {item.filename}
        </button>
        <div className="mt-1 flex items-center gap-2 flex-wrap text-sm text-fg-muted">
          <span className="capitalize">{item.domain}</span>
          <StatusPill
            status={item.status}
            reviewedAt={item.reviewed_at}
            trustedDefault={item.trusted_default}
          />
          <PriorityPill priority={item.priority} />
          <span className="text-fg-subtle">Added {formatDate(item.registered_at)}</span>
        </div>
      </div>

      <div className="flex items-center gap-1 flex-shrink-0">
        {item.status !== "approved" ? (
          <Button variant="primary" size="sm" className="!h-10" onClick={onApprove}>
            Approve
          </Button>
        ) : (
          <Button size="sm" className="!h-10" onClick={onView}>
            View
          </Button>
        )}
        <OverflowMenu
          label={`More actions for ${item.filename}`}
          items={itemMenu(item, {
            onView: item.status !== "approved" ? onView : undefined,
            onFlag,
            onReject,
            onPriorityChange,
          })}
        />
      </div>
    </div>
  );
}
