"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState, useSyncExternalStore } from "react";

import { useExecutiveStatus } from "@/components/executive/ExecutiveStatusContext";
import Icon from "@/components/Icon";
import { MODE_LABEL } from "@/components/settings/WorkspaceCard";
import {
  ADVANCED_ITEMS,
  SETTINGS_PAGES,
  settingsHrefForHash,
  type SettingsPageDef,
  type SettingsPageId,
} from "@/components/shell/navConfig";
import { useWorkspace } from "@/components/workspace/WorkspaceContext";
import {
  getAgentDetail,
  getHistory,
  getVersion,
  listPersonas,
  type HistoryState,
} from "@/lib/api";
import { retentionLabel } from "@/lib/history";
import { versionNotice } from "@/lib/versionNotice";

// Settings — a hub of tiles, one per page (SETTINGS_PAGES): Your Executive,
// About you, Workspace, Advanced and About. Each tile says what's on its page
// and, where it's cheap to know, how things stand right now. The one-page
// Settings this replaces used anchors (`/settings#workspace`); a link that
// still carries one is sent on to the matching page (Act as me's, to its tab
// under Delegate).

function subscribeHash(onChange: () => void): () => void {
  window.addEventListener("hashchange", onChange);
  return () => window.removeEventListener("hashchange", onChange);
}

function readHash(): string {
  return window.location.hash;
}

type Tone = "ok" | "warn" | "none";

interface TileStatus {
  text: string;
  tone: Tone;
}

export default function SettingsPage() {
  const router = useRouter();
  // The address's hash: null while rendering on the server, where there is
  // none to read. The hub is held back until it is known, and while an old
  // anchor link is on its way to its page, so the tiles don't flash.
  const hash = useSyncExternalStore(subscribeHash, readHash, () => null);
  const target = hash === null ? null : settingsHrefForHash(hash);
  useEffect(() => {
    if (target) router.replace(target);
  }, [router, target]);

  const statuses = useTileStatuses();
  // About you has a tile only for someone with notes to keep (GET /memories/history
  // answers null for anyone not signed in or not on the People list).
  const pages = SETTINGS_PAGES.filter((p) => p.id !== "memory" || statuses.memoryOffered);

  if (hash === null || target) return <main className="flex-1" />;

  return (
    <main className="flex-1 min-h-0 overflow-y-auto">
      <div className="max-w-5xl mx-auto px-4 sm:px-6 pt-6 sm:pt-10 pb-16">
        <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-fg">Settings</h1>
        <p className="mt-2 text-[15px] text-fg-muted">
          How the Executive runs, who it runs for, and the tools for looking under the hood.
        </p>
        {/* Phones: one compact list (a card with a row per page) so every page
            fits on one screen. Wider: a grid of tiles. */}
        <ul className="mt-6 sm:mt-8 grid grid-cols-1 overflow-hidden rounded-2xl border border-line bg-surface-elevated divide-y divide-line sm:overflow-visible sm:rounded-none sm:border-0 sm:bg-transparent sm:divide-y-0 sm:gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {pages.map((page) => (
            <li key={page.id} className="min-w-0">
              <SettingsTile page={page} status={statuses.byPage[page.id]} />
            </li>
          ))}
        </ul>
      </div>
    </main>
  );
}

