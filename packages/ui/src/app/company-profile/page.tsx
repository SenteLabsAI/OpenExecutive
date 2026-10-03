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
import { PROFILE_COPY } from "@/components/company-profile/profileCopy";
import { profileWording } from "@/components/shell/navConfig";
import { buttonClass } from "@/components/ui/Button";
import OverflowMenu from "@/components/ui/OverflowMenu";
import { useWorkspace } from "@/components/workspace/WorkspaceContext";
import {
  getCompanyProfile,
  updateCompanyProfile,
  type CompanyProfile,
  type PageFormField,
} from "@/lib/api";

// ── page ─────────────────────────────────────────────────────────────────────

export default function CompanyProfilePage() {
  // Team: the company profile, as always. Solo: a business owner's business,
  // anyone else's work — the same fields, worded for them.
  const { mode, role } = useWorkspace();
  const wording = profileWording(mode, role.role_kind);
  const copy = PROFILE_COPY[wording];
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
        <div className="max-w-6xl mx-auto px-4 py-8 sm:px-6">

          {loading && (
            <div className="flex items-center justify-center h-40">
              <div className="w-6 h-6 border-2 border-accent border-t-transparent rounded-full animate-spin" />
            </div>
          )}

          {notFound && (
            <div className="max-w-3xl bg-accent/10 border border-accent/20 rounded-2xl px-5 py-5 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
              <p className="text-[15px] text-fg">{copy.missing}</p>
              <Link href="/onboard" className={buttonClass("primary", "md", "flex-shrink-0")}>
                Complete setup
              </Link>
            </div>
          )}

          {profile && (
            <>
              <div className={`flex items-start justify-between gap-4 ${copy.intro ? "mb-4" : "mb-8"}`}>
                <div className="min-w-0">
                  <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-fg">{profile.name}</h1>
                  <p className="text-[15px] text-fg-muted mt-1.5">{[profile.industry, profile.stage].filter(Boolean).join(" · ")}</p>
                </div>
                <OverflowMenu
                  label="More profile actions"
                  items={[{ label: "Re-run setup", href: "/onboard" }]}
                />
              </div>

              {copy.intro && (
                <p className="text-[15px] text-fg-muted leading-relaxed mb-8 max-w-3xl">
                  {copy.intro}
                  {copy.roleNote && (
                    <>
                      {" "}
                      {copy.roleNote}{" "}
                      <Link href="/settings/workspace" className="whitespace-nowrap text-accent hover:underline">
                        Settings → Workspace
                      </Link>
                      .
                    </>
                  )}
                </p>
              )}

              <ProfileSections
                profile={profile}
                saving={saving}
                onSave={save}
                pending={pending}
                wording={wording}
                layout="columns"
              />
            </>
          )}
        </div>
      </main>
    </div>
  );
}
