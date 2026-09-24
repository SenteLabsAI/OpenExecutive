"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  SKILL_CATEGORIES,
  approveSkillDraft,
  createSkill,
  deleteSkill,
  discardSkillDraft,
  getSkill,
  getSkillDraft,
  listSkillDrafts,
  listSkills,
  restoreSkill,
  searchSkills,
  updateSkill,
  type SkillDeleteOutcome,
  type SkillDraft,
  type SkillDetail,
  type SkillInput,
  type SkillMeta,
  type SkillSearchHit,
} from "@/lib/api";

// Playbooks are the UI name for the backend's "skills": how the Executive
// does a piece of work. Workflows (the other tabs) are the runnable jobs.

const NAME_RE = /^[a-zA-Z0-9_-]+$/;

const DELETE_NOTICE: Record<SkillDeleteOutcome, (name: string) => string> = {
  deleted: (n) => `Deleted “${n}”.`,
  reverted: (n) => `“${n}” is back to the built-in version.`,
  hidden: (n) => `Hid “${n}”. The Executive won't use it until you restore it.`,
};

interface EditorState {
  /** Remounts the form, so "+ New playbook" always starts blank. */
  id: number;
  mode: "create" | "edit";
  /** Editing a built-in: saving creates this company's customized copy. */
  customizing: boolean;
  initial: SkillInput;
  /** Editing a chat draft: a successful save also clears that draft. */
  fromDraft?: string;
}

const DRAFT_ACTION_LABEL: Record<SkillDraft["action"], string> = {
  create: "new",
  update: "change",
  delete: "delete",
};

function draftInput(d: SkillDraft): SkillInput {
  return {
    name: d.name,
    category: d.category,
    description: d.description,
    when_to_use: d.when_to_use,
    body: d.body,
  };
}

const EMPTY_INPUT: SkillInput = {
  name: "",
  category: "general",
  description: "",
  when_to_use: "",
  body: "",
};

function toInput(s: SkillDetail): SkillInput {
  return {
    name: s.name,
    category: s.category,
    description: s.description,
    when_to_use: s.when_to_use,
    body: s.body,
  };
}

function groupByCategory(items: SkillMeta[]): Record<string, SkillMeta[]> {
  return items.reduce<Record<string, SkillMeta[]>>((acc, s) => {
    (acc[s.category] ??= []).push(s);
    return acc;
  }, {});
}

/** "The MBR workflow" / "The Board prep and MBR workflows", or "" when none follow it. */
function workflowList(skill: SkillMeta): string {
  const titles = skill.used_by.map((w) => w.title);
  if (titles.length === 0) return "";
  if (titles.length === 1) return `The ${titles[0]} workflow`;
  return `The ${titles.slice(0, -1).join(", ")} and ${titles[titles.length - 1]} workflows`;
}

function tryInChatHref(name: string): string {
  const draft = `Use the "${name}" playbook to `;
  return `/?new=1&draft=${encodeURIComponent(draft)}`;
}

