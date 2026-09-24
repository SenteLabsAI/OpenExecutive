import type { ReviewStatus } from "@/lib/api";

// Shared by the review queue and the Knowledge base file view so both label a
// file's review state the same way.

const STATUS_LABELS: Record<ReviewStatus, string> = {
  pending: "Pending",
  approved: "Approved",
  rejected: "Rejected",
  needs_revision: "Needs revision",
};

const STATUS_CLASSES: Record<ReviewStatus, string> = {
  pending: "bg-amber-950/60 text-amber-400 border border-amber-900/60",
  approved: "bg-emerald-950/60 text-emerald-400 border border-emerald-900/60",
  rejected: "bg-red-950/60 text-red-400 border border-red-900/60",
  needs_revision: "bg-violet-950/60 text-violet-400 border border-violet-900/60",
};

const TRUSTED_DEFAULT_CLASS =
  "bg-surface-overlay/60 text-fg-muted border border-line-strong/60";

export default function ReviewStatusPill({
  status,
  reviewedAt,
  trustedDefault,
}: {
  status: ReviewStatus;
  reviewedAt?: string | null;
  trustedDefault?: boolean;
}) {
  // Provenance comes from the server, never inferred: a user's own upload can
  // also sit approved-with-no-timestamp, and labelling it "Ships with Open
  // Executive" would be a lie about where the content came from.
  const trusted = trustedDefault === true && status === "approved" && reviewedAt == null;
  return (
    <span
      className={`text-[10px] font-medium px-1.5 py-0.5 rounded-full ${
        trusted ? TRUSTED_DEFAULT_CLASS : STATUS_CLASSES[status]
      }`}
      title={
        trusted
          ? "Ships with Open Executive. Available to the Executive, but nobody here has reviewed it."
          : undefined
      }
    >
      {trusted ? "Default" : STATUS_LABELS[status]}
    </span>
  );
}
