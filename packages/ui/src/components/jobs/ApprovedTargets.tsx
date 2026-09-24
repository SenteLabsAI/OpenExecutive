"use client";

import { useEffect, useState } from "react";
import { ApprovedTarget, forgetApprovedTarget, listApprovedTargets } from "@/lib/api";

/**
 * Where a custom workflow's tool steps may write without asking again. Each
 * entry was approved the first time the workflow wrote there; removing one
 * means the next write there asks again.
 */
export default function ApprovedTargets({ name }: { name: string }) {
  const [targets, setTargets] = useState<ApprovedTarget[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    listApprovedTargets(name)
      .then((t) => {
        if (!cancelled) setTargets(t);
      })
      .catch(() => {
        if (!cancelled) setTargets([]);
      });
    return () => {
      cancelled = true;
    };
  }, [name]);

  async function remove(value: string) {
    setError(null);
    try {
      await forgetApprovedTarget(name, value);
      setTargets((t) => (t ?? []).filter((x) => x.value !== value));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  if (!targets || targets.length === 0) return null;

  return (
    <details className="rounded-md border border-line bg-surface/40 px-3 py-2 text-sm">
      <summary className="cursor-pointer text-fg">
        Approved targets <span className="text-fg-muted">({targets.length})</span>
      </summary>
      <p className="mt-2 text-xs text-fg-muted">
        This workflow can write to these without asking. Remove one and the next
        write there asks again.
      </p>
      <ul className="mt-2 divide-y divide-line/60">
        {targets.map((t) => (
          <li key={t.value} className="flex items-center gap-3 py-1.5 min-w-0">
            <span className="shrink-0 text-[11px] text-fg-subtle">{t.key}</span>
            <code className="min-w-0 flex-1 truncate text-xs text-fg" title={t.value}>
              {t.value}
            </code>
            <button
              type="button"
              onClick={() => void remove(t.value)}
              className="shrink-0 text-[11px] text-fg-muted hover:text-red-400 transition"
            >
              Remove
            </button>
          </li>
        ))}
      </ul>
      {error && <p className="mt-1 text-xs text-red-300">{error}</p>}
    </details>
  );
}
