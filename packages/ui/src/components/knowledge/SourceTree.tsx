"use client";

import { useMemo, useState } from "react";
import type { BuiltinFileMeta } from "@/lib/api";
import Icon, { type IconName } from "@/components/Icon";

export type FileKind = "builtin" | "failures";

export type Selection =
  | { kind: "file"; fileKind: FileKind; domain: string; filename: string }
  | { kind: "new"; fileKind: FileKind }
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
  /** Pending + needs-revision items, shown on the Review queue entry. */
  reviewCount: number;
  onSelect: (sel: Selection) => void;
}

export default function SourceTree({
  domains,
  builtinFiles,
  failureFiles,
  selection,
  filter,
  reviewCount,
  onSelect,
}: SourceTreeProps) {
  const [collapsedBuiltin, setCollapsedBuiltin] = useState(true);
  const [collapsedDomains, setCollapsedDomains] = useState<Set<string>>(new Set());

  const builtinByDomain = useMemo(() => groupByDomain(builtinFiles), [builtinFiles]);
  const failuresByDomain = useMemo(() => groupByDomain(failureFiles), [failureFiles]);

  const normalizedFilter = filter.trim().toLowerCase();
  const matches = (s: string) =>
    !normalizedFilter || s.toLowerCase().includes(normalizedFilter);

  function toggleDomain(d: string) {
    setCollapsedDomains((prev) => {
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
    <nav className="text-sm space-y-3">
      <RootButton
        active={selection?.kind === "review"}
        onClick={() => onSelect({ kind: "review" })}
        icon="check-circle"
        badge={reviewCount}
      >
        Review queue
      </RootButton>
      <Section
        label="Built-in"
        icon="grid"
        collapsed={collapsedBuiltin}
        onToggle={() => setCollapsedBuiltin((v) => !v)}
      >
        {domains.map((domain) => {
          const playbooks = (builtinByDomain[domain] ?? []).filter((f) =>
            matches(f.filename)
          );
          const failures = (failuresByDomain[domain] ?? []).filter((f) =>
            matches(f.filename)
          );
          if (normalizedFilter && playbooks.length === 0 && failures.length === 0) {
            return null;
          }
          const isCollapsed = collapsedDomains.has(domain) && !normalizedFilter;
          return (
            <div key={domain}>
              <button
                onClick={() => toggleDomain(domain)}
                className="w-full flex items-center gap-1.5 px-1 text-[11px] font-semibold text-fg-muted uppercase tracking-widest hover:text-fg transition-colors"
              >
                <span className="text-fg-subtle w-3 inline-block">
                  {isCollapsed ? "▸" : "▾"}
                </span>
                {domain}
              </button>
              {!isCollapsed && (
                <div className="ml-3 mt-1 space-y-2">
                  <FileGroup
                    label="Playbooks"
                    files={playbooks}
                    accent="indigo"
                    onClickFile={(f) =>
                      onSelect({
                        kind: "file",
                        fileKind: "builtin",
                        domain,
                        filename: f.filename,
                      })
                    }
                    onAdd={() => onSelect({ kind: "new", fileKind: "builtin" })}
                    isActive={(f) => isActiveFile("builtin", domain, f.filename)}
                  />
                  <FileGroup
                    label="Failures"
                    files={failures}
                    accent="rose"
                    onClickFile={(f) =>
                      onSelect({
                        kind: "file",
                        fileKind: "failures",
                        domain,
                        filename: f.filename,
                      })
                    }
                    onAdd={() => onSelect({ kind: "new", fileKind: "failures" })}
                    isActive={(f) => isActiveFile("failures", domain, f.filename)}
                  />
                </div>
              )}
            </div>
          );
        })}
      </Section>

      <RootButton
        active={selection?.kind === "company"}
        onClick={() => onSelect({ kind: "company" })}
        icon="building"
      >
        Company
      </RootButton>
      <RootButton
        active={selection?.kind === "reference"}
        onClick={() => onSelect({ kind: "reference" })}
        icon="book"
      >
        Reference Library
      </RootButton>
      <RootButton
        active={selection?.kind === "query"}
        onClick={() => onSelect({ kind: "query" })}
        accent="indigo"
        icon="doc-search"
      >
        Query mode
      </RootButton>
    </nav>
  );
}

function groupByDomain(files: BuiltinFileMeta[]): Record<string, BuiltinFileMeta[]> {
  return files.reduce<Record<string, BuiltinFileMeta[]>>((acc, f) => {
    (acc[f.domain] ??= []).push(f);
    return acc;
  }, {});
}

function Section({
  label,
  icon,
  collapsed,
  onToggle,
  children,
}: {
  label: string;
  icon?: IconName;
  collapsed: boolean;
  onToggle: () => void;
  children: React.ReactNode;
}) {
  return (
    <div>
      <button
        onClick={onToggle}
        className="flex items-center gap-1.5 text-xs font-bold text-fg uppercase tracking-widest hover:text-white transition-colors mb-2"
      >
        <span className="text-fg-muted w-3 inline-block">{collapsed ? "▸" : "▾"}</span>
        {icon && <Icon name={icon} size="w-3.5 h-3.5" className="text-fg-muted" />}
        {label}
      </button>
      {!collapsed && <div className="space-y-3">{children}</div>}
    </div>
  );
}

function FileGroup({
  label,
  files,
  accent,
  onClickFile,
  onAdd,
  isActive,
}: {
  label: string;
  files: BuiltinFileMeta[];
  accent: "indigo" | "rose";
  onClickFile: (f: BuiltinFileMeta) => void;
  onAdd: () => void;
  isActive: (f: BuiltinFileMeta) => boolean;
}) {
  const labelClass = accent === "rose" ? "text-rose-400/80" : "text-fg-muted";
  return (
    <div>
      <div className="flex items-center justify-between px-1">
        <span className={`text-[10px] uppercase tracking-widest font-semibold ${labelClass}`}>
          {label}
        </span>
        <button
          onClick={onAdd}
          className="text-[10px] text-fg-subtle hover:text-fg transition-colors px-1"
          title={`Add ${label.toLowerCase()} file`}
        >
          +
        </button>
      </div>
      {files.length === 0 ? (
        <p className="text-[11px] text-fg-subtle px-1 mt-0.5">none</p>
      ) : (
        <div className="mt-0.5">
          {files.map((f) => {
            const active = isActive(f);
            return (
              <button
                key={f.filename}
                onClick={() => onClickFile(f)}
                className={`w-full text-left px-2 py-0.5 rounded text-xs transition-colors truncate ${
                  active
                    ? accent === "rose"
                      ? "bg-rose-500/15 text-rose-200"
                      : "bg-surface-input text-fg"
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

function RootButton({
  active,
  onClick,
  accent,
  icon,
  badge = 0,
  children,
}: {
  active: boolean;
  onClick: () => void;
  accent?: "indigo";
  icon?: IconName;
  badge?: number;
  children: React.ReactNode;
}) {
  const activeClass = accent === "indigo" ? "bg-indigo-500/15 text-indigo-200" : "bg-surface-input text-fg";
  return (
    <button
      onClick={onClick}
      className={`w-full flex items-center gap-2 text-left px-2 py-1.5 rounded-lg text-xs font-bold uppercase tracking-widest transition-colors ${
        active ? activeClass : "text-fg hover:text-white hover:bg-surface-overlay"
      }`}
    >
      {icon && <Icon name={icon} size="w-3.5 h-3.5" />}
      {children}
      {badge > 0 && (
        <span className="ml-auto text-[10px] font-semibold normal-case tracking-normal px-1.5 rounded-full bg-amber-950/60 text-amber-400 border border-amber-900/60">
          {badge}
        </span>
      )}
    </button>
  );
}
