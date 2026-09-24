"use client";

import { useEffect, useMemo, useState } from "react";
import { WorkflowToolInfo, describeWorkflowTools } from "@/lib/api";

/**
 * "google_workspace__append_table_rows" → { label: "Append table rows", source: "google_workspace" }
 * "oe__read_file" → { label: "Read file", source: "Open Executive" }
 */
export function toolLabel(name: string): { label: string; source: string } {
  const cut = name.indexOf("__");
  const server = cut > 0 ? name.slice(0, cut) : "";
  const tool = cut > 0 ? name.slice(cut + 2) : name;
  const words = (tool || name).replace(/[_-]+/g, " ").trim();
  return {
    label: words.charAt(0).toUpperCase() + words.slice(1),
    source: server === "oe" ? "Open Executive" : server.replace(/[_-]+/g, " "),
  };
}

/** Descriptions and read/write labels for a set of tool names (one request). */
export function useToolInfo(names: string[]): Map<string, WorkflowToolInfo> {
  const key = useMemo(() => Array.from(new Set(names)).sort().join(","), [names]);
  const [info, setInfo] = useState<Map<string, WorkflowToolInfo>>(new Map());
  useEffect(() => {
    if (!key) {
      setInfo(new Map());
      return;
    }
    let cancelled = false;
    describeWorkflowTools(key.split(","))
      .then((tools) => {
        if (!cancelled) setInfo(new Map(tools.map((t) => [t.name, t])));
      })
      .catch(() => {
        if (!cancelled) setInfo(new Map());
      });
    return () => {
      cancelled = true;
    };
  }, [key]);
  return info;
}

/** A tool reads only when its info says so; unknown means it may change things. */
export function mayWrite(name: string, info: Map<string, WorkflowToolInfo>): boolean {
  return info.get(name)?.read_only !== true;
}

export default function ToolChips({
  names,
  info,
  onRemove,
}: {
  names: string[];
  info: Map<string, WorkflowToolInfo>;
  onRemove?: (name: string) => void;
}) {
  if (names.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-1.5">
      {names.map((name) => {
        const { label, source } = toolLabel(name);
        const writes = mayWrite(name, info);
        return (
          <span
            key={name}
            title={`${name}${info.get(name)?.description ? ` — ${info.get(name)?.description}` : ""}`}
            className="inline-flex items-center gap-1 rounded-full border border-line bg-surface-overlay px-2 py-0.5 text-[11px] text-fg"
          >
            {label}
            {source && <span className="text-fg-subtle">· {source}</span>}
            {writes && (
              <span className="rounded bg-amber-500/15 px-1 text-[10px] text-amber-300">
                may change things
              </span>
            )}
            {onRemove && (
              <button
                type="button"
                onClick={() => onRemove(name)}
                aria-label={`Remove ${name}`}
                className="ml-0.5 text-fg-subtle hover:text-red-400"
              >
                ×
              </button>
            )}
          </span>
        );
      })}
    </div>
  );
}
