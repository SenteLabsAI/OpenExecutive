"use client";

import { useEffect, useState } from "react";

import LeadRulesEditor from "@/components/settings/LeadRulesEditor";
import SettingsCard from "@/components/settings/SettingsCard";
import Switch from "@/components/Switch";
import {
  addCompanyLeadRule,
  allowLeadSuggestion,
  deleteCompanyLeadRule,
  getTakeTheLead,
  removeLeadLearned,
  setTakeTheLead,
  type TakeTheLead,
} from "@/lib/api";

type Mode = "off" | "training" | "on";

const MODES: { mode: Mode; label: string }[] = [
  { mode: "off", label: "Off" },
  { mode: "training", label: "In training" },
  { mode: "on", label: "On" },
];

function modeOf(lead: TakeTheLead): Mode {
  if (!lead.enabled) return "off";
  return lead.training ? "training" : "on";
}

// Take the lead as the Executive (GET/PUT /take-the-lead), the owner's
// alone: its unattended runs act on what they find, behind the gate. In
// training everything waits for a yes on Today unless it's on the "What it's
// learned" list (allowed from a card, or from a suggestion here). The six
// "Always asks first" kinds each have a switch, shown only while it's on (they
// gate nothing else). The company's rules always hold, for everyone's Take the
// lead as you too, so they stay. Anyone else (the route answers 403) sees only
// who can turn it on.
export default function TakeTheLeadCard() {
  const [lead, setLead] = useState<TakeTheLead | null>(null);
  const [state, setState] = useState<"loading" | "hidden" | "ready" | "error">("loading");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getTakeTheLead(controller.signal)
      .then((next) => {
        setLead(next);
        setState(next ? "ready" : "hidden");
      })
      .catch((err) => {
        if ((err as Error)?.name !== "AbortError") setState("error");
      });
    return () => controller.abort();
  }, []);

  if (state === "loading") return <p className="text-[15px] text-fg-muted">Loading…</p>;
  if (state === "hidden") {
    return (
      <SettingsCard title="Take the lead">
        <p className="text-sm text-fg-muted">
          Only the account owner can turn this on. When it&apos;s on, the Executive acts on what it finds without
          asking first.
        </p>
      </SettingsCard>
    );
  }
  if (state === "error" || !lead) {
    return (
      <SettingsCard>
        <p className="text-sm text-fg-muted">Couldn&apos;t load Take the lead.</p>
      </SettingsCard>
    );
  }

  const run = async (change: () => Promise<TakeTheLead>) => {
    setBusy(true);
    setError(null);
    try {
      setLead(await change());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the setting.");
    } finally {
      setBusy(false);
    }
  };
  const save = (update: { enabled?: boolean; training?: boolean; ask_first?: Record<string, boolean> }) =>
    run(() => setTakeTheLead(update));
  const mode = modeOf(lead);
  const pick = (next: Mode) => {
    if (next === mode) return;
    void save(next === "off" ? { enabled: false } : { enabled: true, training: next === "training" });
  };

  return (
    <SettingsCard
      title="Take the lead"
      titleId="take-the-lead-label"
      description={
        !lead.available
          ? "Needs signed sign-ins on this server before it can be turned on."
          : mode === "training"
            ? "In training, everything it would do comes to you as a card on Today first. What you allow, it does on its own from then on."
            : mode === "on"
              ? "It acts on what it finds while it looks over your day: it messages people, books meetings and starts workflows. Anything below waits for a yes first."
              : "Off: when it looks over your day it tells you what it would do, and you do it."
      }
    >
      <div className="flex flex-col gap-5">
        <div
          role="radiogroup"
          aria-labelledby="take-the-lead-label"
          className="grid grid-cols-3 overflow-hidden rounded-xl border border-line"
        >
          {MODES.map((m, i) => {
            const picked = m.mode === mode;
            return (
              <button
                key={m.mode}
                type="button"
                role="radio"
                aria-checked={picked}
                disabled={busy || (m.mode !== "off" && !lead.available)}
                onClick={() => pick(m.mode)}
                className={`min-h-touch px-2 py-2.5 text-[15px] font-semibold transition-colors cursor-pointer disabled:cursor-not-allowed disabled:opacity-60 ${
                  i > 0 ? "border-l border-line " : ""
                }${picked ? "bg-accent-strong text-white" : "text-fg hover:bg-surface-overlay/60"}`}
              >
                {m.label}
              </button>
            );
          })}
        </div>
        {lead.enabled && (lead.learned.length > 0 || lead.suggested.length > 0 || mode === "training") && (
          <div>
            <h3 className="text-[15px] font-semibold text-fg">What it&apos;s learned</h3>
            <p className="mt-1 text-sm text-fg-muted">
              In training it does these on its own. Use Approve + allow on a card to add one.
            </p>
            {lead.learned.length === 0 && lead.suggested.length === 0 ? (
              <p className="mt-3 text-sm text-fg-muted">Nothing yet.</p>
            ) : (
              <ul className="mt-3 flex flex-col divide-y divide-line rounded-xl border border-line">
                {lead.learned.map((item) => {
                  const example = Object.values(item.example).join(" · ");
                  return (
                    <li key={item.id} className="flex min-h-touch items-center justify-between gap-3 px-4 py-2">
                      <span className="min-w-0">
                        <span className="block text-[15px] font-medium">{item.label}</span>
                        <span className="mt-0.5 block text-[13px] leading-snug text-fg-muted line-clamp-2">
                          Allowed {new Date(item.created_at).toLocaleDateString(undefined, { month: "short", day: "numeric" })}
                          {item.uses > 0 ? ` · done ${item.uses} ${item.uses === 1 ? "time" : "times"} since` : ""}
                          {example ? ` · like “${example}”` : ""}
                        </span>
                      </span>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => void run(() => removeLeadLearned(item.id))}
                        className="flex-shrink-0 text-sm font-semibold text-accent hover:underline disabled:opacity-60"
                        aria-label={`Remove ${item.label}`}
                      >
                        Remove
                      </button>
                    </li>
                  );
                })}
                {lead.suggested.map((item) => (
                  <li key={item.key} className="flex min-h-touch items-center justify-between gap-3 px-4 py-2">
                    <span className="min-w-0">
                      <span className="block text-[15px] font-medium">{item.label}</span>
                      <span className="mt-0.5 block text-[13px] leading-snug text-fg-muted">
                        Suggested: you approved {item.approvals} unchanged
                      </span>
                    </span>
                    <button
                      type="button"
                      disabled={busy || !lead.available}
                      onClick={() => void run(() => allowLeadSuggestion(item.key))}
                      className="flex-shrink-0 text-sm font-semibold text-accent hover:underline disabled:opacity-60"
                      aria-label={`Allow ${item.label}`}
                    >
                      Allow
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
        {lead.enabled && (
          <>
            <p className="rounded-xl bg-surface-overlay/60 px-4 py-3 text-sm leading-relaxed text-fg-muted">
              Department approval levels still apply. A department on Proposes still sends its meetings to its head for
              a yes. Set a department to Acts on its own to let it book without asking.
            </p>
            <div>
              <h3 className="text-[15px] font-semibold text-fg">Always asks first</h3>
              <p className="mt-1 text-sm text-fg-muted">
                These wait for you, or for whoever approves that area (like spending or hiring), with a card on Today.
              </p>
              <ul className="mt-3 flex flex-col divide-y divide-line rounded-xl border border-line">
                {lead.ask_first.map((item) => {
                  const id = `ask-first-${item.kind}`;
                  return (
                    <li key={item.kind} className="flex min-h-touch items-center justify-between gap-3 px-4 py-2">
                      <span className="min-w-0">
                        <span id={id} className="block text-[15px]">
                          {item.label}
                        </span>
                        <span className="mt-0.5 block text-[13px] leading-snug text-fg-muted">{item.hint}</span>
                      </span>
                      <Switch
                        checked={item.on}
                        onChange={() => void save({ ask_first: { [item.kind]: !item.on } })}
                        disabled={busy}
                        labelledBy={id}
                      />
                    </li>
                  );
                })}
              </ul>
            </div>
          </>
        )}
        <div>
          <h3 className="text-[15px] font-semibold text-fg">Company rules</h3>
          <p className="mt-1 mb-3 text-sm text-fg-muted">
            Anything matching one of these always waits for a yes, for the Executive and for everyone&apos;s Take the
            lead as you.
          </p>
          <LeadRulesEditor
            rules={lead.rules}
            emptyText="No company rules yet."
            disabled={busy}
            onAdd={async (kind, value) => setLead(await addCompanyLeadRule(kind, value))}
            onDelete={async (id) => setLead(await deleteCompanyLeadRule(id))}
          />
        </div>
      </div>
      {error && <p className="mt-2 text-sm text-red-500">{error}</p>}
    </SettingsCard>
  );
}