export default function PlaybooksBrowser({
  onCountChange,
  initialPlaybook,
  initialDraft,
}: {
  onCountChange?: (count: number) => void;
  /** Playbook to open on mount (from a `?playbook=` link). */
  initialPlaybook?: string;
  /** Chat draft to open on mount (from a `?draft=` link). */
  initialDraft?: string;
}) {
  const [skills, setSkills] = useState<SkillMeta[]>([]);
  const [showHidden, setShowHidden] = useState(false);
  const [selected, setSelected] = useState<SkillDetail | null>(null);
  const [drafts, setDrafts] = useState<SkillDraft[]>([]);
  const [selectedDraft, setSelectedDraft] = useState<SkillDraft | null>(null);
  const [editor, setEditor] = useState<EditorState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchHits, setSearchHits] = useState<SkillSearchHit[] | null>(null);
  const [isSearching, setIsSearching] = useState(false);
  // The query behind the shown results (null = no results shown). A ref, so
  // a mutation that finishes later refreshes the search the user has *now*,
  // not the one captured when it started.
  const submittedQueryRef = useRef<string | null>(null);
  const [editorSeq, setEditorSeq] = useState(0);

  function openEditor(state: Omit<EditorState, "id">) {
    const id = editorSeq + 1;
    setEditorSeq(id);
    setEditor({ ...state, id });
  }

  const load = useCallback(async () => {
    try {
      const [data, pending] = await Promise.all([
        listSkills(showHidden),
        // The review strip is optional; never fail the tab over it.
        listSkillDrafts().catch(() => [] as SkillDraft[]),
      ]);
      setSkills(data);
      setDrafts(pending);
      onCountChange?.(data.filter((s) => !s.hidden).length);
    } catch {
      setError("Failed to load playbooks");
    }
  }, [showHidden, onCountChange]);

  useEffect(() => {
    load();
  }, [load]);

  const openedInitialRef = useRef(false);
  useEffect(() => {
    if (!initialPlaybook || openedInitialRef.current) return;
    openedInitialRef.current = true;
    void select(initialPlaybook);
    // select() only reads setters; runs once, for the link's playbook.
  }, [initialPlaybook]);

  const openedDraftRef = useRef(false);
  useEffect(() => {
    if (!initialDraft || openedDraftRef.current) return;
    openedDraftRef.current = true;
    void selectDraft(initialDraft);
  }, [initialDraft]);

  async function selectDraft(name: string) {
    setError(null);
    setNotice(null);
    setEditor(null);
    setSelected(null);
    try {
      setSelectedDraft(await getSkillDraft(name));
    } catch (e) {
      setSelectedDraft(null);
      setError(
        e instanceof Error ? `${e.message} — it may already have been reviewed.` : "Failed to load draft"
      );
    }
  }

  function handleApproveDraft(draft: SkillDraft) {
    if (
      draft.action === "delete" &&
      !confirm(`Delete the playbook “${draft.name}” as the Executive proposed? This cannot be undone.`)
    )
      return;
    void run(async () => {
      const result = await approveSkillDraft(draft.name);
      setSelectedDraft(null);
      setSelected(result.skill);
      setNotice(
        draft.action === "delete"
          ? `Deleted “${draft.name}”.`
          : draft.action === "create"
            ? `Added “${draft.name}” to your playbooks.`
            : `Applied the change to “${draft.name}”.`
      );
    });
  }

  function handleDiscardDraft(draft: SkillDraft) {
    void run(async () => {
      await discardSkillDraft(draft.name);
      setSelectedDraft(null);
      setNotice(`Discarded the Executive's proposal for “${draft.name}”.`);
    });
  }

  async function select(name: string) {
    setError(null);
    setNotice(null);
    setEditor(null);
    setSelectedDraft(null);
    try {
      setSelected(await getSkill(name));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load playbook");
    }
  }

  /** Run a mutation, then reload the list (and any search) even if it failed. */
  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await action();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong");
    } finally {
      await load();
      const q = submittedQueryRef.current;
      if (q) {
        const hits = await searchSkills(q, 10).catch(() => []);
        if (submittedQueryRef.current === q) setSearchHits(hits);
      }
      setBusy(false);
    }
  }

  function handleDelete(skill: SkillDetail) {
    const workflows = workflowList(skill);
    // A custom workflow re-checks its playbook on every save.
    const resave = skill.used_by.some((w) => w.is_custom)
      ? " A custom workflow that follows it will need a different playbook (or none) before you can save it again."
      : "";
    const prompt = skill.customized
      ? `Revert “${skill.name}” to the built-in version? Your changes will be lost.` +
        (workflows ? ` ${workflows} will follow the built-in again.` : "")
      : skill.source === "builtin"
        ? `Hide “${skill.name}”? The Executive will stop using it. You can restore it from “Show hidden”.` +
          (workflows ? ` ${workflows} will run without it until you do.` : "") +
          resave
        : `Delete the playbook “${skill.name}”? This cannot be undone.` +
          (workflows ? ` ${workflows} will run without it.` : "") +
          resave;
    if (!confirm(prompt)) return;
    void run(async () => {
      const outcome = await deleteSkill(skill.name);
      setNotice(DELETE_NOTICE[outcome](skill.name));
      if (outcome === "reverted") setSelected(await getSkill(skill.name));
      else if (outcome === "hidden" && showHidden) setSelected(await getSkill(skill.name));
      else setSelected(null);
    });
  }

  function handleRestore(skill: SkillDetail) {
    void run(async () => {
      setSelected(await restoreSkill(skill.name));
      setNotice(`Restored “${skill.name}”.`);
    });
  }

  function handleSave(input: SkillInput) {
    if (!editor) return;
    const { mode, customizing, fromDraft } = editor;
    void run(async () => {
      const saved = mode === "create" ? await createSkill(input) : await updateSkill(input);
      if (fromDraft) await discardSkillDraft(fromDraft);
      setEditor(null);
      setSelectedDraft(null);
      setSelected(saved);
      setNotice(
        customizing
          ? `Saved your version of “${saved.name}”. Delete it any time to go back to the built-in.`
          : `Saved “${saved.name}”.`
      );
    });
  }

  async function handleSearch(e: React.FormEvent) {
    e.preventDefault();
    const q = searchQuery.trim();
    submittedQueryRef.current = q || null;
    if (!q) {
      setSearchHits(null);
      return;
    }
    setIsSearching(true);
    setError(null);
    try {
      const hits = await searchSkills(q, 10);
      if (submittedQueryRef.current === q) setSearchHits(hits);
    } catch {
      setError("Search failed");
    } finally {
      setIsSearching(false);
    }
  }

  const yours = skills.filter((s) => s.source === "company" && !s.customized);
  const builtin = skills.filter(
    (s) => (s.source === "builtin" && !s.hidden) || s.customized
  );
  const hidden = skills.filter((s) => s.hidden);

  return (
    <div>
      <p className="mb-4 text-sm text-fg-muted max-w-3xl">
        Playbooks are <span className="text-fg">how</span> the Executive does a
        piece of work: a method, format, or checklist. In chat it looks one up
        when your request matches its “When to use”, and some workflows follow
        them too. Create one here, or ask the Executive to save one from chat.
      </p>

      <div className="mb-4 flex flex-wrap items-center gap-2">
        <form onSubmit={handleSearch} className="flex flex-1 min-w-[16rem] gap-2">
          <input
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search playbooks (e.g. 'cash forecast for next quarter')"
            className="flex-1 rounded-lg border border-line-strong bg-surface-elevated px-3 py-2 text-sm text-fg placeholder-fg-subtle focus:outline-none focus:ring-2 focus:ring-indigo-500/50"
          />
          <button
            type="submit"
            disabled={isSearching}
            className="px-4 py-2 rounded-lg border border-line-strong text-fg-muted hover:text-fg disabled:opacity-40 text-sm transition-colors"
          >
            {isSearching ? "Searching…" : "Search"}
          </button>
          {searchHits !== null && (
            <button
              type="button"
              onClick={() => {
                setSearchQuery("");
                setSearchHits(null);
                submittedQueryRef.current = null;
              }}
              className="px-3 py-2 border border-line-strong text-fg-muted hover:text-fg text-sm rounded-lg transition-colors"
            >
              Clear
            </button>
          )}
        </form>
        <button
          type="button"
          onClick={() => {
            setSelected(null);
            setNotice(null);
            openEditor({ mode: "create", customizing: false, initial: EMPTY_INPUT });
          }}
          className="shrink-0 rounded-md bg-indigo-600 px-3 py-2 text-sm font-medium text-white hover:bg-indigo-500 transition"
        >
          + New playbook
        </button>
      </div>

      {error && (
        <p className="mb-4 text-xs text-red-400 bg-red-500/10 border border-red-500/20 rounded-lg px-3 py-2">
          {error}
        </p>
      )}
      {notice && (
        <p className="mb-4 text-xs text-emerald-400 bg-emerald-500/10 border border-emerald-500/20 rounded-lg px-3 py-2">
          {notice}
        </p>
      )}

      <div className="flex flex-col md:flex-row gap-6">
        <div className="md:w-60 flex-shrink-0 space-y-5">
          {searchHits !== null ? (
            <div>
              <SectionLabel>Results</SectionLabel>
              {searchHits.length === 0 ? (
                <p className="text-xs text-fg-subtle px-1">No matches</p>
              ) : (
                searchHits.map((hit) => (
                  <ListButton
                    key={`${hit.source}::${hit.name}`}
                    active={selected?.name === hit.name}
                    onClick={() => select(hit.name)}
                  >
                    <span className="truncate">{hit.name}</span>
                    <span className="text-[10px] text-fg-subtle flex-shrink-0">
                      {hit.score.toFixed(2)}
                    </span>
                  </ListButton>
                ))
              )}
            </div>
          ) : (
            <>
              {drafts.length > 0 && (
                <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-2">
                  <SectionLabel>To review ({drafts.length})</SectionLabel>
                  <p className="text-[11px] text-fg-subtle px-1 mb-1">
                    Proposed by the Executive. Nothing changes until you approve.
                  </p>
                  {drafts.map((d) => (
                    <ListButton
                      key={d.name}
                      active={selectedDraft?.name === d.name}
                      onClick={() => selectDraft(d.name)}
                    >
                      <span className="truncate">{d.name}</span>
                      <span className="text-[10px] text-amber-400 flex-shrink-0">
                        {DRAFT_ACTION_LABEL[d.action]}
                      </span>
                    </ListButton>
                  ))}
                </div>
              )}
              <PlaybookSection
                title="Yours"
                items={yours}
                selectedName={selected?.name}
                onSelect={select}
                emptyMessage="Playbooks you create, or approve from chat, appear here."
              />
              <PlaybookSection
                title="Built-in"
                items={builtin}
                selectedName={selected?.name}
                onSelect={select}
              />
              {showHidden && (
                <PlaybookSection
                  title="Hidden"
                  items={hidden}
                  selectedName={selected?.name}
                  onSelect={select}
                  emptyMessage="No hidden playbooks."
                />
              )}
              <label className="flex items-center gap-2 px-1 text-xs text-fg-muted cursor-pointer">
                <input
                  type="checkbox"
                  checked={showHidden}
                  onChange={(e) => setShowHidden(e.target.checked)}
                />
                Show hidden
              </label>
            </>
          )}
        </div>

        <div className="flex-1 min-w-0">
          {editor ? (
            <PlaybookEditor
              key={editor.id}
              editor={editor}
              busy={busy}
              onCancel={() => setEditor(null)}
              onSave={handleSave}
            />
          ) : selectedDraft ? (
            <DraftView
              draft={selectedDraft}
              busy={busy}
              onApprove={() => handleApproveDraft(selectedDraft)}
              onDiscard={() => handleDiscardDraft(selectedDraft)}
              onEdit={() =>
                openEditor({
                  mode: selectedDraft.action === "create" ? "create" : "edit",
                  customizing: false,
                  initial: draftInput(selectedDraft),
                  fromDraft: selectedDraft.name,
                })
              }
            />
          ) : selected ? (
            <PlaybookView
              skill={selected}
              busy={busy}
              onEdit={() =>
                openEditor({
                  mode: "edit",
                  customizing: selected.source === "builtin",
                  initial: toInput(selected),
                })
              }
              onDelete={() => handleDelete(selected)}
              onRestore={() => handleRestore(selected)}
            />
          ) : (
            <div className="flex items-center justify-center h-64 text-fg-subtle text-sm">
              Select a playbook to see its steps.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-xs font-semibold text-fg-muted uppercase tracking-widest mb-1.5 px-1">
      {children}
    </p>
  );
}

function ListButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`w-full text-left px-2.5 py-1.5 rounded-lg text-sm transition-colors flex items-center justify-between gap-2 ${
        active
          ? "bg-surface-input text-fg"
          : "text-fg-muted hover:text-fg hover:bg-surface-overlay"
      }`}
    >
      {children}
    </button>
  );
}

