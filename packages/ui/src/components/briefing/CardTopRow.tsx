// The row every card in Needs you opens with: what it needs from you, who it
// acts as (As you, As me) or where it came from (Watch list, Workflow, ...),
// and how old it is. A card someone else is waiting on says who, last.

import FeatureName, { type Feature } from "@/components/FeatureName";
import { askerName } from "@/lib/needsYou";
import { formatRelativeTime } from "@/lib/relativeTime";

import { Chip } from "./shared";

// "Sam asked", from Sam Okafor; "Someone on the team asked" stays whole.
export default function CardTopRow({
  need,
  mode,
  source,
  at,
  waitingOn,
  tag,
  className = "mb-2",
}: {
  need?: string;
  // As you / As me: the named feature's own tag.
  mode?: Feature;
  // Where it came from, when it isn't either.
  source?: string;
  // When it came (ISO); shown as "2h ago".
  at?: string;
  // Someone else waiting on it ("Sam asked").
  waitingOn?: string;
  // A word the card carries after its age ("In training").
  tag?: string;
  className?: string;
}) {
  const age = at ? formatRelativeTime(at) : "";
  if (!need && !mode && !source && !age && !waitingOn && !tag) return null;
  return (
    <div className={`flex flex-wrap items-center gap-2 ${className}`}>
      {need && (
        <span className="inline-flex items-center rounded-lg bg-surface-overlay px-2 py-0.5 text-[13px] font-medium text-fg-muted">
          {need}
        </span>
      )}
      {mode ? (
        <FeatureName feature={mode} className="text-[12px]" />
      ) : (
        source && (
          <span className="inline-flex items-center rounded-lg border border-line px-2 py-0.5 text-[13px] font-medium text-fg-muted">
            {source}
          </span>
        )
      )}
      {age && <span className="text-sm text-fg-subtle tabular-nums">{age}</span>}
      {tag && <Chip tone="amber">{tag}</Chip>}
      {waitingOn && <Chip tone="amber">{`${askerName(waitingOn)} asked`}</Chip>}
    </div>
  );
}
