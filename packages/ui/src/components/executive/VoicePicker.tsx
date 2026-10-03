"use client";

import { useEffect, useState } from "react";

import Icon from "@/components/Icon";
import { getAgentDetail, listPersonas, patchAgent, type PersonaMeta } from "@/lib/api";

// The Executive's voice, picked from the three general voices. Shared by the
// setup flow (/onboard) and Settings → Your Executive, the one place it is
// chosen after setup; both save through PATCH
// /agents/executive, the same field the Agent Council edits.
//
// Named built-in voices are legacy: hidden here, but an install that already
// uses one (or a custom voice made in the Council) sees it listed as its
// current voice, so picking never hides what is active.

// `default` is the Direct voice; it is stored as no override.
const DIRECT = "default";
const GENERAL_ORDER = [DIRECT, "supportive", "analytical"];

export function generalVoices(all: PersonaMeta[]): PersonaMeta[] {
  return GENERAL_ORDER.map((slug) => all.find((p) => p.slug === slug)).filter(
    (p): p is PersonaMeta => p !== undefined && !p.is_legacy,
  );
}

interface Props {
  // "card" saves on each pick (Settings). "step" saves on Continue (setup).
  variant: "card" | "step";
  onDone?: () => void;
}

export default function VoicePicker({ variant, onDone }: Props) {
  const [voices, setVoices] = useState<PersonaMeta[]>([]);
  const [current, setCurrent] = useState<string>(DIRECT);
  const [picked, setPicked] = useState<string>(DIRECT);
  const [loaded, setLoaded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([listPersonas(), getAgentDetail("executive")])
      .then(([all, exec]) => {
        if (cancelled) return;
        const slug = exec.voice_persona_slug ?? DIRECT;
        const shown = generalVoices(all);
        const active = all.find((p) => p.slug === slug);
        if (active && !shown.some((p) => p.slug === slug)) shown.push(active);
        setVoices(shown);
        setCurrent(slug);
        setPicked(slug);
        setLoaded(true);
      })
      .catch(() => {
        if (!cancelled) setError("Could not load the voices.");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function save(slug: string): Promise<boolean> {
    if (slug === current) return true;
    setSaving(true);
    setError(null);
    try {
      const updated = await patchAgent("executive", {
        voice_persona_slug: slug === DIRECT ? null : slug,
      });
      setCurrent(updated.voice_persona_slug ?? DIRECT);
      return true;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the voice.");
      setPicked(current);
      return false;
    } finally {
      setSaving(false);
    }
  }

  async function pick(slug: string) {
    setPicked(slug);
    if (variant === "card") await save(slug);
  }

  async function continueStep() {
    if (await save(picked)) onDone?.();
  }

  return (
    <div>
      <div role="radiogroup" aria-label="Executive voice" className="grid gap-3 sm:grid-cols-3">
        {voices.map((v) => {
          const selected = picked === v.slug;
          return (
            <button
              key={v.slug}
              type="button"
              role="radio"
              aria-checked={selected}
              onClick={() => void pick(v.slug)}
              disabled={saving || !loaded}
              className={`flex flex-col justify-start text-left rounded-2xl border-2 p-4 transition-colors cursor-pointer disabled:cursor-not-allowed focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 ${
                selected
                  ? "border-accent bg-accent/10"
                  : "border-line bg-surface-elevated hover:border-accent/50"
              }`}
            >
              <span className="flex items-center justify-between gap-2 text-base font-semibold text-fg">
                {v.display_name}
                {selected && <Icon name="check" size="w-4 h-4" className="text-accent" />}
              </span>
              {v.description ? (
                <span className="block text-sm text-fg-muted mt-1 leading-relaxed">
                  {v.description}
                </span>
              ) : (
                <span className="block text-sm text-fg-muted mt-1">Your current voice</span>
              )}
              {v.sample && (
                <span className="block text-[13px] text-fg-subtle italic mt-3 leading-relaxed">
                  &ldquo;{v.sample}&rdquo;
                </span>
              )}
            </button>
          );
        })}
      </div>

      {error && <p className="mt-3 text-sm text-red-500">{error}</p>}

      {variant === "card" && (
        <p className="mt-3 text-sm text-fg-subtle">
          {saving
            ? "Saving…"
            : "Applies from the next message. Custom voices are made in Agent Council → Executive → Advanced settings."}
        </p>
      )}

      {variant === "step" && (
        <div className="mt-6 flex items-center gap-3">
          <button
            type="button"
            onClick={() => void continueStep()}
            disabled={saving || !loaded}
            className="px-4 py-2 rounded-lg text-sm font-medium bg-indigo-500 hover:bg-indigo-600 text-white transition-colors cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {saving ? "Saving…" : "Continue"}
          </button>
          <button
            type="button"
            onClick={onDone}
            disabled={saving}
            className="px-3 py-2 rounded-lg text-sm text-fg-muted hover:text-fg transition-colors cursor-pointer disabled:opacity-50"
          >
            Skip for now
          </button>
        </div>
      )}
    </div>
  );
}
