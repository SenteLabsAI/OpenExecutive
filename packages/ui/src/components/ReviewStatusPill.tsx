import type { ReviewStatus } from "@/lib/api";

// Shared by the review queue and the Knowledge base file view so both label a
// file's review state the same way.

const STATUS_LABELS: Record<ReviewStatus, string> = {
  pending: "Pending",
  approved: "Approved",
  rejected: "Rejected",
  needs_revision: "Needs revision",
};

// Status is a coloured dot plus a word.
const STATUS_DOT: Record<ReviewStatus, string> = {
  pending: "bg-amber-500",
  approved: "bg-emerald-500",
  rejected: "bg-red-500",
  needs_revision: "bg-violet-500",
};

const TRUSTED_DEFAULT_DOT = "bg-fg-subtle";

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
      className="inline-flex items-center gap-1.5 text-sm font-medium text-fg-muted"
      title={
        trusted
          ? "Ships with Open Executive. Available to the Executive, but nobody here has reviewed it."
          : undefined
      }
    >
      <span
        aria-hidden
        className={`h-2 w-2 rounded-full ${trusted ? TRUSTED_DEFAULT_DOT : STATUS_DOT[status]}`}
      />
      {trusted ? "Default" : STATUS_LABELS[status]}
    </span>
  );
}