function PlaybookSection({
  title,
  items,
  selectedName,
  onSelect,
  emptyMessage,
}: {
  title: string;
  items: SkillMeta[];
  selectedName: string | undefined;
  onSelect: (name: string) => void;
  emptyMessage?: string;
}) {
  const grouped = groupByCategory(items);
  const categories = Object.keys(grouped).sort();
  return (
    <div>
      <SectionLabel>{title}</SectionLabel>
      {categories.length === 0 ? (
        <p className="text-xs text-fg-subtle px-1">{emptyMessage ?? "None"}</p>
      ) : (
        categories.map((cat) => (
          <div key={cat} className="mb-3">
            <p className="text-[11px] font-medium text-fg-subtle uppercase tracking-wider mb-0.5 px-1">
              {cat}
            </p>
            {grouped[cat].map((s) => (
              <ListButton
                key={s.name}
                active={selectedName === s.name}
                onClick={() => onSelect(s.name)}
              >
                <span className="truncate">{s.name}</span>
                {s.customized && (
                  <span className="text-[10px] text-indigo-400 flex-shrink-0">edited</span>
                )}
              </ListButton>
            ))}
          </div>
        ))
      )}
    </div>
  );
}

function sourceLabel(skill: SkillMeta): string {
  if (skill.hidden) return "hidden";
  if (skill.customized) return "built-in · customized";
  return skill.source === "builtin" ? "built-in" : "yours";
}

