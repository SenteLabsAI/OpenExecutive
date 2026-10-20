import type { MayDoOutcome, MayDoRow, MayDoSection, WhatItMayDo } from "./api";

// What it does for you (components/delegate/WhatItDoes.tsx): how its rows
// group by outcome, and which rows changed between two loads.

export type Group = "goes" | "asks" | "drafts" | "off";

export const GROUP_OF: Record<MayDoOutcome, Group> = {
  goes: "goes",
  waits: "asks",
  always_waits: "asks",
  drafts: "drafts",
  off: "off",
};

export const GROUP_ORDER: Group[] = ["goes", "asks", "drafts", "off"];

export function groupTitle(group: Group, section: MayDoSection["key"]): string {
  if (group === "goes") {
    return section === "as_you" ? "Sends on its own" : section === "departments" ? "Acts on its own" : "Does on its own";
  }
  if (group === "asks") return section === "departments" ? "Waits for an approver" : "Asks first";
  if (group === "drafts") return "Writes for you to send";
  return "Off";
}

/** A row's key across sections: ids repeat (``rules``, ``lead``). */
export function rowId(section: MayDoSection["key"], row: MayDoRow): string {
  return `${section}:${row.id}`;
}

/** What each row says, by ``rowId``, to compare with the next load. */
export function snapshot(data: WhatItMayDo | null): Map<string, string> {
  const rows = new Map<string, string>();
  data?.sections.forEach((s) => s.rows.forEach((r) => rows.set(rowId(s.key, r), `${r.outcome}|${r.what}|${r.why}`)));
  return rows;
}

/** The rows (by ``rowId``) that are new or say something different since
 *  ``before``; none on the first load. */
export function changedRows(before: Map<string, string> | null, after: Map<string, string>): Set<string> {
  const changed = new Set<string>();
  if (!before) return changed;
  after.forEach((said, id) => {
    if (before.get(id) !== said) changed.add(id);
  });
  return changed;
}
