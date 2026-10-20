// How the Executive's changes to the contacts read (people/contact_keeper.py):
// "Changes I made" on Contacts and the lines on a person's page; and a
// person's other contact details (people/contact_info.py), as they read on
// their page and as the edit form types them, one "Mobile: 555 0100" a line.
// Plain functions, so scripts/contactChanges.test.mjs can check the wording.

import type { ContactChange, ContactDetail, ContactNote, DetailKind } from "./api";

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

/** What a changed field is called on a person's page. */
export function fieldLabel(field: ContactChange["field"]): string {
  if (field === "role") return "Role and company";
  if (field === "full_name") return "Name";
  if (field === "email_alias") return "Other address";
  if (field === "contact") return "Contact";
  return DETAIL_LABELS[field];
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
        : change.field === "email_alias"
          ? `Also writes from ${change.new_value}.`
          : `${fieldLabel(change.field)} ${change.old_value ? "is now" : "added:"} ${change.new_value}.`;
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

/** When you were last in touch with a contact, one fact per line, newest
 * kinds first. Empty when nothing is known. */
export function contactActivityLines(note: ContactNote, day: (iso: string) => string): string[] {
  const out: string[] = [];
  if (note.next_meeting_at) {
    const title = note.next_meeting_title ? `: ${note.next_meeting_title}` : "";
    out.push(`Next meeting ${day(note.next_meeting_at)}${title}`);
  }
  if (note.last_emailed_at) {
    const count = note.emailed_count > 1 ? ` (${note.emailed_count} emails in 60 days)` : "";
    out.push(`You last emailed them ${day(note.last_emailed_at)}${count}`);
  }
  if (note.last_heard_at) out.push(`They last wrote to you ${day(note.last_heard_at)}`);
  if (note.last_met_at) out.push(`You last met ${day(note.last_met_at)}`);
  return out;
}

// --- other contact details ------------------------------------------------

export const DETAIL_LABELS: Record<DetailKind, string> = {
  phone: "Phone",
  mobile: "Mobile",
  website: "Website",
  linkedin: "LinkedIn",
  social: "Profile",
  address: "Address",
};

const KIND_BY_LABEL: Record<string, DetailKind> = {
  phone: "phone",
  office: "phone",
  tel: "phone",
  mobile: "mobile",
  cell: "mobile",
  website: "website",
  web: "website",
  site: "website",
  linkedin: "linkedin",
  profile: "social",
  social: "social",
  address: "address",
};

/** Where a detail opens: a phone dials, a link opens; null for an address. */
export function detailHref(detail: ContactDetail): string | null {
  if (detail.kind === "phone" || detail.kind === "mobile") {
    const dial = detail.value.replace(/(?:\s*(?:x|ext\.?)\s*\d+)$/i, "").replace(/[^\d+]/g, "");
    return dial ? `tel:${dial}` : null;
  }
  if (detail.kind === "address") return null;
  return /^https:\/\//.test(detail.value) ? detail.value : null;
}

/** What a link shows: without https:// and www. */
export function detailText(detail: ContactDetail): string {
  if (detail.kind === "phone" || detail.kind === "mobile" || detail.kind === "address") return detail.value;
  return detail.value.replace(/^https?:\/\//, "").replace(/^www\./, "");
}

/** The edit form's text: one "Label: value" per line. */
export function detailsToText(details: ContactDetail[]): string {
  return details.map((d) => `${DETAIL_LABELS[d.kind]}: ${d.value}`).join("\n");
}

/** The edit form's text back as details. A line without a known label is
 * read as a phone when it is mostly digits, a website when it looks like
 * one, else an address. */
export function textToDetails(text: string): ContactDetail[] {
  const out: ContactDetail[] = [];
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const match = /^([A-Za-z]+)\s*:\s*(.+)$/.exec(line);
    const kind = match ? KIND_BY_LABEL[match[1].toLowerCase()] : undefined;
    if (kind && match) {
      out.push({ kind, value: match[2].trim() });
      continue;
    }
    if (/^[+\d\s().\-/]+$/.test(line)) out.push({ kind: "phone", value: line });
    else if (/linkedin\.com\//i.test(line)) out.push({ kind: "linkedin", value: line });
    else if (/^(https?:\/\/)?[\w-]+(\.[\w-]+)+(\/\S*)?$/i.test(line)) out.push({ kind: "website", value: line });
    else out.push({ kind: "address", value: line });
  }
  return out;
}
