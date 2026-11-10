"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import Button from "@/components/ui/Button";
import { answerRepeatOffer, type RepeatOffer } from "@/lib/api";
import { offerDescription, offerHeadline, offerQuestion } from "@/lib/repeatOffer";
import { stashWorkflowDescription } from "@/lib/workflowStarters";

/**
 * The offer to do an ask the person keeps making on a schedule
 * (workflows/repeat_asks.py): at the top of Workflows and under the chat
 * reply to the repeated ask. Set it up opens the build-by-talking flow with
 * the ask and the timing filled in; nothing runs until they approve the
 * steps there. Not now holds the same offer back for a good while.
 */
export default function RepeatOfferCard({
  offer,
  onDone,
  className = "",
}: {
  offer: RepeatOffer;
  /** Called once it is answered, so the caller can drop it. */
  onDone: (id: number) => void;
  className?: string;
}) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);

  const setUp = async () => {
    setBusy(true);
    await answerRepeatOffer(offer.id, true);
    const text = offerDescription(offer);
    onDone(offer.id);
    router.push(stashWorkflowDescription(text) ? "/jobs/new" : `/jobs/new?describe=${encodeURIComponent(text)}`);
  };

  const notNow = async () => {
    setBusy(true);
    await answerRepeatOffer(offer.id, false);
    onDone(offer.id);
  };

  return (
    <section
      aria-label="Do this on a schedule"
      className={`flex flex-col gap-3 rounded-2xl border border-accent/30 bg-accent/5 p-4 sm:flex-row sm:items-center sm:justify-between sm:p-5 ${className}`}
    >
      <div className="min-w-0">
        <p className="text-[15px] font-semibold text-fg">{offerHeadline(offer)}</p>
        <p className="mt-1 text-sm text-fg-muted">{offerQuestion(offer)}</p>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <Button variant="primary" disabled={busy} onClick={setUp}>
          Set it up
        </Button>
        <Button variant="secondary" disabled={busy} onClick={notNow}>
          Not now
        </Button>
      </div>
    </section>
  );
}