function PlaybookView({
  skill,
  busy,
  onEdit,
  onDelete,
  onRestore,
}: {
  skill: SkillDetail;
  busy: boolean;
  onEdit: () => void;
  onDelete: () => void;
  onRestore: () => void;
}) {
  const btn =
    "px-3 py-1.5 rounded-lg border text-xs transition-colors disabled:opacity-40";
  const deleteLabel = skill.customized
    ? "Revert to built-in"
    : skill.source === "builtin"
      ? "Hide"
      : "Delete";
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-xs font-semibold text-indigo-400 uppercase tracking-widest">
              {skill.category}
            </span>
            <span className="text-[10px] uppercase tracking-wider text-fg-subtle px-2 py-0.5 border border-line rounded">
              {sourceLabel(skill)}
            </span>
          </div>
          <h2 className="text-base font-semibold text-fg mt-1 break-words">{skill.name}</h2>
          <p className="text-sm text-fg-muted mt-1">{skill.description}</p>
          <p className="text-xs text-fg-muted italic mt-1">When to use: {skill.when_to_use}</p>
          {skill.used_by.length > 0 && (
            <p className="text-xs text-fg-muted mt-1">
              Followed by{" "}
              {skill.used_by.map((w, i) => (
                <span key={w.name}>
                  {i > 0 && ", "}
                  <Link href={`/jobs/${encodeURIComponent(w.name)}`} className="text-indigo-400 hover:underline">
                    {w.title}
                  </Link>
                </span>
              ))}
              {" "}— {skill.used_by.length === 1 ? "that workflow uses" : "those workflows use"} this
              playbook{skill.source === "builtin" ? " (or your customized copy)" : ""}.
            </p>
          )}
        </div>
        <div className="flex flex-wrap gap-2 flex-shrink-0">
          {skill.hidden ? (
            <button
              type="button"
              disabled={busy}
              onClick={onRestore}
              className={`${btn} border-line-strong text-fg hover:bg-surface-overlay`}
            >
              Restore
            </button>
          ) : (
            <>
              <Link
                href={tryInChatHref(skill.name)}
                className={`${btn} border-line-strong text-fg-muted hover:text-fg`}
              >
                Try in chat
              </Link>
              <button
                type="button"
                disabled={busy}
                onClick={onEdit}
                className={`${btn} border-line-strong text-fg hover:bg-surface-overlay`}
              >
                {skill.source === "builtin" ? "Customize" : "Edit"}
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={onDelete}
                className={`${btn} border-red-500/20 text-red-400 hover:bg-red-500/10`}
              >
                {deleteLabel}
              </button>
            </>
          )}
        </div>
      </div>

      <div className="rounded-xl border border-line-strong bg-surface-elevated px-6 py-5 max-h-[520px] overflow-y-auto prose prose-invert prose-sm max-w-none
        prose-p:text-fg prose-p:leading-relaxed
        prose-headings:text-fg prose-headings:font-semibold
        prose-strong:text-fg prose-strong:font-semibold
        prose-code:text-indigo-300 prose-code:bg-surface-overlay prose-code:px-1.5 prose-code:py-0.5 prose-code:rounded prose-code:text-xs prose-code:before:content-none prose-code:after:content-none
        prose-pre:bg-surface-overlay prose-pre:border prose-pre:border-line-strong
        prose-blockquote:border-line-strong prose-blockquote:text-fg-muted
        prose-ul:text-fg prose-ol:text-fg
        prose-li:marker:text-fg-muted
        prose-hr:border-line-strong
        prose-a:text-indigo-400 prose-a:no-underline hover:prose-a:underline
        prose-table:text-fg prose-th:text-fg prose-th:border-line-strong prose-td:border-line-strong">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{skill.body}</ReactMarkdown>
      </div>
    </div>
  );
}

