/** Shared presentation of a workflow run's status.
 *
 * Both the jobs list and the run-detail page render the same six statuses, and
 * an earlier version of this feature defined the label map and the colour
 * branching separately in each — byte-for-byte identical, and free to drift
 * the moment a seventh status arrives. One source, imported by both.
 */
import { WorkflowRunStatus } from "@/lib/api";

/** Raw status strings are for the database. "awaiting_human" and "timed_out"
 *  mean nothing to someone scanning a list of jobs. */
export const RUN_STATUS_LABELS: Record<string, string> = {
  running: "running",
  done: "done",
  error: "failed",
  awaiting_human: "awaiting sign-off",
  resolved: "resuming",
  timed_out: "no reply",
};

export function runStatusLabel(status: string): string {
  return RUN_STATUS_LABELS[status] ?? status;
}

/** Tailwind text colour per status. `timed_out` reads as a failure from the
 *  viewer's side — nobody answered and the run produced nothing — so it shares
 *  the error colour rather than looking like it is still going. */
export function runStatusTextColor(status: string): string {
  if (status === "done") return "text-emerald-400";
  if (status === "error" || status === "timed_out") return "text-red-400";
  if (status === "resolved") return "text-indigo-400";
  return "text-amber-400";
}

/** The badge variant used in list views: same semantics, with a ring + tint. */
export function runStatusBadgeColor(status: string): string {
  if (status === "done")
    return "bg-emerald-500/10 text-emerald-400 ring-emerald-500/30";
  if (status === "error" || status === "timed_out")
    return "bg-red-500/10 text-red-400 ring-red-500/30";
  if (status === "resolved")
    return "bg-indigo-500/10 text-indigo-400 ring-indigo-500/30";
  return "bg-amber-500/10 text-amber-400 ring-amber-500/30";
}

/** Coarse bucket for the runs sub-tabs.
 *
 * `resolved` groups with active, not done: the answer is in and the resumer is
 * about to run the steps after the gate. `awaiting_human` is its own bucket —
 * it is the one state that needs a person to DO something, and folding it into
 * "active" hid it among jobs that are merely still running.
 */
export type RunBucket = "active" | "awaiting" | "done" | "error";

export function runStatusBucket(s: WorkflowRunStatus): RunBucket {
  if (s === "running" || s === "resolved") return "active";
  if (s === "awaiting_human") return "awaiting";
  if (s === "error" || s === "timed_out") return "error";
  if (s === "done") return "done";
  // Defensive: unknown future statuses surface under Active so they're not lost.
  return "active";
}
