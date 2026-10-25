"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { buttonClass } from "@/components/ui/Button";
import { ownPageFor, type CardAction } from "@/lib/cardActions";
import { SeenGate, tracksVisibility, watchPanel, type Watch } from "@/lib/seen";

// In a frame, a card action, a chat message or another change asks here,
// inside the frame, naming what will happen. The page around the frame
// could hide, cover or move it and lure clicks, so its Yes counts only under
// lib/seen.ts's rule: seen, pointed at for a moment, pressed and clicked for
// real, and still seen a moment later. A browser that can't report that
// offers no Yes: the person opens this app's own page for it, top level in
// the same tab, as can anyone who can't point (a keyboard, a touch screen).
// The panel keeps this app's own colours and face (`.consent`, globals.css),
// whatever the host's theme. Cancel is No.

const YES: Record<CardAction["verb"], string> = {
  approve: "Yes, approve",
  reject: "Yes, dismiss",
  cancel: "Yes, cancel it",
  change: "Yes, go ahead",
};

export default function FrameConfirm({
  action,
  onAnswer,
}: {
  action: CardAction;
  onAnswer: (ok: boolean) => void;
}) {
  const panel = useRef<HTMLDivElement | null>(null);
  const yes = useRef<HTMLButtonElement | null>(null);
  const gate = useRef(new SeenGate());
  const watch = useRef<Watch | null>(null);
  const answered = useRef(false);
  // Shown only in the browser (EmbedProvider asks from a request), so
  // `window` is there. False where the browser can't tell it is seen.
  const [supported] = useState(() => tracksVisibility(window));
  const [ownPage] = useState(() => ownPageFor(action, window.location.pathname, window.location.search));
  const [ready, setReady] = useState(false);

  const finish = useCallback(
    (ok: boolean) => {
      if (answered.current) return;
      answered.current = true;
      onAnswer(ok);
    },
    [onAnswer],
  );

  useEffect(() => {
    const check = () => setReady(watch.current?.armed() ?? false);
    const watching = panel.current
      ? watchPanel(window, panel.current, (target) => target !== null && target === yes.current, gate.current, check)
      : null;
    if (!watching) return;
    watch.current = watching;
    // The runs of "seen" and "inside" grow without an event, so look again meanwhile.
    const timer = window.setInterval(check, 100);
    return () => {
      window.clearInterval(timer);
      watching.stop();
      watch.current = null;
    };
  }, []);

  const onYes = async (event: React.MouseEvent<HTMLButtonElement>) => {
    const judged = watch.current?.judge({ isTrusted: event.nativeEvent.isTrusted, target: event.target });
    if (judged && (await judged)) finish(true);
  };

  const danger = action.verb === "reject" || action.verb === "cancel";

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 px-4">
      <div
        ref={panel}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="frame-confirm-title"
        className="consent w-full max-w-md rounded-2xl border border-line bg-surface-elevated p-5 shadow-xl"
      >
        <h2 id="frame-confirm-title" className="text-base font-semibold text-fg">
          Are you sure?
        </h2>
        <p className="mt-2 text-sm text-fg">{action.what}</p>
        {!supported && (
          <p className="mt-2 text-xs text-fg-subtle">
            This page is shown inside another one, and this browser can&rsquo;t tell whether this question is really
            on screen, so answer it on Open Executive&rsquo;s own page. Your browser&rsquo;s Back brings you here again.
          </p>
        )}
        <div className="mt-5 flex justify-end gap-2">
          <button type="button" onClick={() => finish(false)} className={buttonClass("secondary", "sm")}>
            Cancel
          </button>
          {!supported ? (
            <a href={ownPage} target="_top" rel="noopener" className={buttonClass("primary", "sm")}>
              Open it here
            </a>
          ) : (
            <button
              ref={yes}
              type="button"
              onClick={(event) => void onYes(event)}
              disabled={!ready}
              className={buttonClass(danger ? "danger" : "primary", "sm")}
            >
              {YES[action.verb]}
            </button>
          )}
        </div>
        {supported && (
          <p className="mt-2 text-right text-xs text-fg-subtle">
            {!ready && "Hold the pointer on this box to answer. "}
            <a href={ownPage} target="_top" rel="noopener" className="text-accent hover:underline">
              Answer on its own page
            </a>
          </p>
        )}
      </div>
    </div>
  );
}
