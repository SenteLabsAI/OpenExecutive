"use client";

import { useCallback, useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import Button from "@/components/ui/Button";
import { type SkillDetail, deleteSkill, getSkill, updateSkill } from "@/lib/api";

const PROSE =
  "prose prose-invert prose-sm max-w-none prose-p:text-fg prose-p:leading-relaxed " +
  "prose-headings:text-fg prose-headings:font-semibold prose-strong:text-fg " +
  "prose-ul:text-fg prose-ol:text-fg prose-li:marker:text-fg-muted " +
  "prose-code:text-accent prose-code:before:content-none prose-code:after:content-none " +
  "prose-a:text-accent prose-table:text-fg prose-th:border-line-strong prose-td:border-line-strong";

/**
 * "How it's done" on a workflow's page: the method (playbook) its steps
 * follow, which the Executive also follows when it does this work in chat.
 * Editing a built-in saves the company's own copy; Reset to original drops
 * that copy. A playbook that can't be read (hidden, missing) is left out:
 * the workflow then runs on its own steps.
 */
export default function HowItsDone({
  playbooks,
  workflowTitle,
}: {
  playbooks: string[];
  workflowTitle: string;
}) {
  const [skills, setSkills] = useState<SkillDetail[]>([]);
  // A stable key, so a re-render with an equal list doesn't refetch.
  const names = playbooks.join("\n");

  useEffect(() => {
    let cancelled = false;
    const list = names ? names.split("\n") : [];
    Promise.allSettled(list.map((p) => getSkill(p))).then((results) => {
      if (cancelled) return;
      setSkills(
        results.flatMap((r) => (r.status === "fulfilled" && !r.value.hidden ? [r.value] : []))
      );
    });
    return () => {
      cancelled = true;
    };
  }, [names]);

  const replace = useCallback((next: SkillDetail) => {
    setSkills((cur) => cur.map((s) => (s.name === next.name ? next : s)));
  }, []);

  if (skills.length === 0) return null;

  return (
    <section className="rounded-2xl border border-line bg-surface-elevated p-5 shadow-sm sm:p-7">
      <h2 className="text-lg font-semibold text-fg">How it&rsquo;s done</h2>
      <p className="mt-1 text-sm text-fg-muted">
        Your Executive follows this for {workflowTitle}, here and in chat.
      </p>
      <div className="mt-4 space-y-6">
        {skills.map((s) => (
          <Method key={s.name} skill={s} showName={skills.length > 1} onSaved={replace} />
        ))}
      </div>
    </section>
  );
}

function Method({
  skill,
  showName,
  onSaved,
}: {
  skill: SkillDetail;
  showName: boolean;
  onSaved: (s: SkillDetail) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(skill.body);
  const [expanded, setExpanded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The method's own `# Title` repeats the workflow's name; the box says it.
  const shown = skill.body.replace(/^\s*# [^\n]*\n+/, "");
  const long = shown.length > 900;

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const saved = await updateSkill({
        name: skill.name,
        category: skill.category,
        description: skill.description,
        when_to_use: skill.when_to_use,
        body: draft,
      });
      onSaved(saved);
      setEditing(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const reset = async () => {
    if (!confirm("Go back to the original method? Your changes to it will be lost.")) return;
    setBusy(true);
    setError(null);
    try {
      await deleteSkill(skill.name);
      const original = await getSkill(skill.name);
      onSaved(original);
      setDraft(original.body);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      {(showName || skill.customized) && (
        <div className="mb-2 flex items-center justify-between gap-3">
          {showName ? (
            <p className="text-[15px] font-semibold text-fg">{skill.description}</p>
          ) : (
            <span />
          )}
          {skill.customized && (
            <span className="shrink-0 rounded-full bg-amber-500/10 px-2.5 py-0.5 text-xs font-semibold text-amber-500">
              Customized
            </span>
          )}
        </div>
      )}

      {editing ? (
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          rows={14}
          aria-label="How it's done"
          className="w-full rounded-xl border border-line bg-surface px-3 py-2.5 font-mono text-sm text-fg focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
        />
      ) : (
        <div
          className={`border-l-[3px] border-accent/70 pl-4 ${PROSE} ${
            long && !expanded ? "max-h-72 overflow-hidden" : ""
          }`}
        >
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{shown}</ReactMarkdown>
        </div>
      )}
      {!editing && long && (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="mt-2 min-h-10 text-sm font-medium text-accent hover:underline"
        >
          {expanded ? "Show less" : "Show all"}
        </button>
      )}

      {error && <p className="mt-2 text-sm text-red-400">{error}</p>}

      <div className="mt-3 flex flex-wrap gap-2.5">
        {editing ? (
          <>
            <Button variant="primary" onClick={save} disabled={busy || !draft.trim()}>
              Save
            </Button>
            <Button
              onClick={() => {
                setDraft(skill.body);
                setEditing(false);
                setError(null);
              }}
              disabled={busy}
            >
              Cancel
            </Button>
          </>
        ) : (
          <>
            <Button
              onClick={() => {
                setDraft(skill.body);
                setEditing(true);
              }}
              disabled={busy}
            >
              Edit
            </Button>
            {skill.customized && (
              <Button onClick={reset} disabled={busy}>
                Reset to original
              </Button>
            )}
          </>
        )}
      </div>
    </div>
  );
}
