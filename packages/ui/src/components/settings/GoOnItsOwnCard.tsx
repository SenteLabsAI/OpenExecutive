"use client";

import { useState } from "react";

import SettingsCard from "@/components/settings/SettingsCard";
import { answerApprovalsOffer, type ApprovalsOffer } from "@/lib/api";

// Delegate → Approvals, "Could go on its own": what you approved three
// times without changing it, As you and As me together. Let it go adds it
// to What it's learned, the same as ticking "on its own next time" on its
// card; Keep asking drops the offer until three more unchanged approvals.
// Careful, Your rules and In training still decide in the same order. The
// same offer shows on the card on Home (GoOnItsOwnOffer). Hidden when
// there are none.

export function offerDetail(offer: ApprovalsOffer): string {
  const when = offer.last_at
    ? ` · last on ${new Date(offer.last_at).toLocaleDateString(undefined, { month: "short", day: "numeric" })}`
    : "";
  // As you: the address too, so a display name alone never stands for who it is.
  const who = offer.side === "as_you" && offer.name ? ` · ${offer.address}` : "";
  return `${offer.side === "as_me" ? "As me" : "As you"}${who} · approved ${offer.approvals} times unchanged${when}`;
}

export function OfferButtons({
  offer,
  busy,
  onAnswer,
}: {
  offer: ApprovalsOffer;
  busy: boolean;
  onAnswer: (answer: "let-go" | "keep-asking") => void;
}) {
  return (
    <span className="flex flex-shrink-0 items-center gap-4">
      <button
        type="button"
        disabled={busy}
        onClick={() => onAnswer("let-go")}
        className="min-h-touch text-sm font-semibold text-accent hover:underline disabled:opacity-60"
        aria-label={`Let ${offer.label} go on ${offer.side === "as_you" ? "their" : "its"} own`}
      >
        Let it go
      </button>
      <button
        type="button"
        disabled={busy}
        onClick={() => onAnswer("keep-asking")}
        className="min-h-touch text-sm font-medium text-fg hover:underline disabled:opacity-60"
        aria-label={`Keep asking about ${offer.label}`}
      >
        Keep asking
      </button>
    </span>
  );
}

export function useOfferAnswer(): {
  busy: boolean;
  error: string | null;
  answer: (offer: ApprovalsOffer, answer: "let-go" | "keep-asking") => void;
} {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const answer = (offer: ApprovalsOffer, choice: "let-go" | "keep-asking") => {
    setBusy(true);
    setError(null);
    answerApprovalsOffer(offer.key, choice)
      .catch((err) => setError(err instanceof Error ? err.message : "Couldn't save that."))
      .finally(() => setBusy(false));
  };
  return { busy, error, answer };
}

export default function GoOnItsOwnCard({ offers }: { offers: ApprovalsOffer[] }) {
  const { busy, error, answer } = useOfferAnswer();
  if (offers.length === 0) return null;
  return (
    <SettingsCard
      title="Could go on its own"
      description="You approved these three times without changing them. The same offer shows on the card on Home."
    >
      <ul className="flex flex-col divide-y divide-line rounded-xl border border-line">
        {offers.map((offer) => (
          <li
            key={offer.key}
            className="flex flex-col gap-1 px-4 py-3 sm:flex-row sm:items-center sm:justify-between sm:gap-3"
          >
            <span className="min-w-0">
              <span className="block text-[15px] font-medium text-fg break-words">{offer.label}</span>
              <span className="mt-0.5 block text-[13px] leading-snug text-fg-muted">{offerDetail(offer)}</span>
            </span>
            <OfferButtons offer={offer} busy={busy} onAnswer={(a) => answer(offer, a)} />
          </li>
        ))}
      </ul>
      {error && <p className="mt-2 text-sm text-red-500">{error}</p>}
    </SettingsCard>
  );
}
