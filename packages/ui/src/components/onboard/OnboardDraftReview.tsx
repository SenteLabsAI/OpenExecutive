"use client";

import { useCallback, useEffect, useState } from "react";
import { useSession } from "next-auth/react";
import { ProfileSections } from "@/components/company-profile/ProfileSections";
import OnboardDepartmentsDraft from "@/components/onboard/OnboardDepartmentsDraft";
import OnboardPeopleDraft from "@/components/onboard/OnboardPeopleDraft";
import { profileWording } from "@/components/shell/navConfig";
import { useWorkspace } from "@/components/workspace/WorkspaceContext";
import {
  commitOnboardDraft,
  listDepartments,
  listPeople,
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
  // Solo (one person, just for themselves): only "you" is shown and saved —
  // the drafted principal, else the first person drafted, else a blank row —
  // and no departments; the existing areas stay as they are.
  const { mode, role: workspaceRole } = useWorkspace();
  const solo = mode === "solo";
  // The profile's headings follow the role, as on /company-profile.
  const wording = profileWording(mode, workspaceRole.role_kind);
  const [me, setMe] = useState<OnboardPersonDraft>(() => {
    const drafted = turn.draft_people.find((p) => p.is_principal) ?? turn.draft_people[0];
    // The title from the role step, when the draft has none.
    const role = drafted?.role || workspaceRole.role_title || "";
    return { full_name: drafted?.full_name ?? "", role, is_principal: true };
  });
  const [existingTitles, setExistingTitles] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The owner's sign-in email. Pre-filled with the owner's current one when
  // setup is re-run — so whoever re-runs it doesn't silently take over the
  // owner's login — otherwise with the login doing the setup. Null until
  // edited, so the field follows both as they load.
  // Save waits for both: the commit runs once per setup session, so saving
  // before they load would send no email with no way to add it afterwards.
  const { data: session, status: sessionStatus } = useSession();
  const [currentOwnerEmail, setCurrentOwnerEmail] = useState<string | null>(null);
  const [ownerLookupDone, setOwnerLookupDone] = useState(false);
  const [ownerEmailEdit, setOwnerEmailEdit] = useState<string | null>(null);
  const ownerEmail = ownerEmailEdit ?? currentOwnerEmail ?? session?.user?.email ?? "";

  useEffect(() => {
    listDepartments()
      .then((ds) => setExistingTitles(ds.map((d) => d.config.title)))
      .catch(() => setExistingTitles([]));
    listPeople()
      .then((ps) => setCurrentOwnerEmail(ps.find((p) => p.is_principal && p.email)?.email ?? null))
      .catch(() => setCurrentOwnerEmail(null))
      .finally(() => setOwnerLookupDone(true));
  }, []);

  // The seam that lets the /company-profile section editors work here: they
  // call onSave with a patch, and we merge it locally instead of PATCHing.
  const mergeIntoDraft = useCallback(async (patch: Partial<CompanyProfile>) => {
    setProfile((prev) => ({ ...prev, ...patch }));
  }, []);

  // Check the principal over the people we actually SEND, not over all rows —
  // a blank row flagged "this is me" is filtered out server-side, which would
  // otherwise save a company with no principal at all.
  const namedPeople = solo
    ? me.full_name.trim()
      ? [{ ...me, is_principal: true }]
      : []
    : people.filter((p) => p.full_name.trim());
  const principals = namedPeople.filter((p) => p.is_principal).length;
  const namedDepartments = solo ? [] : departments.filter((d) => d.title.trim());
  const duplicateNames =
    new Set(namedPeople.map((p) => p.full_name.trim().toLowerCase())).size !==
    namedPeople.length;
  const duplicateDepartments =
    new Set(namedDepartments.map((d) => d.title.trim().toLowerCase())).size !==
    namedDepartments.length;

  // The owner email is checked only by the server (check_owner_email): a
  // rejection comes back as the error below and leaves the draft editable.
  const blocker = !profile.name.trim()
    ? solo
      ? workspaceRole.role_kind === "owner"
        ? "Your business needs a name."
        : "Add the name of the business or organisation you work in."
      : "Your company needs a name."
    : namedPeople.length === 0
      ? solo
        ? "Add your name."
        : "Add at least one person, and mark which one is you."
      : principals !== 1
        ? "Mark exactly one person as you."
        : duplicateNames
          ? "Two people have the same name — give them distinct names."
          : duplicateDepartments
            ? "Two departments have the same name."
            : sessionStatus === "loading" || !ownerLookupDone
              ? "Loading your sign-in details…"
              : null;

  async function save() {
    if (blocker || saving) return;
    setSaving(true);
    setError(null);
    try {
      await commitOnboardDraft(turn.session_id, profile, namedPeople, namedDepartments, ownerEmail);
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
        wording={wording}
      />

      {solo ? (
        <div className="bg-surface-elevated border border-line rounded-xl p-5">
          <h2 className="text-sm font-semibold text-fg">You</h2>
          <p className="text-xs text-fg-muted mt-1 mb-3">
            The Executive reports to you. Add clients, partners and anyone else you work
            with later, on the People page.
          </p>
          <div className="flex flex-col sm:flex-row gap-2">
            <input
              value={me.full_name}
              onChange={(e) => setMe((m) => ({ ...m, full_name: e.target.value }))}
              placeholder="Your full name"
              aria-label="Your full name"
              autoComplete="name"
              className="flex-1 rounded-lg border border-line-strong bg-surface-overlay px-3 py-2 text-sm text-fg placeholder-fg-subtle focus:outline-none focus:ring-2 focus:ring-indigo-500/50 transition-colors"
            />
            <input
              value={me.role}
              onChange={(e) => setMe((m) => ({ ...m, role: e.target.value }))}
              placeholder="Your title, e.g. Owner or Director of Operations"
              aria-label="Your role"
              className="flex-1 rounded-lg border border-line-strong bg-surface-overlay px-3 py-2 text-sm text-fg placeholder-fg-subtle focus:outline-none focus:ring-2 focus:ring-indigo-500/50 transition-colors"
            />
          </div>
        </div>
      ) : (
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
      )}
      <div className="bg-surface-elevated border border-line rounded-xl p-5">
        <label htmlFor="owner-email" className="text-sm font-semibold text-fg">
          Your sign-in email
        </label>
        <p className="text-xs text-fg-muted mt-1 mb-3">
          {solo ? (
            "Links you to this login, so your chats and full access work straight away."
          ) : (
            <>
              Links the person marked &ldquo;This is me&rdquo; to this login, so your chats and
              owner rights work straight away.
            </>
          )}{" "}
          Setting this up for someone else? Enter the Google email they will sign in with.
          {session?.localLogin &&
            " Optional on this computer — add one if you set up Google sign-in later."}
        </p>
        <input
          id="owner-email"
          type="email"
          value={ownerEmail}
          onChange={(e) => setOwnerEmailEdit(e.target.value)}
          placeholder="you@company.com"
          autoComplete="email"
          className="w-full rounded-lg border border-line-strong bg-surface-overlay px-3 py-2 text-sm text-fg placeholder-fg-subtle focus:outline-none focus:ring-2 focus:ring-indigo-500/50 transition-colors"
        />
      </div>
      {!solo && (
        <OnboardDepartmentsDraft
          departments={departments}
          people={people}
          existingTitles={existingTitles}
          onChange={setDepartments}
        />
      )}

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