const MARKDOWN_BOX =
  "rounded-xl border border-line-strong bg-surface-elevated px-6 py-5 max-h-[420px] overflow-y-auto prose prose-invert prose-sm max-w-none prose-p:text-fg prose-headings:text-fg prose-strong:text-fg prose-ul:text-fg prose-ol:text-fg prose-li:marker:text-fg-muted prose-code:text-indigo-300 prose-a:text-indigo-400";

function DraftView({
  draft,
  busy,
  onApprove,
  onDiscard,
  onEdit,
}: {
  draft: SkillDraft;
  busy: boolean;
  onApprove: () => void;
  onDiscard: () => void;
  onEdit: () => void;
}) {
  const btn = "px-3 py-1.5 rounded-lg border text-xs transition-colors disabled:opacity-40";
  const heading =
    draft.action === "create"
      ? "New playbook"
      : draft.action === "update"
        ? "Change to an existing playbook"
        : "Delete a playbook";
  const followers = draft.current?.used_by ?? [];
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <span className="text-[10px] uppercase tracking-wider text-amber-400 px-2 py-0.5 border border-amber-500/30 rounded">
            Proposed by the Executive · {heading}
          </span>
          <h2 className="text-base font-semibold text-fg mt-2 break-words">{draft.name}</h2>
          {draft.action !== "delete" && (
            <>
              <p className="text-sm text-fg-muted mt-1">{draft.description}</p>
              <p className="text-xs text-fg-muted italic mt-1">
                When to use: {draft.when_to_use} · {draft.category}
              </p>
            </>
          )}
          <p className="text-[11px] text-fg-subtle mt-1">
            Proposed {new Date(draft.proposed_at).toLocaleString()}. Nothing changes until you
            approve it.
          </p>
          {followers.length > 0 && (
            <p className="text-xs text-amber-300 mt-1">
              Followed by {followers.map((w) => w.title).join(", ")} — approving changes what
              {followers.length === 1 ? " that workflow" : " those workflows"} follow.
            </p>
          )}
        </div>
        <div className="flex flex-wrap gap-2 flex-shrink-0">
          <button
            type="button"
            disabled={busy}
            onClick={onApprove}
            className={`${btn} border-emerald-500/30 text-emerald-300 hover:bg-emerald-500/10`}
          >
            {draft.action === "delete" ? "Approve delete" : "Approve"}
          </button>
          {draft.action !== "delete" && (
            <button
              type="button"
              disabled={busy}
              onClick={onEdit}
              className={`${btn} border-line-strong text-fg hover:bg-surface-overlay`}
            >
              Edit, then save
            </button>
          )}
          <button
            type="button"
            disabled={busy}
            onClick={onDiscard}
            className={`${btn} border-red-500/20 text-red-400 hover:bg-red-500/10`}
          >
            Discard
          </button>
        </div>
      </div>

      {draft.action !== "delete" && (
        <div className={MARKDOWN_BOX}>
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{draft.body}</ReactMarkdown>
        </div>
      )}
      {draft.current && (
        <details open={draft.action === "delete"} className="text-sm">
          <summary className="cursor-pointer text-xs text-fg-muted">
            {draft.action === "delete" ? "The playbook it would delete" : "Current version"}
          </summary>
          <div className={`${MARKDOWN_BOX} mt-2`}>
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{draft.current.body}</ReactMarkdown>
          </div>
        </details>
      )}
    </div>
  );
}