function SettingsTile({ page, status }: { page: SettingsPageDef; status?: TileStatus }) {
  return (
    <Link
      href={page.href}
      className="group flex h-full min-h-14 items-center gap-3 px-4 py-3 sm:items-start sm:flex-col sm:gap-0 sm:rounded-2xl sm:border sm:border-line sm:bg-surface-elevated sm:p-6 transition-colors hover:bg-surface-overlay/40 sm:hover:border-accent/50 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/60"
    >
      <span className="flex w-9 h-9 sm:w-11 sm:h-11 flex-shrink-0 items-center justify-center rounded-xl bg-accent/10 text-accent">
        <Icon name={page.icon} size="w-5 h-5" />
      </span>
      <span className="min-w-0 flex-1 sm:mt-4 sm:w-full">
        <span className="block font-display text-base sm:text-lg font-bold tracking-tight text-fg">{page.label}</span>
        <span className="hidden sm:block mt-1 text-[15px] text-fg-muted leading-relaxed">
          {page.description}
        </span>
        <span className="mt-0.5 sm:mt-3 flex min-h-[1.25rem] items-center gap-2 text-[13px] sm:text-sm text-fg-muted">
          {status && (
            <>
              {status.tone !== "none" && (
                <span
                  className={`w-2 h-2 rounded-full flex-shrink-0 ${
                    status.tone === "ok" ? "bg-emerald-500" : "bg-amber-500"
                  }`}
                  aria-hidden="true"
                />
              )}
              <span className="truncate">{status.text}</span>
            </>
          )}
        </span>
      </span>
      <Icon
        name="chevron-right"
        size="w-5 h-5"
        className="sm:hidden text-fg-subtle group-hover:text-fg transition-colors"
      />
    </Link>
  );
}

// The one-line status under each tile. Each comes from a read the app makes
// anyway (run state, workspace) or one small request; a read that fails
// leaves its tile without a status rather than guessing.
function useTileStatuses(): {
  byPage: Partial<Record<SettingsPageId, TileStatus>>;
  memoryOffered: boolean;
} {
  const { status: run, unknown } = useExecutiveStatus();
  const { mode, effectiveTimezone, loading: workspaceLoading } = useWorkspace();
  const [voice, setVoice] = useState<string | null>(null);
  const [version, setVersion] = useState<TileStatus | null>(null);
  const [history, setHistory] = useState<HistoryState | null | "error">(null);

  useEffect(() => {
    const ctrl = new AbortController();
    Promise.all([listPersonas(), getAgentDetail("executive")])
      .then(([all, exec]) => {
        const slug = exec.voice_persona_slug ?? "default";
        const name = all.find((p) => p.slug === slug)?.display_name;
        if (!ctrl.signal.aborted && name) setVoice(name);
      })
      .catch(() => {});
    getHistory(undefined, ctrl.signal)
      .then((h) => setHistory(h))
      .catch((err) => {
        if ((err as Error)?.name !== "AbortError") setHistory("error");
      });
    getVersion(ctrl.signal)
      .then((v) => {
        const notice = versionNotice(v);
        setVersion(
          notice.update
            ? { text: `${notice.running} · update available`, tone: "warn" }
            : { text: notice.running, tone: "none" },
        );
      })
      .catch(() => {});
    return () => ctrl.abort();
  }, []);

  const byPage: Partial<Record<SettingsPageId, TileStatus>> = {};

  if (run) {
    const state: TileStatus = unknown
      ? { text: "Status unknown", tone: "none" }
      : run.paused
        ? { text: "Paused", tone: "warn" }
        : { text: "Running", tone: "ok" };
    byPage.executive = { ...state, text: voice ? `${state.text} · ${voice} voice` : state.text };
  } else if (voice) {
    byPage.executive = { text: `${voice} voice`, tone: "none" };
  }

  if (history && history !== "error") {
    const days = history.company_retention_days;
    byPage.memory = {
      text: `Notes ${history.reply_notes ? "on" : "off"} · ${
        days === null ? "kept until forgotten" : `last ${retentionLabel(days)}`
      }`,
      tone: "none",
    };
  }

  if (!workspaceLoading) {
    byPage.workspace = {
      text: [MODE_LABEL[mode], effectiveTimezone].filter(Boolean).join(" · "),
      tone: "none",
    };
  }

  byPage.advanced = { text: `${ADVANCED_ITEMS.length} tools for power users`, tone: "none" };
  if (version) byPage.about = version;

  return { byPage, memoryOffered: history !== null };
}
