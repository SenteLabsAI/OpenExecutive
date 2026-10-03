"use client";

import { useMemo, useState } from "react";
import type { BuiltinFileMeta } from "@/lib/api";
import Icon from "@/components/Icon";

export type FileKind = "builtin" | "failures";

export type Selection =
  | { kind: "file"; fileKind: FileKind; domain: string; filename: string }
  | { kind: "new"; fileKind: FileKind }
  | { kind: "playbooks" }
  | { kind: "company" }
  | { kind: "reference" }
  | { kind: "query" }
  | { kind: "review" }
  | null;

interface SourceTreeProps {
  domains: string[];
  builtinFiles: BuiltinFileMeta[];
  failureFiles: BuiltinFileMeta[];
  selection: Selection;
  filter: string;
  onFilterChange: (value: string) => void;
  onSelect: (sel: Selection) => void;
}

// The built-in playbooks file tree: one group per domain, each with its
// playbooks and failure case studies. Lives in the "Built-in playbooks" view
// under Advanced on the Knowledge page.
export default function SourceTree({
  domains,
  builtinFiles,
  failureFiles,
  selection,
  filter,
  onFilterChange,
  onSelect,
}: SourceTreeProps) {
  // Domains start folded so the tree reads as a short list; the open file's
  // domain starts open.
  const [openDomains, setOpenDomains] = useState<Set<string>>(
    () => new Set(selection?.kind === "file" ? [selection.domain] : [])
  );

  const builtinByDomain = useMemo(() => groupByDomain(builtinFiles), [builtinFiles]);
  const failuresByDomain = useMemo(() => groupByDomain(failureFiles), [failureFiles]);

  const normalizedFilter = filter.trim().toLowerCase();
  const matches = (s: string) =>
    !normalizedFilter || s.toLowerCase().includes(normalizedFilter);

  function toggleDomain(d: string) {
    setOpenDomains((prev) => {
      const next = new Set(prev);
      if (next.has(d)) next.delete(d);
      else next.add(d);
      return next;
    });
  }

  function isActiveFile(fileKind: FileKind, domain: string, filename: string) {
    return (
      selection?.kind === "file" &&
      selection.fileKind === fileKind &&
      selection.domain === domain &&
      selection.filename === filename
    );
  }

  return (
    <nav aria-label="Built-in playbooks" className="space-y-4">
      <div className="grid grid-cols-2 gap-2">
        <button
          data-closes-nav
          onClick={() => onSelect({ kind: "new", fileKind: "builtin" })}
          className="inline-flex h-10 items-center justify-center gap-1 whitespace-nowrap rounded-xl border border-line bg-surface-elevated px-2 text-sm font-medium text-fg hover:bg-surface-hover transition-colors"
        >
          <Icon name="plus" size="w-4 h-4" />
          Playbook
        </button>
        <button
          data-closes-nav
          onClick={() => onSelect({ kind: "new", fileKind: "failures" })}
          className="inline-flex h-10 items-center justify-center gap-1 whitespace-nowrap rounded-xl border border-line bg-surface-elevated px-2 text-sm font-medium text-fg hover:bg-surface-hover transition-colors"
        >
          <Icon name="plus" size="w-4 h-4" />
          Failure case
        </button>
      </div>
      <input
        value={filter}
        onChange={(e) => onFilterChange(e.target.value)}
        placeholder="Filter files…"
        aria-label="Filter files"
        className="w-full h-10 rounded-xl border border-line bg-surface-elevated px-3 text-sm text-fg placeholder-fg-subtle focus:outline-none focus:ring-2 focus:ring-accent/40"
      />
      {domains.map((domain) => {
        const playbooks = (builtinByDomain[domain] ?? []).filter((f) => matches(f.filename));
        const failures = (failuresByDomain[domain] ?? []).filter((f) => matches(f.filename));
        if (normalizedFilter && playbooks.length === 0 && failures.length === 0) {
          return null;
        }
        const isCollapsed = !openDomains.has(domain) && !normalizedFilter;
        return (
          <div key={domain}>
            <button
              onClick={() => toggleDomain(domain)}
              aria-expanded={!isCollapsed}
              className="w-full flex items-center gap-2 h-10 px-1 text-sm font-semibold text-fg capitalize hover:text-accent transition-colors"
            >
              <Icon
                name="chevron-right"
                size="w-4 h-4"
                className={`text-fg-subtle transition-transform ${isCollapsed ? "" : "rotate-90"}`}
              />
              {domain}
              <span className="ml-auto text-xs font-normal text-fg-subtle tabular-nums">
                {playbooks.length + failures.length}
              </span>
            </button>
            {!isCollapsed && (
              <div className="ml-3 mt-1 space-y-2 border-l border-line pl-2">
                <FileGroup
                  label="Playbooks"
                  files={playbooks}
                  tone="default"
                  onClickFile={(f) =>
                    onSelect({ kind: "file", fileKind: "builtin", domain, filename: f.filename })
                  }
                  isActive={(f) => isActiveFile("builtin", domain, f.filename)}
                />
                <FileGroup
                  label="Failures"
                  files={failures}
                  tone="rose"
                  onClickFile={(f) =>
                    onSelect({ kind: "file", fileKind: "failures", domain, filename: f.filename })
                  }
                  isActive={(f) => isActiveFile("failures", domain, f.filename)}
                />
              </div>
            )}
          </div>
        );
      })}
    </nav>
  );
}

function groupByDomain(files: BuiltinFileMeta[]): Record<string, BuiltinFileMeta[]> {
  return files.reduce<Record<string, BuiltinFileMeta[]>>((acc, f) => {
    (acc[f.domain] ??= []).push(f);
    return acc;
  }, {});
}

function FileGroup({
  label,
  files,
  tone,
  onClickFile,
  isActive,
}: {
  label: string;
  files: BuiltinFileMeta[];
  tone: "default" | "rose";
  onClickFile: (f: BuiltinFileMeta) => void;
  isActive: (f: BuiltinFileMeta) => boolean;
}) {
  const labelClass = tone === "rose" ? "text-rose-500" : "text-fg-subtle";
  return (
    <div>
      <div className={`px-2 text-xs font-medium ${labelClass}`}>{label}</div>
      {files.length === 0 ? (
        <p className="text-xs text-fg-subtle px-2 mt-0.5">None</p>
      ) : (
        <div className="mt-0.5">
          {files.map((f) => {
            const active = isActive(f);
            return (
              <button
                key={f.filename}
                data-closes-nav
                onClick={() => onClickFile(f)}
                aria-current={active ? "true" : undefined}
                className={`w-full text-left px-2 py-2.5 rounded-lg text-sm transition-colors truncate ${
                  active
                    ? "bg-accent/10 text-accent font-medium"
                    : "text-fg-muted hover:text-fg hover:bg-surface-overlay"
                }`}
              >
                {f.filename.replace(/\.md$/, "")}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
