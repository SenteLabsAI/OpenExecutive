"use client";

import { useState } from "react";
import { WorkflowToolInfo, searchWorkflowTools } from "@/lib/api";
import ToolChips, { toolLabel, useToolInfo } from "./ToolChips";

/**
 * Pick the tools an action step may use: the chosen list (removable chips)
 * plus a search over everything the system can reach. Whatever ends up here
 * is exactly what the user approves when they save the workflow.
 */
export default function ToolPicker({
  value,
  onChange,
  inputCls,
}: {
  value: string[];
  onChange: (tools: string[]) => void;
  inputCls: string;
}) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<WorkflowToolInfo[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const info = useToolInfo(value);

  async function search() {
    const q = query.trim();
    if (!q) return;
    setSearching(true);
    setError(null);
    try {
      setResults(await searchWorkflowTools(q));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSearching(false);
    }
  }

  return (
    <div className="space-y-2">
      {value.length > 0 ? (
        <ToolChips
          names={value}
          info={info}
          onRemove={(name) => onChange(value.filter((t) => t !== name))}
        />
      ) : (
        <p className="text-xs text-fg-subtle">No tools yet — search below.</p>
      )}
      <div className="flex gap-2">
        <input
          className={inputCls}
          value={query}
          placeholder="What should it do? e.g. append rows to a sheet"
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              void search();
            }
          }}
        />
        <button
          type="button"
          onClick={() => void search()}
          disabled={searching || !query.trim()}
          className="shrink-0 rounded-md border border-line px-3 text-xs text-fg-muted hover:text-fg disabled:opacity-50"
        >
          {searching ? "Searching…" : "Search tools"}
        </button>
      </div>
      {error && <p className="text-xs text-red-400">{error}</p>}
      {results && results.length === 0 && (
        <p className="text-xs text-fg-subtle">No matching tools are connected.</p>
      )}
      {results && results.length > 0 && (
        <ul className="divide-y divide-line rounded-md border border-line">
          {results.map((t) => {
            const added = value.includes(t.name);
            const { label, source } = toolLabel(t.name);
            return (
              <li key={t.name} className="flex items-start gap-3 px-3 py-2">
                <div className="min-w-0 flex-1">
                  <p className="text-xs text-fg">
                    {label} <span className="text-fg-subtle">· {source}</span>
                    {t.read_only !== true && (
                      <span className="ml-1 rounded bg-amber-500/15 px-1 text-[10px] text-amber-300">
                        may change things
                      </span>
                    )}
                  </p>
                  <p className="text-[11px] text-fg-muted line-clamp-2">{t.description}</p>
                </div>
                <button
                  type="button"
                  disabled={added}
                  onClick={() => onChange([...value, t.name])}
                  className="shrink-0 text-xs text-indigo-400 hover:text-indigo-300 disabled:text-fg-subtle"
                >
                  {added ? "Added" : "+ Add"}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
