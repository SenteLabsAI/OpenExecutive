"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { CONFIRM_PROTOCOL, isConfirmMessage, type CardAction } from "@/lib/cardActions";

// In a frame, a card action or another change asks in a window of this
// app's own (app/confirm), not inside the frame: the page around the frame
// can cover the frame with anything, but not another window. This is what
// the frame shows meanwhile. The window is opened at once when the browser
// allows it (the click that asked counts); otherwise, or after it was closed,
// a button opens it. Closing it, or Cancel here, is No.

function makeNonce(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

export default function FrameConfirm({
  action,
  onAnswer,
}: {
  action: CardAction;
  onAnswer: (ok: boolean) => void;
}) {
  const [nonce] = useState(makeNonce);
  const popup = useRef<Window | null>(null);
  const [open, setOpen] = useState(false);
  const answered = useRef(false);

  const finish = useCallback(
    (ok: boolean) => {
      if (answered.current) return;
      answered.current = true;
      if (popup.current && !popup.current.closed) popup.current.close();
      onAnswer(ok);
    },
    [onAnswer],
  );

  const openWindow = useCallback(() => {
    const opened = window.open(`/confirm?nonce=${nonce}`, `oe-confirm-${nonce}`, "popup,width=460,height=420");
    popup.current = opened;
    return opened !== null;
  }, [nonce]);

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      const current = popup.current;
      if (!current || event.origin !== window.location.origin || event.source !== current) return;
      if (isConfirmMessage(event.data, "ready", nonce)) {
        current.postMessage({ protocol: CONFIRM_PROTOCOL, type: "ask", nonce, action }, window.location.origin);
      } else if (isConfirmMessage(event.data, "answer", nonce)) {
        finish(event.data.ok === true);
      }
    };
    window.addEventListener("message", onMessage);
    // Opened now, while the click that asked still counts as one.
    const opened = openWindow();
    const timer = window.setInterval(() => {
      const isOpen = popup.current !== null && !popup.current.closed;
      setOpen(isOpen);
    }, 300);
    if (!opened) window.setTimeout(() => setOpen(false), 0);
    return () => {
      window.removeEventListener("message", onMessage);
      window.clearInterval(timer);
    };
  }, [action, nonce, finish, openWindow]);

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 px-4">
      <div
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="frame-confirm-title"
        className="w-full max-w-md rounded-2xl border border-line bg-surface-elevated p-5 shadow-xl"
      >
        <h2 id="frame-confirm-title" className="text-base font-semibold text-fg">
          Confirm in the small window
        </h2>
        <p className="mt-2 text-sm text-fg">{action.what}</p>
        <p className="mt-2 text-xs text-fg-subtle">
          This page is shown inside another one, so you say yes in a window of its own, where nothing else can cover
          it.
        </p>
        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={() => finish(false)}
            className="rounded-lg border border-line px-3 py-1.5 text-sm font-medium hover:bg-surface-overlay"
          >
            Cancel
          </button>
          {!open && (
            <button
              type="button"
              onClick={() => setOpen(openWindow())}
              className="rounded-lg bg-accent-strong px-3 py-1.5 text-sm font-medium text-white"
            >
              Open the window
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
