// The People page splits the roster into the team and the principal's
// contacts. A contact is someone outside the team (a client, a contractor):
// they cannot sign in or talk to the bot, and the Executive emails or invites
// them only when the principal asks directly. Contacts are private to the
// principal — the API never returns one to anyone else — so the Contacts tab
// is offered to the principal only.
//
// Type-only imports, so `npm test` can exercise this under
// `node --experimental-strip-types` (see scripts/peopleKinds.test.mjs).

import type { Person, PersonKind, WorkspaceMode } from "@/lib/api";

export type PeopleTab = "team" | "contacts";

type KindFields = Pick<Person, "kind" | "is_principal">;

/** A row from an older server has no `kind`: it is a team member. */
export function isContact(person: Pick<Person, "kind">): boolean {
  return person.kind === "contact";
}

/** The tabs this viewer gets: Contacts is the principal's alone. */
export function tabsFor(viewerIsPrincipal: boolean): PeopleTab[] {
  return viewerIsPrincipal ? ["team", "contacts"] : ["team"];
}

/**
 * Where the page opens: using it just for yourself, the people the principal
 * adds are mostly contacts. Anyone else only ever has the Team tab.
 */
export function defaultPeopleTab(mode: WorkspaceMode, viewerIsPrincipal = true): PeopleTab {
  return viewerIsPrincipal && mode === "solo" ? "contacts" : "team";
}

/**
 * The people a tab lists. In solo mode the Team tab is just the principal:
 * there is no team to show.
 */
export function peopleForTab<P extends KindFields>(people: P[], tab: PeopleTab, mode: WorkspaceMode): P[] {
  if (tab === "contacts") return people.filter(isContact);
  const team = people.filter((p) => !isContact(p));
  return mode === "solo" ? team.filter((p) => p.is_principal) : team;
}

/** Team members the solo Team tab leaves out (everyone but the principal). */
export function hiddenTeamCount(people: KindFields[], mode: WorkspaceMode): number {
  if (mode !== "solo") return 0;
  return people.filter((p) => !isContact(p) && !p.is_principal).length;
}

/** The kind the add form starts on: whatever the open tab lists. */
export function defaultKindForTab(tab: PeopleTab): PersonKind {
  return tab === "contacts" ? "contact" : "team";
}

/** The principal is always on the team, whatever the form says. */
export function effectiveKind(kind: PersonKind, isPrincipal: boolean): PersonKind {
  return isPrincipal ? "team" : kind;
}

/**
 * Adding someone to the team while using Open Executive just for yourself
 * means there is a team now: offer to switch the workspace to team mode.
 * Never for the principal's own entry or for a contact.
 */
export function shouldOfferTeamMode(mode: WorkspaceMode, kind: PersonKind, isPrincipal: boolean): boolean {
  return mode === "solo" && effectiveKind(kind, isPrincipal) === "team" && !isPrincipal;
}

export type CardStatusTone = "ok" | "warn" | "muted";

/**
 * The one status a People card shows. A contact: whether there's an email to
 * reach them on. A team member on leave: until when (an ISO date, compared
 * with `today` as YYYY-MM-DD). Anyone else on the team: how fast they're
 * expected to reply. Their approval scopes are on their own page.
 */
export function personCardStatus(
  person: Pick<Person, "kind" | "email" | "on_leave_until" | "response_sla_hours">,
  today: string,
): { label: string; tone: CardStatusTone } {
  if (isContact(person)) {
    return person.email ? { label: "Email on file", tone: "ok" } : { label: "No email", tone: "muted" };
  }
  const leave = person.on_leave_until?.slice(0, 10);
  if (leave && leave >= today) return { label: `On leave until ${leave}`, tone: "warn" };
  return { label: `Replies within ${person.response_sla_hours}h`, tone: "ok" };
}
