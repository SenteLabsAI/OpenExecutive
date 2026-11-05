// Home's Done for you: what the Executive did, or is doing, for the reader
// in the last day (GET /today `done_for_you`), one row per thing. These
// helpers say what a row's status chip and link read. No imports, so
// `npm test` can exercise this under `node --experimental-strip-types`
// (see scripts/doneForYou.test.mjs).

export interface DoneRowLike {
  kind: string;
  status: "working" | "done" | "failed";
  session_id?: string | null;
  artifact_id?: string | null;
}

/** The chip on a row. */
export function statusLabel(status: DoneRowLike["status"]): string {
  return status === "working" ? "Working on it" : status === "failed" ? "Didn't work" : "Done";
}

/** Where a row leads, and what its button says: the chat that did the work,
 *  else its write-up. Null when there is nothing more to open. */
export function doneLink(row: DoneRowLike): { href: string; label: string } | null {
  if (row.session_id) {
    return {
      href: `/?session=${encodeURIComponent(row.session_id)}`,
      label: row.status === "working" ? "Watch" : "See",
    };
  }
  if (row.artifact_id) return { href: `/artifacts/${encodeURIComponent(row.artifact_id)}`, label: "Read it" };
  return null;
}

/** True while any row is still running, so Home checks again soon. */
export function anyWorking(rows: DoneRowLike[]): boolean {
  return rows.some((r) => r.status === "working");
}
