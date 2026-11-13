"use client";

import { useEffect, useState, type ReactNode } from "react";

import SettingsCard from "@/components/settings/SettingsCard";
import SettingsSubpage from "@/components/settings/SettingsSubpage";
import { buttonClass } from "@/components/ui/Button";
import { PageSpinner } from "@/components/ui/Spinner";
import { getBrowserStatus, type BrowserStatus } from "@/lib/api";

// Settings → Browser. Each person adds the Open Executive browser extension
// (packages/extension) to their own Chrome and connects it here, so the
// Executive can use websites for them (browser/tools.py). GET
// /browser/status, checked again while the page is open, says whether it is
// connected and whether "Stay connected" is ticked in it (what workflows
// need). Three states: not set up (how to set it up), set up, and connected
// with Stay connected off (the one step left). With no install page
// (BROWSER_EXTENSION_URL set empty) it says how to load it from the
// repository instead.

const RECHECK_MS = 10_000;
const GUIDE_URL = "https://github.com/ForeactLabs/OpenExecutive/blob/main/docs/browser_extension_setup.md";

type Tone = "ok" | "off" | "muted";

function Pill({ tone, children }: { tone: Tone; children: ReactNode }) {
  const look =
    tone === "ok"
      ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300"
      : tone === "off"
        ? "bg-amber-500/15 text-amber-700 dark:text-amber-300"
        : "bg-surface-overlay text-fg-muted";
  return <span className={`whitespace-nowrap rounded-full px-2.5 py-1 text-xs font-semibold ${look}`}>{children}</span>;
}

function Row({ title, line, pill }: { title: string; line: string; pill?: ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4 border-t border-line py-3.5">
      <div className="min-w-0 space-y-0.5">
        <p className="text-[15px] font-semibold text-fg">{title}</p>
        <p className="text-sm text-fg-muted">{line}</p>
      </div>
      {pill}
    </div>
  );
}

function AddToChrome({ storeUrl }: { storeUrl: string | null }) {
  if (!storeUrl) {
    return (
      <a href={GUIDE_URL} target="_blank" rel="noopener noreferrer" className="self-start text-sm font-medium text-accent hover:underline">
        How to load it from this repository
      </a>
    );
  }
  return (
    <a href={storeUrl} target="_blank" rel="noopener noreferrer" className={`${buttonClass("primary")} self-start`}>
      Add to Chrome
    </a>
  );
}

const CHAT_LINE = "Works on the tab you're on.";
const ELSEWHERE_LINE =
  "Uses its own window on your computer, even with no Open Executive tab open. Workflows wait if Chrome is closed.";

function NotSetUp({ storeUrl }: { storeUrl: string | null }) {
  return (
    <>
      <SettingsCard
        title="Set it up"
        description="Add the Open Executive extension to Chrome on your computer. Each person adds it to their own browser."
        action={<Pill tone="muted">Not connected</Pill>}
      >
        <div className="flex flex-col gap-4">
          <AddToChrome storeUrl={storeUrl} />
          <ol className="list-decimal space-y-1.5 pl-5 text-[15px] text-fg">
            <li>
              {storeUrl
                ? "Press Add to Chrome, then Add extension."
                : "Load the extension in Chrome (chrome://extensions, Developer mode, Load unpacked)."}
            </li>
            <li>With Open Executive open in a tab, click the extension&apos;s icon and press Connect.</li>
            <li>Allow it on all sites when Chrome asks.</li>
            <li>Leave Stay connected ticked, so your workflows can use it.</li>
          </ol>
        </div>
      </SettingsCard>
      <SettingsCard title="Once it's connected" description="After the steps above, your Executive can use websites here.">
        <div>
          <Row title="Chat on this computer" line={CHAT_LINE} />
          <Row title="In workflows" line={`${ELSEWHERE_LINE} Needs Stay connected.`} />
        </div>
      </SettingsCard>
    </>
  );
}

function Connected({ stay, storeUrl }: { stay: boolean | null; storeUrl: string | null }) {
  const stayOff = stay === false;
  return (
    <>
      <SettingsCard title={stayOff ? "Almost ready" : "Ready to use"} action={<Pill tone="ok">Connected</Pill>}>
        <div>
          <Row title="Chat on this computer" line={CHAT_LINE} pill={<Pill tone="ok">Ready</Pill>} />
          <Row
            title="In workflows"
            line={stay === null ? `${ELSEWHERE_LINE} Needs Stay connected.` : ELSEWHERE_LINE}
            pill={stay === null ? undefined : <Pill tone={stayOff ? "off" : "ok"}>{stayOff ? "Off" : "Ready"}</Pill>}
          />
          {stayOff && (
            <div className="mt-1 space-y-1 rounded-xl border border-amber-500/30 bg-amber-500/5 p-4">
              <p className="text-[15px] font-semibold text-fg">One step left</p>
              <p className="text-sm text-fg-muted">Click the extension&apos;s icon in Chrome and tick Stay connected.</p>
            </div>
          )}
        </div>
      </SettingsCard>
      <SettingsCard title="On another computer?" description="Add the extension there and press Connect.">
        <AddToChrome storeUrl={storeUrl} />
      </SettingsCard>
    </>
  );
}

export default function BrowserSettingsPage() {
  const [status, setStatus] = useState<BrowserStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () =>
      getBrowserStatus()
        .then((next) => {
          if (!alive) return;
          setStatus(next);
          setError(null);
        })
        .catch((err: unknown) => {
          if (alive) setError(err instanceof Error ? err.message : "Failed to load your browser's status");
        });
    void load();
    const timer = window.setInterval(() => void load(), RECHECK_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);

  return (
    <SettingsSubpage title="Browser" description="Let your Executive use websites for you, in Chrome on your computer.">
      {error && !status ? (
        <p className="rounded-xl border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-300">{error}</p>
      ) : !status ? (
        <PageSpinner />
      ) : status.available === false ? (
        <SettingsCard title="Open Executive extension">
          <p className="text-[15px] text-fg-muted">The browser extension isn&apos;t turned on here.</p>
        </SettingsCard>
      ) : (
        <div className="space-y-4">
          {status.connected ? (
            <Connected stay={status.stay_connected ?? null} storeUrl={status.store_url} />
          ) : (
            <NotSetUp storeUrl={status.store_url} />
          )}
          <p className="text-sm text-fg-muted">
            It follows your Approvals, never fills in passwords or card numbers, and Stop on the bar ends it.
          </p>
        </div>
      )}
    </SettingsSubpage>
  );
}
