"use client";

import { createContext, useContext, useEffect, useRef, useState } from "react";

import FrameConfirm from "@/components/embed/FrameConfirm";
import { CardActionNotConfirmed, frameActionFor, type CardAction } from "@/lib/cardActions";
import HOST_LIST from "@/lib/host-list.json";
import {
  acceptsEvent,
  embedRequested,
  parseHostMessage,
  readyMessage,
  themeProperties,
  type EmbedInit,
  type EmbedTheme,
} from "@/lib/embed";

// Embedded mode (lib/embed.ts): the page is in a host origin's frame, asked
// for with ?embed=1, and the host answered `ready` with a valid `init`. Until
// then, and in every other case, the page is exactly what it is outside a
// frame. AppShell reads `embedded` to leave out its own chrome.
//
// Apart from that, in ANY frame (embedded or not, since a page on a host
// origin can frame these pages without saying so), a card action, and any
// other change off the host list that follows a click, waits for a Yes in a
// window of this app's own that no page can cover (lib/cardActions.ts,
// FrameConfirm, app/confirm).

const HOST_CALLS = HOST_LIST.calls.map(([method, pattern]) => [method, pattern] as const);
// A change made this soon after a click or a key in the frame may have been
// lured, so it asks; later than that, the page made it on its own.
const AFTER_INPUT_MS = 10_000;
// What counts as input: a click, a key, a drop or a paste (a page over the
// frame can lure any of them, and put its own file in a drag).
const INPUT_EVENTS = ["pointerdown", "keydown", "drop", "paste"] as const;

export type EmbedState = {
  embedded: boolean;
  init: EmbedInit | null;
  // The host origin that sent `init`; every later message must come from it.
  hostOrigin: string | null;
};

const NOT_EMBEDDED: EmbedState = { embedded: false, init: null, hostOrigin: null };
const EmbedContext = createContext<EmbedState>(NOT_EMBEDDED);

export function useEmbed(): EmbedState {
  return useContext(EmbedContext);
}

function applyTheme(theme: EmbedTheme, font: string | null): void {
  const root = document.documentElement;
  root.dataset.theme = theme.mode;
  root.style.colorScheme = theme.mode;
  for (const [name, value] of Object.entries(themeProperties(theme))) root.style.setProperty(name, value);
  if (font) {
    root.style.setProperty("--font-sans", font);
    root.style.setProperty("--font-display", font);
  }
}

type Pending = { action: CardAction; resolve: (ok: boolean) => void };

export default function EmbedProvider({
  hostOrigins,
  version,
  children,
}: {
  hostOrigins: string[];
  version: string;
  children: React.ReactNode;
}) {
  const [state, setState] = useState<EmbedState>(NOT_EMBEDDED);
  const [pending, setPending] = useState<Pending | null>(null);
  const pendingRef = useRef<Pending | null>(null);
  const lastInput = useRef(0);

  // The handshake: `ready` to the parent, addressed to each listed host
  // origin (the browser delivers it only to the one the parent is), then
  // `init` and `theme` from that parent and origin alone.
  useEffect(() => {
    if (window.parent === window || hostOrigins.length === 0) return;
    if (!embedRequested(window.location.search)) return;
    const parent = window.parent;
    let lockedOrigin: string | null = null;
    let font: string | null = null;
    const onMessage = (event: MessageEvent) => {
      if (!acceptsEvent(event, lockedOrigin ? [lockedOrigin] : hostOrigins, parent)) return;
      const message = parseHostMessage(event.data);
      if (!message) return;
      if (message.type === "init") {
        lockedOrigin = event.origin;
        font = message.init.font;
        applyTheme(message.init.theme, font);
        setState({ embedded: true, init: message.init, hostOrigin: event.origin });
      } else if (lockedOrigin) {
        applyTheme(message.theme, font);
        setState((prev) => (prev.init ? { ...prev, init: { ...prev.init, theme: message.theme } } : prev));
      }
    };
    window.addEventListener("message", onMessage);
    for (const origin of hostOrigins) parent.postMessage(readyMessage(version), origin);
    return () => window.removeEventListener("message", onMessage);
  }, [hostOrigins, version]);

  // In any frame: every card action, and any change off the host list that
  // follows a click, waits for the confirmation window.
  useEffect(() => {
    if (window.top === window.self) return;
    const onInput = (event: Event) => {
      if (event.isTrusted) lastInput.current = Date.now();
    };
    for (const type of INPUT_EVENTS) window.addEventListener(type, onInput, true);
    const original = window.fetch;
    const confirm = (action: CardAction) =>
      new Promise<boolean>((resolve) => {
        // One at a time: a second action while one is asked about is refused.
        if (pendingRef.current) return resolve(false);
        const next = { action, resolve };
        pendingRef.current = next;
        setPending(next);
      });
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const method = init?.method ?? (input instanceof Request ? input.method : "GET");
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      const action = frameActionFor(method, url, init?.body, HOST_CALLS);
      const asks = action !== null && (action.card || Date.now() - lastInput.current < AFTER_INPUT_MS);
      if (asks && !(await confirm(action))) throw new CardActionNotConfirmed();
      return original(input, init);
    };
    return () => {
      window.fetch = original;
      for (const type of INPUT_EVENTS) window.removeEventListener(type, onInput, true);
    };
  }, []);

  const answer = (ok: boolean) => {
    const current = pendingRef.current;
    pendingRef.current = null;
    // A Yes is input too: what the confirmed change sets off next asks again.
    lastInput.current = Date.now();
    setPending(null);
    current?.resolve(ok);
  };

  return (
    <EmbedContext.Provider value={state}>
      {children}
      {pending && (
        <FrameConfirm action={pending.action} onAnswer={answer} />
      )}
    </EmbedContext.Provider>
  );
}