function PlaybookEditor({
  editor,
  busy,
  onCancel,
  onSave,
}: {
  editor: EditorState;
  busy: boolean;
  onCancel: () => void;
  onSave: (input: SkillInput) => void;
}) {
  const [form, setForm] = useState<SkillInput>(editor.initial);
  const set = (k: keyof SkillInput) => (v: string) => setForm((f) => ({ ...f, [k]: v }));

  const nameOk = NAME_RE.test(form.name);
  const complete =
    nameOk &&
    form.description.trim() !== "" &&
    form.when_to_use.trim() !== "" &&
    form.body.trim() !== "";

  const input =
    "w-full rounded-lg border border-line-strong bg-surface-elevated px-3 py-2 text-sm text-fg placeholder-fg-subtle focus:outline-none focus:ring-2 focus:ring-indigo-500/50";

  return (
    <form
      className="flex flex-col gap-3"
      onSubmit={(e) => {
        e.preventDefault();
        if (complete) onSave(form);
      }}
    >
      <h2 className="text-base font-semibold text-fg">
        {editor.mode === "create"
          ? "New playbook"
          : editor.customizing
            ? `Customize “${form.name}”`
            : `Edit “${form.name}”`}
      </h2>
      {editor.customizing && (
        <p className="text-xs text-fg-muted">
          Your edits are saved as this company&apos;s version and replace the
          built-in everywhere, including workflows that follow it. The original
          is kept; revert to it any time.
        </p>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <Field label="Name" hint="Letters, numbers, dashes. Can't be changed later.">
          <input
            value={form.name}
            disabled={editor.mode === "edit"}
            onChange={(e) => set("name")(e.target.value.trim())}
            placeholder="monthly-revenue-review"
            className={`${input} disabled:opacity-60`}
          />
          {form.name !== "" && !nameOk && (
            <span className="text-[11px] text-red-400">Use letters, numbers, - or _ only.</span>
          )}
        </Field>
        <Field label="Category">
          <select
            value={form.category}
            onChange={(e) => set("category")(e.target.value)}
            className={input}
          >
            {SKILL_CATEGORIES.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </Field>
      </div>
      <Field label="Description" hint="One line: what this playbook produces.">
        <input
          value={form.description}
          onChange={(e) => set("description")(e.target.value)}
          className={input}
        />
      </Field>
      <Field
        label="When to use"
        hint="The Executive matches requests against this to decide when to follow it."
      >
        <input
          value={form.when_to_use}
          onChange={(e) => set("when_to_use")(e.target.value)}
          className={input}
        />
      </Field>
      <Field label="Steps" hint="Markdown: the procedure, template, or checklist.">
        <textarea
          value={form.body}
          onChange={(e) => set("body")(e.target.value)}
          rows={18}
          className={`${input} font-mono text-xs leading-relaxed`}
        />
      </Field>

      <div className="flex gap-2">
        <button
          type="submit"
          disabled={!complete || busy}
          className="rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-40 transition"
        >
          {busy ? "Saving…" : "Save"}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="rounded-md border border-line-strong px-3 py-1.5 text-sm text-fg-muted hover:text-fg transition"
        >
          Cancel
        </button>
      </div>
    </form>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-xs font-medium text-fg">{label}</span>
      {children}
      {hint && <span className="text-[11px] text-fg-subtle">{hint}</span>}
    </label>
  );
}
