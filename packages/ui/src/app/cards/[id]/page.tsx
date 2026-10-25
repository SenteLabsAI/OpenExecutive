"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";

import { ChatActionCards } from "@/components/ActionCards";
import { ReplyCardItem, useReplyCards } from "@/components/RepliesWaiting";
import { usePageCrumb } from "@/components/shell/PageCrumb";
import { getDecision, type DecisionInstance } from "@/lib/api";

// One card on a page of its own, by its decision id: what a host's frame
// opens to show a card (in a frame it can't be approved, lib/cardActions.ts:
// the person opens the page in Open Executive itself), and a link anyone can
// keep. It shows the card with
// the same component Home and the chat use; the server answers only the
// card's own person, so anyone else sees that it isn't there.
export default function CardPage() {
  const params = useParams<{ id: string }>();
  const id = /^\d+$/.test(params?.id ?? "") ? Number(params.id) : null;
  usePageCrumb(id !== null ? `Card ${id}` : "Card");
  const [instance, setInstance] = useState<DecisionInstance | null | undefined>(undefined);
  const replies = useReplyCards();
  const reply = replies.cards.find((c) => c.decision_id === id);

  useEffect(() => {
    if (id === null) return;
    const controller = new AbortController();
    getDecision(id, controller.signal)
      .then(setInstance)
      .catch(() => setInstance(null));
    return () => controller.abort();
  }, [id]);

  const waiting = instance?.status === "proposed" || instance?.status === "executing";
  return (
    <main className="mx-auto w-full max-w-2xl px-4 sm:px-6 py-6 space-y-4">
      {id === null || instance === null ? (
        <p className="text-sm text-fg-muted">This card isn&rsquo;t here: it may be someone else&rsquo;s, or gone.</p>
      ) : instance === undefined ? (
        <p className="text-sm text-fg-muted">Loading the card…</p>
      ) : !waiting ? (
        <p className="text-sm text-fg-muted">
          This card is no longer waiting ({instance.status.replace(/_/g, " ")}).
        </p>
      ) : reply ? (
        <ReplyCardItem
          card={reply}
          onGone={replies.gone}
          onRefresh={replies.refresh}
          onChanged={replies.replace}
          emphasized
        />
      ) : (
        <ChatActionCards ids={[id]} />
      )}
      {replies.notice && <p className="text-sm text-fg-muted">{replies.notice}</p>}
      <p className="text-sm">
        <Link href="/" className="text-accent hover:underline">
          Everything waiting for you
        </Link>
      </p>
    </main>
  );
}
