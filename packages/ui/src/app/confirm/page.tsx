"use client";

import { useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import Button, { buttonClass } from "@/components/ui/Button";
import { CONFIRM_PROTOCOL, describeDecision, isConfirmMessage, parseAction, type CardAction } from "@/lib/cardActions";

// The window a framed page opens to have a card action or another change
// confirmed (components/embed/FrameConfirm.tsx). It is a window of its own,
// so no page can lay anything over it. It takes the question only from the
// window that opened it, on this app's own origin, and answers only that
// window.

const YES: Record<CardAction["verb"], string> = {
  approve: "Yes, approve",
  reject: "Yes, dismiss",
  cancel: "Yes, cancel it",
  change: "Yes, make the change",
};

export default function ConfirmPage() {
  const nonce = useSearchParams()?.get("nonce") ?? "";
  const [action, setAction] = useState<CardAction | null>(null);
  const [card, setCard] = useState<string | null>(null);

  useEffect(() => {
    const opener = window.opener as Window | null;
    if (!opener || !/^[0-9a-f]{32}$/.test(nonce)) return;
    const onMessage = (event: MessageEvent) => {
      if (event.origin !== window.location.origin || event.source !== opener) return;
      if (!isConfirmMessage(event.data, "ask", nonce)) return;
      const asked = parseAction(event.data.action);
      if (asked) setAction(asked);
    };
    window.addEventListener("message", onMessage);
    opener.postMessage({ protocol: CONFIRM_PROTOCOL, type: "ready", nonce }, window.location.origin);
    return () => window.removeEventListener("message", onMessage);
  }, [nonce]);

  const decisionId = action?.decisionId ?? null;
  useEffect(() => {
    if (decisionId === null) return;
    const controller = new AbortController();
    fetch(`/api/backend/decisions/${decisionId}`, { signal: controller.signal })
      .then((res) => (res.ok ? res.json() : null))
      .then((instance) => {
        if (instance) setCard(describeDecision(instance));
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, [decisionId]);

  const answer = (ok: boolean) => {
    const opener = window.opener as Window | null;
    opener?.postMessage({ protocol: CONFIRM_PROTOCOL, type: "answer", nonce, ok }, window.location.origin);
    window.close();
  };

  return (
    <main className="min-h-screen bg-surface px-5 py-6 text-fg">
      {action ? (
        <div className="mx-auto max-w-md">
          <h1 className="text-base font-semibold">Are you sure?</h1>
          <p className="mt-2 text-sm">{action.what}</p>
          {card && <p className="mt-2 text-sm text-fg-muted">The card: {card}</p>}
          <div className="mt-5 flex justify-end gap-2">
            <button type="button" className={buttonClass("secondary")} onClick={() => answer(false)}>
              No
            </button>
            <Button
              variant={action.verb === "reject" || action.verb === "cancel" ? "danger" : "primary"}
              onClick={() => answer(true)}
            >
              {YES[action.verb]}
            </Button>
          </div>
        </div>
      ) : (
        <p className="text-sm text-fg-muted">Nothing to confirm here. You can close this window.</p>
      )}
    </main>
  );
}
