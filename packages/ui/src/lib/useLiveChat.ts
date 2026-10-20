"use client";

import { useEffect, useRef } from "react";
import { followChat, type LiveChatEvent } from "@/lib/api";

const RETRY_MIN_MS = 500;
const RETRY_MAX_MS = 30_000;

/** Hold one stream to a shared chat (`followChat`) while the page shows it.
 *
 * It closes while the tab is hidden, so a chat left open in a background tab
 * holds no connection, and opens again when the tab is shown: the server's
 * first event (`ready`) makes the page read the chat again, which catches up
 * whatever it missed. A dropped stream reconnects, waiting longer after each
 * failure. `removed` and `deleted` end it for good; so does a 404, passed on
 * as `removed`. */
export function useLiveChat(
  sessionId: string | undefined,
  enabled: boolean,
  onEvent: (event: LiveChatEvent) => void,
): void {
  const handler = useRef(onEvent);
  useEffect(() => {
    handler.current = onEvent;
  });

  useEffect(() => {
    if (!sessionId || !enabled) return;
    let disposed = false;
    let over = false;
    let ctrl: AbortController | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let failures = 0;

    const later = (ms: number) => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => {
        timer = null;
        void connect();
      }, ms);
    };

    const connect = async () => {
      if (disposed || over || ctrl || document.hidden) return;
      const mine = new AbortController();
      ctrl = mine;
      try {
        for await (const event of followChat(sessionId, mine.signal)) {
          failures = 0;
          handler.current(event);
          if (event.type === "removed" || event.type === "deleted") over = true;
        }
      } catch (err) {
        if (mine.signal.aborted) return;
        if (err instanceof Error && err.message === "not_found") {
          over = true;
          handler.current({ type: "removed" });
        } else {
          failures += 1;
        }
      } finally {
        if (ctrl === mine) ctrl = null;
      }
      if (disposed || over || mine.signal.aborted) return;
      // Ended without a last word (a restart, the network, `resync`).
      later(failures ? Math.min(RETRY_MAX_MS, RETRY_MIN_MS * 2 ** failures) : RETRY_MIN_MS);
    };

    const onVisibility = () => {
      if (document.hidden) {
        if (timer) clearTimeout(timer);
        timer = null;
        ctrl?.abort();
        ctrl = null;
      } else {
        failures = 0;
        void connect();
      }
    };

    void connect();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      disposed = true;
      document.removeEventListener("visibilitychange", onVisibility);
      if (timer) clearTimeout(timer);
      ctrl?.abort();
    };
  }, [sessionId, enabled]);
}
