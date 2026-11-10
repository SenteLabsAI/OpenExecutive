"use client";

import { OfferButtons, useOfferAnswer } from "@/components/settings/GoOnItsOwnCard";
import { useApprovalsSummary } from "@/components/settings/useApprovalsSummary";
import type { ApprovalsOffer } from "@/lib/api";

// Under a card's buttons on Home: the offer from Delegate → Approvals
// (Could go on its own) for the next thing of its kind. A reply to someone
// you sent unchanged three times, or something the Executive did that you
// approved three times unchanged. Let it go and Keep asking do what they do
// there, and answering in one place clears the other.

export function offerFor(
  offers: ApprovalsOffer[],
  want: { addresses?: string[]; leadKey?: string | null },
): ApprovalsOffer | null {
  const addresses = new Set((want.addresses ?? []).map((a) => a.trim().toLowerCase()).filter(Boolean));
  return (
    offers.find((o) =>
      o.side === "as_you" ? addresses.has(o.address.toLowerCase()) : Boolean(want.leadKey) && o.key === want.leadKey,
    ) ?? null
  );
}

export default function GoOnItsOwnOffer({
  addresses,
  leadKey,
  className = "mt-4",
}: {
  // A reply card: everyone it goes to.
  addresses?: string[];
  // An As me card: what "on its own next time" would allow.
  leadKey?: string | null;
  className?: string;
}) {
  const summary = useApprovalsSummary();
  const { busy, error, answer } = useOfferAnswer();
  const offer = summary ? offerFor(summary.offers, { addresses, leadKey }) : null;
  if (!offer) return null;
  const who = offer.name || offer.address;
  const [line, ask] =
    offer.side === "as_you"
      ? [
          `You sent the last ${offer.approvals} replies to ${who} unchanged.`,
          `Let replies to ${who} go on their own?`,
        ]
      : [`You approved this ${offer.approvals} times unchanged.`, `Let it do this on its own: ${offer.label}?`];
  return (
    <div className={`${className} rounded-xl bg-accent/10 px-4 py-3`}>
      <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between sm:gap-3">
        <div className="min-w-0">
          <p className="text-[15px] text-fg break-words">{line}</p>
          <p className="mt-0.5 text-sm text-fg-muted break-words">{ask}</p>
        </div>
        <OfferButtons offer={offer} busy={busy} onAnswer={(a) => answer(offer, a)} />
      </div>
      {error && <p className="mt-2 text-sm text-red-500">{error}</p>}
    </div>
  );
}
