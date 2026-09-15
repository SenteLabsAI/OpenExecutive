"use client";

import { useCallback, useEffect, useState } from "react";
import { ProfileSections } from "@/components/company-profile/ProfileSections";
import OnboardDepartmentsDraft from "@/components/onboard/OnboardDepartmentsDraft";
import OnboardPeopleDraft from "@/components/onboard/OnboardPeopleDraft";
import {
  commitOnboardDraft,
  listDepartments,
  type CompanyProfile,
  type OnboardDepartmentDraft,
  type OnboardPersonDraft,
  type OnboardTurn,
} from "@/lib/api";

interface Props {
  turn: OnboardTurn;
  onBackToConversation: () => void;
  onSaved: () => void;
}

export default function OnboardDraftReview({
  turn,
  onBackToConversation,
  onSaved,
}: Props) {
  // The draft is local state until the single commit at the end — every edit
  // below, including the ProfileSections ones, just merges into it.
  const [profile, setProfile] = useState<CompanyProfile>(turn.draft!);
  const [people, setPeople] = useState<OnboardPersonDraft[]>(turn.draft_people);
  const [departments, setDepartments] = useState<OnboardDepartmentDraft[]>(
    turn.draft_departments
  );
  const [existingTitles, setExistingTitles] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listDepartments()
      .then((ds) => setExistingTitles(ds.map((d) => d.config.title)))
      .catch(() => setExistingTitles([]));
  }, []);

  // The seam that lets the /company-profile section editors work here: they
  // call onSave with a patch, and we merge it locally instead of PATCHing.
  const mergeIntoDraft = useCallback(async (patch: Partial<CompanyProfile>) => {
    setProfile((prev) => ({ ...prev, ...patch }));
  }, []);

  // Check the principal over the people we actually SEND, not over all rows —
  // a blank row flagged "this is me" is filtered out server-side, which would
  // otherwise save a company with no principal at all.
  const namedPeople = people.filter((p) => p.full_name.trim());
  const principals = namedPeople.filter((p) => p.is_principal).length;
  const namedDepartments = departments.filter((d) => d.title.trim());
  const duplicateNames =
    new Set(namedPeople.map((p) => p.full_name.trim().toLowerCase())).size !==
    namedPeople.length;
  const duplicateDepartments =
    new Set(namedDepartments.map((d) => d.title.trim().toLowerCase())).size !==
    namedDepartments.length;

  const blocker = !profile.name.trim()
    ? "Your company needs a name."
    : namedPeople.length === 0
      ? "Add at least one person, and mark which one is you."
      : principals !== 1
        ? "Mark exactly one person as you."
        : duplicateNames
          ? "Two people have the same name — give them distinct names."
          : duplicateDepartments
            ? "Two departments have the same name."
            : null;

  async function save() {
    if (blocker || saving) return;
    setSaving(true);
    setError(null);
    try {
      await commitOnboardDraft(turn.session_id, profile, namedPeople, namedDepartments);
      onSaved();
    } catch (err) {
      setError((err as Error).message);
      setSaving(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-lg font-semibold text-fg">Here&rsquo;s what I understood</h1>
        <p className="text-sm text-fg-muted mt-0.5">
          Nothing is saved yet. Edit anything that&rsquo;s off, then save.
        </p>
      </div>

      {turn.summary && (
        <div className="bg-surface-elevated border border-line rounded-xl px-5 py-4">
          <p className="text-sm text-fg whitespace-pre-wrap">{turn.summary}</p>
        </div>
      )}

      {turn.confidence_notes.length > 0 && (
        <div className="bg-amber-500/10 border border-amber-500/20 rounded-xl px-5 py-4">
          <p className="text-xs font-medium text-amber-400 uppercase tracking-wide mb-2">
            I couldn&rsquo;t determine these
          </p>
          <ul className="flex flex-col gap-1">
            {turn.confidence_notes.map((note, i) => (
              <li key={i} className="text-sm text-fg">
                {note}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* "org" is omitted: org_structure is derived from the two tables below
          when you save, so editing it here would be a second source of truth. */}
      <ProfileSections
        profile={profile}
        saving={false}
        onSave={mergeIntoDraft}
        omit={["org"]}
      />

      <OnboardPeopleDraft
        people={people}
        onChange={(next) => {
          // Keep department heads in sync. A renamed person would otherwise
          // leave a head_person_name matching nobody, which the server drops
          // silently — the head would just vanish on save.
          const valid = new Set(next.map((p) => p.full_name.trim()).filter(Boolean));
          const renamed = new Map<string, string>();
          next.forEach((p, i) => {
            const before = people[i]?.full_name.trim();
            const after = p.full_name.trim();
            if (before && after && before !== after) renamed.set(before, after);
          });
          setDepartments((ds) =>
            ds.map((d) => {
              const head = d.head_person_name.trim();
              if (!head) return d;
              const moved = renamed.get(head);
              if (moved) return { ...d, head_person_name: moved };
              return valid.has(head) ? d : { ...d, head_person_name: "" };
            })
          );
          setPeople(next);
        }}
      />
      <OnboardDepartmentsDraft
        departments={departments}
        people={people}
        existingTitles={existingTitles}
        onChange={setDepartments}
      />

      {error && <p className="text-sm text-red-400">{error}</p>}
      {blocker && <p className="text-xs text-fg-muted">{blocker}</p>}

      <div className="flex items-center gap-3 pb-10">
        <button
          onClick={() => void save()}
          disabled={saving || blocker !== null}
          className="px-4 py-2 bg-indigo-500 hover:bg-indigo-600 disabled:opacity-40 text-white text-sm font-medium rounded-lg transition-colors"
        >
          {saving ? "Saving…" : "Save & finish setup"}
        </button>
        <button
          onClick={onBackToConversation}
          disabled={saving}
          className="text-xs text-fg-muted hover:text-fg disabled:opacity-40 transition-colors"
        >
          Not quite — ask me more questions
        </button>
      </div>
    </div>
  );
}
