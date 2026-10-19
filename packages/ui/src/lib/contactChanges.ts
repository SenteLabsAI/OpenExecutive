// How the Executive's changes to the contacts read (people/contact_keeper.py):
// "Changes I made" on Contacts and the lines on a person's page.
// Plain functions, so scripts/contactChanges.test.mjs can check the wording.

import type { ContactChange } from "./api";

export interface ChangeLine {
  tag: "Added" | "Updated" | "Suggested";
  title: string;
  detail: string;
}

function times(n: number): string {
  return n === 1 ? "once" : n === 2 ? "twice" : `${n} times`;
}

/** Why the change was made, in a few words. */
export function changeReason(change: ContactChange): string {
  switch (change.reason) {
    case "emailed":
      return change.action === "add"
        ? `You emailed them ${times(change.evidence)} lately.`
        : "You emailed them at this address.";
    case "met":
      return change.evidence > 1 ? `Guest at ${change.evidence} of your meetings.` : "Guest at one of your meetings.";
    case "asked":
      return "You asked in chat.";
    case "from_line":
      return "From their own emails.";
    case "signature":
      return "From their email signature.";
  }
}

export function changeLine(change: ContactChange): ChangeLine {
  const tag = change.status === "suggested" ? "Suggested" : change.action === "add" ? "Added" : "Updated";
  if (change.action === "add") {
    return { tag, title: change.full_name, detail: `${change.email}. ${changeReason(change)}` };
  }
  const what =
    change.field === "role"
      ? `Role is now ${change.new_value}.`
      : change.field === "full_name"
        ? `Name is now ${change.new_value}.`
        : `Also writes from ${change.new_value}.`;
  return { tag, title: change.full_name, detail: `${what} ${changeReason(change)}` };
}

/** The changes for the "Changes I made" card: open suggestions, then what
 * wasn't cleared with Got it. */
export function cardChanges(changes: ContactChange[]): ContactChange[] {
  return changes.filter((c) => c.status === "suggested" || (c.status === "applied" && !c.seen));
}

/** People the card lists as just added, for the "New" badge. */
export function newlyAdded(changes: ContactChange[]): Set<number> {
  return new Set(
    cardChanges(changes)
      .filter((c) => c.action === "add" && c.status === "applied" && c.person_id !== null)
      .map((c) => c.person_id as number),
  );
}

/** Why the switch can't do anything yet, or null. */
export function keeperBlocked(status: string): string | null {
  if (status === "act_as_me_off") return "It starts once Act as me is on.";
  if (["no_email", "not_configured", "needs_reconnect", "mismatch", "shared_mailbox"].includes(status)) {
    return "It starts once your own mailbox is connected in Act as me.";
  }
  return null;
}

/** The line on a person's page about how they were added, or null. */
export function addedByLine(changes: ContactChange[], when: (iso: string) => string): string | null {
  const add = changes.find((c) => c.action === "add" && c.status === "applied" && c.reason !== "asked");
  if (!add) return null;
  return `Added by me on ${when(add.created_at)}. ${changeReason(add)}`;
}
