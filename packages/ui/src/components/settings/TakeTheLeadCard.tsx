"use client";

import { useEffect, useState } from "react";

import LeadRulesEditor from "@/components/settings/LeadRulesEditor";
import SettingsCard from "@/components/settings/SettingsCard";
import Switch from "@/components/Switch";
import {
  addCompanyLeadRule,
  deleteCompanyLeadRule,
  getTakeTheLead,
  setTakeTheLead,
  type TakeTheLead,
} from "@/lib/api";

// Take the lead as the Executive (GET/PUT /take-the-lead), the owner's
// alone: its unattended runs act on what they find, behind the gate. The six
// "Always asks first" kinds each have a switch; the company's rules always
// hold. Anyone else (the route answers 403) sees only who can turn it on.
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

  const save = async (update: { enabled?: boolean; ask_first?: Record<string, boolean> }) => {
    setBusy(true);
    setError(null);
    try {
      setLead(await setTakeTheLead(update));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the setting.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <SettingsCard
      title="Take the lead"
      titleId="take-the-lead-label"
      description={
        !lead.available
          ? "Needs signed sign-ins on this server before it can be turned on."
          : lead.enabled
            ? "It acts on what it finds while it looks over your day: it messages people, books meetings and starts workflows. Anything below waits for a yes first."
            : "Off: when it looks over your day it tells you what it would do, and you do it."
      }
      action={
        <Switch
          checked={lead.enabled}
          onChange={() => void save({ enabled: !lead.enabled })}
          disabled={busy || (!lead.enabled && !lead.available)}
          labelledBy="take-the-lead-label"
        />
      }
    >
      <div className="flex flex-col gap-5">
        <p className="rounded-xl bg-surface-overlay/60 px-4 py-3 text-sm leading-relaxed text-fg-muted">
          Department approval levels still apply. A department on Proposes still sends its meetings to its head for a
          yes. Set a department to Acts on its own to let it book without asking.
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
