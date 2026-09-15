"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import Link from "next/link";
import { useAskOEFormContext } from "@/components/askoe/AskOEContext";
import {
  ProfileSections,
  snapshotProfile,
  coerceList,
  TEXT_FIELDS,
  NUM_FIELDS,
  LIST_FIELDS,
  type PendingValues,
} from "@/components/company-profile/ProfileSections";
import {
  getCompanyProfile,
  updateCompanyProfile,
  type CompanyProfile,
  type PageFormField,
} from "@/lib/api";

// ── page ─────────────────────────────────────────────────────────────────────

export default function CompanyProfilePage() {
  const [profile, setProfile] = useState<CompanyProfile | null>(null);
  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const [saving, setSaving] = useState(false);
  const [pending, setPending] = useState<PendingValues | null>(null);
  const seqRef = useRef(0);

  useEffect(() => {
    getCompanyProfile()
      .then(setProfile)
      .catch((err: Error) => {
        if (err.message === "404") setNotFound(true);
      })
      .finally(() => setLoading(false));
  }, []);

  const save = useCallback(
    async (patch: Partial<CompanyProfile>) => {
      setSaving(true);
      try {
        const updated = await updateCompanyProfile(patch);
        setProfile(updated);
      } finally {
        setSaving(false);
      }
    },
    []
  );

  // Register with Ask OE once the profile is loaded. Field values are the
  // SAVED profile values — unsaved per-section drafts stay local to each
  // section until the user hits Save.
  useAskOEFormContext(
    profile
      ? {
          formId: "company_profile",
          title: "Company profile",
          description:
            "The structured company profile the Executive grounds every answer in. " +
            "Applied values open the matching section in edit mode; the user saves per section.",
          getFields: (): PageFormField[] => {
            const flat = snapshotProfile(profile);
            const label = (k: string) => k.replaceAll("_", " ");
            return Object.entries(flat).map(([name, value]) => ({
              name,
              label: label(name),
              type: NUM_FIELDS.has(name)
                ? ("number" as const)
                : LIST_FIELDS.has(name)
                  ? ("json" as const)
                  : ("text" as const),
              value,
              description: LIST_FIELDS.has(name) ? "JSON array of strings." : "",
            }));
          },
          applyPatch: (values) => {
            const applied: string[] = [];
            const skipped: string[] = [];
            const picked: Record<string, unknown> = {};
            for (const [key, raw] of Object.entries(values)) {
              if (TEXT_FIELDS.has(key) && typeof raw === "string") {
                picked[key] = raw;
                applied.push(key);
              } else if (NUM_FIELDS.has(key) && Number.isFinite(Number(raw))) {
                picked[key] = Number(raw);
                applied.push(key);
              } else if (LIST_FIELDS.has(key)) {
                const list = coerceList(raw);
                if (list !== null) {
                  picked[key] = list;
                  applied.push(key);
                } else skipped.push(key);
              } else skipped.push(key);
            }
            if (applied.length > 0) {
              setPending({ seq: ++seqRef.current, values: picked });
            }
            const savedSnapshot = snapshotProfile(profile);
            return {
              applied,
              skipped,
              undo: () => {
                // Restore the SAVED values for the touched fields; sections
                // stay in edit mode so the user sees what was restored.
                const restore: Record<string, unknown> = {};
                for (const k of applied) restore[k] = savedSnapshot[k];
                setPending({ seq: ++seqRef.current, values: restore });
              },
            };
          },
        }
      : null
  );

  return (
    <div className="flex flex-col h-full bg-surface">
      <main className="flex-1 overflow-y-auto">
        <div className="max-w-3xl mx-auto px-6 py-10">

          {loading && (
            <div className="flex items-center justify-center h-40">
              <div className="w-5 h-5 border-2 border-indigo-500 border-t-transparent rounded-full animate-spin" />
            </div>
          )}

          {notFound && (
            <div className="bg-indigo-500/10 border border-indigo-500/20 rounded-xl px-5 py-4 flex items-center justify-between">
              <p className="text-sm text-fg">No company profile set up yet.</p>
              <Link href="/onboard" className="text-xs text-indigo-400 hover:text-indigo-300 font-medium transition-colors">
                Complete setup →
              </Link>
            </div>
          )}

          {profile && (
            <>
              <div className="flex items-center justify-between mb-8">
                <div>
                  <h1 className="text-lg font-semibold text-fg">{profile.name}</h1>
                  <p className="text-sm text-fg-muted mt-0.5">{[profile.industry, profile.stage].filter(Boolean).join(" · ")}</p>
                </div>
                <Link href="/onboard" className="text-xs text-fg-muted hover:text-fg-muted transition-colors">
                  Re-run setup →
                </Link>
              </div>

              <ProfileSections profile={profile} saving={saving} onSave={save} pending={pending} />
            </>
          )}
        </div>
      </main>
    </div>
  );
}
