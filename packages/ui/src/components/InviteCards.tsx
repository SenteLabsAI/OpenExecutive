"use client";

import { useCallback, useEffect, useState } from "react";

import Button from "@/components/ui/Button";
import FeatureName from "@/components/FeatureName";
import NextTimeTick from "@/components/NextTimeTick";
import { answerInviteCard, dismissInviteCard, getInviteCards, type InviteCard } from "@/lib/api";
import { answeredLine, clashLine, inviteNextTimeLabel } from "@/lib/inviteCards";
import { relationLabel } from "@/lib/replyCards";
import { formatRelativeTime } from "@/lib/relativeTime";

// Home: meeting invites from people you know (Act as me, with Read my
// calendar on), shown as cards in "Needs you" next to the reply cards: who
// invited you, the meeting, when, and any clash with your calendar (times
// only). Accept and Decline answer it in your own calendar; Dismiss leaves it
// unanswered. GET /delegation/invites answers only the card's own person, so
// nothing shows for anyone else, on a backend without it, and when nothing is
// waiting.

export function useInviteCards() {
  const [cards, setCards] = useState<InviteCard[] | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getInviteCards(controller.signal)
      .then(setCards)
      .catch(() => setCards([])); // no card is better than a broken one
    return () => controller.abort();
  }, []);

  const gone = useCallback((decisionId: number) => {
    setCards((prev) => (prev ?? []).filter((c) => c.decision_id !== decisionId));
  }, []);

  return { cards: cards ?? [], loaded: cards !== null, gone };
}

// What the invite cards are, for the Needs you header's tip.
export const INVITE_CARDS_TIP =
  "Meeting invites from people you know. Accept or Decline answers it in your own calendar, " +
  "and your calendar lets them know. Only the busy times on your calendar are checked for clashes. " +
  "Only you see these.";

export function InviteCardItem({
  card,
  onGone,
  emphasized = false,
}: {
  card: InviteCard;
  emphasized?: boolean;
  onGone: (id: number) => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<"accept" | "decline" | null>(null);
  const [nextTime, setNextTime] = useState(false);
  const created = formatRelativeTime(card.created_at);
  const relation = relationLabel(card.relation);
  const clash = clashLine(card);

  const answer = async (choice: "accept" | "decline") => {
    setBusy(choice === "accept" ? "Accepting…" : "Declining…");
    setError(null);
    try {
      await answerInviteCard(card.decision_id, choice, choice === "accept" && card.can_allow && nextTime);
      setDone(choice);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't answer that invite.");
    } finally {
      setBusy(null);
    }
  };

  const dismiss = async () => {
    setBusy("Dismissing…");
    setError(null);
    try {
      await dismissInviteCard(card.decision_id);
      onGone(card.decision_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't dismiss that invite.");
      setBusy(null);
    }
  };

  return (
    <article
      className={`rounded-2xl border bg-surface-elevated p-4 sm:p-5 ${
        emphasized ? "border-accent/60 ring-1 ring-accent/25 shadow-sm" : "border-line"
      }`}
    >
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <span className="inline-flex items-center rounded-lg bg-surface-overlay px-2 py-0.5 text-[13px] font-medium text-fg-muted">
          Invite
        </span>
        <FeatureName feature="act_as_me" className="text-[12px]" />
        {created && <span className="text-sm text-fg-subtle tabular-nums">{created}</span>}
      </div>
      <div className="text-base sm:text-[17px] font-semibold leading-snug text-fg break-words">
        {card.title || "A meeting"}
      </div>
      <p className="mt-1 text-sm text-fg break-words">
        {card.when}
        <span className="text-fg-muted">
          {" · from "}
          {card.from_name || card.from_email}
          {relation ? ` (${relation})` : ""}
        </span>
      </p>
      {clash && (
        <p className={`mt-2 text-sm ${card.clash_checked ? "text-amber-700 dark:text-amber-300" : "text-fg-muted"}`}>
          {clash}
        </p>
      )}
      {card.waited_because && !done && <p className="mt-2 text-sm text-fg-muted">{card.waited_because}</p>}

      {done ? (
        <>
          <p className="mt-3 text-sm text-emerald-700 dark:text-emerald-300">{answeredLine(done, card)}</p>
          {done === "accept" && card.can_allow && nextTime && (
            <p className="mt-1 text-sm text-fg-muted">From now on, their invites with no clash are accepted on their own.</p>
          )}
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <Button variant="secondary" onClick={() => onGone(card.decision_id)}>
              Close
            </Button>
          </div>
        </>
      ) : card.can_answer ? (
        <>
          {card.can_allow && (
            <NextTimeTick
              className="mt-4"
              checked={nextTime}
              disabled={busy !== null}
              onChange={setNextTime}
              label={inviteNextTimeLabel(card)}
              help="Only when nothing on your calendar clashes. You can undo this in Handle it for me."
            />
          )}
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <Button variant="primary" onClick={() => void answer("accept")} disabled={busy !== null}>
              {busy === "Accepting…" ? busy : "Accept"}
            </Button>
            <Button variant="secondary" onClick={() => void answer("decline")} disabled={busy !== null}>
              {busy === "Declining…" ? busy : "Decline"}
            </Button>
            <Button variant="ghost" className="ml-auto" onClick={() => void dismiss()} disabled={busy !== null}>
              Dismiss
            </Button>
          </div>
        </>
      ) : (
        <>
          <p className="mt-3 text-sm text-fg-muted">
            Your calendar sign-in can read your calendar but not answer invites. Connect it again under Read my
            calendar in Delegate → Act as me, then answer here. Until then, answer it in your calendar.
          </p>
          {card.connect_command && (
            <code className="mt-2 block overflow-x-auto rounded-lg bg-surface px-2 py-1 text-[13px] text-fg-muted">
              {card.connect_command}
            </code>
          )}
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <Button variant="ghost" className="ml-auto" onClick={() => void dismiss()} disabled={busy !== null}>
              Dismiss
            </Button>
          </div>
        </>
      )}
      {error && <p className="mt-2 text-sm text-red-500">{error}</p>}
    </article>
  );
}
