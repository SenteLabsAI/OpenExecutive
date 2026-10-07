"use client";

import { useCallback, useEffect, useState } from "react";

import Switch from "@/components/Switch";
import SettingsCard from "@/components/settings/SettingsCard";
import SettingsSubpage from "@/components/settings/SettingsSubpage";
import Button from "@/components/ui/Button";
import {
  getSavedTool,
  listSavedTools,
  rollbackSavedTool,
  setSavedToolEnabled,
  type SavedTool,
  type SavedToolDetail,
} from "@/lib/api";

// Where a tool was saved or run: "chat", or "workflow:<name>/<step>".
function originLabel(origin: string): string {
  if (origin === "chat") return "a chat";
  if (origin.startsWith("workflow:")) return `workflow ${origin.slice("workflow:".length)}`;
  return origin;
}

function when(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function ToolDetail({
  detail,
  busy,
  onRollback,
}: {
  detail: SavedToolDetail;
  busy: boolean;
  onRollback: (version: number) => void;
}) {
  return (
    <div className="mt-4 space-y-5 border-t border-line pt-4">
      <div>
        <h3 className="text-sm font-semibold text-fg">How it works (version {detail.version})</h3>
        <pre className="mt-2 max-h-80 overflow-auto rounded-xl bg-surface p-3 text-xs leading-relaxed text-fg whitespace-pre-wrap break-words">
          {detail.script}
        </pre>
      </div>
      <div>
        <h3 className="text-sm font-semibold text-fg">Versions</h3>
        <ul className="mt-2 space-y-2">
          {detail.versions.map((v) => (
            <li key={v.version} className="flex items-start justify-between gap-3 text-sm">
              <div className="min-w-0">
                <span className="font-medium text-fg">Version {v.version}</span>
                <span className="text-fg-muted">
                  {" "}
                  · {when(v.created_at)} · from {originLabel(v.origin)}
                </span>
                <p className="text-fg-muted">{v.description}</p>
              </div>
              {v.version === detail.version ? (
                <span className="flex-shrink-0 text-xs text-fg-subtle pt-1">In use</span>
              ) : (
                <Button size="sm" disabled={busy} onClick={() => onRollback(v.version)}>
                  Use this one
                </Button>
              )}
            </li>
          ))}
        </ul>
      </div>
      <div>
        <h3 className="text-sm font-semibold text-fg">Recent runs</h3>
        {detail.runs.length === 0 ? (
          <p className="mt-2 text-sm text-fg-muted">Not run since it was saved.</p>
        ) : (
          <ul className="mt-2 space-y-1 text-sm">
            {detail.runs.map((r, i) => (
              <li key={`${r.at}-${i}`} className="text-fg-muted">
                <span className={r.ok ? "text-emerald-600" : "text-rose-500"}>
                  {r.ok ? "Worked" : "Failed"}
                </span>{" "}
                · {when(r.at)} · version {r.version} · {r.calls} tool call{r.calls === 1 ? "" : "s"} ·{" "}
                {(r.duration_ms / 1000).toFixed(1)}s · {originLabel(r.origin)}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function ToolCard({
  tool,
  onChanged,
}: {
  tool: SavedTool;
  onChanged: (tool: SavedTool) => void;
}) {
  const [detail, setDetail] = useState<SavedToolDetail | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const titleId = `saved-tool-${tool.name}`;

  const apply = (next: SavedToolDetail) => {
    setDetail(next);
    onChanged(next);
  };

  const run = async (fn: () => Promise<SavedToolDetail>) => {
    setBusy(true);
    setError(null);
    try {
      apply(await fn());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong.");
    } finally {
      setBusy(false);
    }
  };

  const toggleOpen = async () => {
    const next = !open;
    setOpen(next);
    // Fetched on every open: the Executive may have saved a new version since.
    if (next) await run(() => getSavedTool(tool.name));
  };

  return (
    <SettingsCard
      title={<span className="font-mono">{tool.name}</span>}
      titleId={titleId}
      description={tool.description}
      action={
        <Switch
          checked={tool.enabled}
          disabled={busy}
          labelledBy={titleId}
          onChange={(on) => run(() => setSavedToolEnabled(tool.name, on))}
        />
      }
    >
      <p className="text-sm text-fg-muted">
        Version {tool.version} · saved from {originLabel(tool.origin)} · updated {when(tool.updated_at)}
      </p>
      <p className="mt-1 text-sm text-fg-muted">
        Uses:{" "}
        {tool.uses_tools.length ? (
          <span className="font-mono text-xs">{tool.uses_tools.join(", ")}</span>
        ) : (
          "no other tools"
        )}
      </p>
      <div className="mt-3">
        <Button size="sm" variant="ghost" onClick={toggleOpen} aria-expanded={open}>
          {open ? "Hide details" : "Details, versions and runs"}
        </Button>
      </div>
      {error && <p className="mt-2 text-sm text-rose-500">{error}</p>}
      {open && detail && (
        <ToolDetail
          detail={detail}
          busy={busy}
          onRollback={(version) => run(() => rollbackSavedTool(tool.name, version))}
        />
      )}
    </SettingsCard>
  );
}

// Settings → Advanced → Custom tools: the tools the Executive built and kept
// (saved tools, run_script save_as), with a switch for each, their versions
// and their recent runs.
export default function SavedToolsSettingsPage() {
  const [state, setState] = useState<
    | { kind: "loading" }
    | { kind: "forbidden" }
    | { kind: "error"; message: string }
    | { kind: "ready"; enabled: boolean; tools: SavedTool[] }
  >({ kind: "loading" });

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const body = await listSavedTools(signal);
      setState(body ? { kind: "ready", ...body } : { kind: "forbidden" });
    } catch (e) {
      if (signal?.aborted) return;
      setState({ kind: "error", message: e instanceof Error ? e.message : "Couldn't load." });
    }
  }, []);

  useEffect(() => {
    const ctrl = new AbortController();
    void load(ctrl.signal);
    return () => ctrl.abort();
  }, [load]);

  const replace = (next: SavedTool) =>
    setState((s) =>
      s.kind === "ready" ? { ...s, tools: s.tools.map((t) => (t.name === next.name ? next : t)) } : s,
    );

  return (
    <SettingsSubpage
      title="Custom tools"
      description="When the Executive needs a tool it doesn't have, it builds one, and keeps the ones it will need again. A custom tool can only do what the conversation or workflow using it is already allowed to do. Turn one off to stop it running, or switch it back to an earlier version."
    >
      {state.kind === "loading" && <p className="text-sm text-fg-muted">Loading…</p>}
      {state.kind === "forbidden" && (
        <SettingsCard>
          <p className="text-sm text-fg-muted">Only the account owner can see custom tools.</p>
        </SettingsCard>
      )}
      {state.kind === "error" && (
        <SettingsCard>
          <p className="text-sm text-rose-500">{state.message}</p>
        </SettingsCard>
      )}
      {state.kind === "ready" && (
        <>
          {!state.enabled && (
            <SettingsCard>
              <p className="text-sm text-fg-muted">
                Custom tools are turned off on this server (SAVED_TOOLS_ENABLED=false): nothing new
                is kept and none of these run.
              </p>
            </SettingsCard>
          )}
          {state.tools.length === 0 ? (
            <SettingsCard>
              <p className="text-sm text-fg-muted">
                No custom tools yet. They appear here when the Executive builds a tool for a job it
                is likely to do again.
              </p>
            </SettingsCard>
          ) : (
            state.tools.map((tool) => <ToolCard key={tool.name} tool={tool} onChanged={replace} />)
          )}
        </>
      )}
    </SettingsSubpage>
  );
}
