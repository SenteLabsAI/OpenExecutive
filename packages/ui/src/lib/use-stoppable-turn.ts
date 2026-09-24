"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { stopChat } from "@/lib/api";
import { newClientTurnId } from "@/lib/turn-id";

// How long to wait for the server's `stopped` + `done` after it accepted the
// stop, before falling back to aborting the fetch outright.
//
// The normal path never reaches the abort: the server flips its switch, cancels
// the in-flight step and reports back over the still-open stream, which is what
// lets the client finish the turn the same way it finishes any other. The abort
// is only for when that never arrives — the backend restarted, or the stream is
// wedged. Keep this comfortably longer than the server's cancel-and-wind-down
// latency, or a healthy stop gets truncated into the abort path and loses the
// server's own accounting of the turn.
export const STOP_ABORT_GRACE_MS = 8000;

// A stop can beat its own turn's registration (the server registers as early as
// it can, but not before the request arrives). One retry covers that without
// aborting a turn that was about to stream normally.
export const STOP_RETRY_DELAY_MS = 400;

export interface StoppableTurn {
  /** True between the Stop click and the turn actually ending. */
  isStopping: boolean;
  /** Call at the start of a turn; returns the id and signal to send with it. */
  beginTurn: () => { clientTurnId: string; signal: AbortSignal };
  /** Wired to the Stop button. */
  stop: () => Promise<void>;
  /** Call when the server's `stopped` event arrives — it is winding the turn
   *  down itself, so the abort fallback must stand down. */
  serverAcknowledgedStop: () => void;
  /** Call in the turn's `finally` to clear per-turn state. */
  endTurn: () => void;
}

/** Stop-button plumbing shared by the main chat and the Ask OE panel.
 *
 * Both surfaces stream from the same `streamChat` helper and need the identical
 * turn-id / abort-controller / grace-timer dance. Keeping it in one place means
 * a fix to the race applies to both, rather than to whichever file someone
 * happened to be looking at.
 */
export function useStoppableTurn(): StoppableTurn {
  const [isStopping, setIsStopping] = useState(false);
  // Mirrors `isStopping` synchronously. The state value is captured at render
  // time, so two clicks in the same tick would both pass a state-based guard.
  const isStoppingRef = useRef(false);
  // Addresses the in-flight turn for `stopChat`. Minted before the request goes
  // out, so the button is live from the moment Send is pressed — the server
  // spends seconds fetching context before the stream exists, and a stop in
  // that window costs nothing because no model call has been made yet.
  const clientTurnIdRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const graceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(
    () => () => {
      if (graceTimerRef.current) clearTimeout(graceTimerRef.current);
    },
    [],
  );

  const beginTurn = useCallback(() => {
    const clientTurnId = newClientTurnId();
    clientTurnIdRef.current = clientTurnId;
    const controller = new AbortController();
    abortRef.current = controller;
    isStoppingRef.current = false;
    setIsStopping(false);
    return { clientTurnId, signal: controller.signal };
  }, []);

  const armAbortFallback = useCallback(() => {
    if (graceTimerRef.current) clearTimeout(graceTimerRef.current);
    graceTimerRef.current = setTimeout(
      () => abortRef.current?.abort(),
      STOP_ABORT_GRACE_MS,
    );
  }, []);

  const stop = useCallback(async () => {
    const clientTurnId = clientTurnIdRef.current;
    if (!clientTurnId || isStoppingRef.current) return;
    isStoppingRef.current = true;
    setIsStopping(true);

    const accepted = await stopChat(clientTurnId);
    if (!accepted) {
      // 404 means either "already finished" (the stream is about to end on its
      // own) or "not registered yet" (we beat the server to it). Retry once
      // before giving up — aborting immediately would truncate a turn that was
      // about to stream normally, and would leave the server still working.
      await new Promise((r) => setTimeout(r, STOP_RETRY_DELAY_MS));
      if (clientTurnIdRef.current === clientTurnId) {
        await stopChat(clientTurnId);
      }
    }
    // Always give the server room to wind down and send `stopped` + `done`;
    // never abort immediately. `serverAcknowledgedStop` cancels this.
    if (clientTurnIdRef.current === clientTurnId) armAbortFallback();
  }, [armAbortFallback]);

  const serverAcknowledgedStop = useCallback(() => {
    // The server is ending the turn itself and will close the stream with
    // `done`. Letting the fallback fire here would abort mid-wind-down and
    // cost us the server's own terminal events — which is how the client
    // learns the session id on a first turn.
    if (graceTimerRef.current) {
      clearTimeout(graceTimerRef.current);
      graceTimerRef.current = null;
    }
  }, []);

  const endTurn = useCallback(() => {
    if (graceTimerRef.current) {
      clearTimeout(graceTimerRef.current);
      graceTimerRef.current = null;
    }
    clientTurnIdRef.current = null;
    abortRef.current = null;
    isStoppingRef.current = false;
    setIsStopping(false);
  }, []);

  return { isStopping, beginTurn, stop, serverAcknowledgedStop, endTurn };
}

/** True for the AbortError our own grace-timer fallback raises.
 *
 * Matched by name rather than by `instanceof DOMException`: browsers raise a
 * DOMException, but Node/undici and jsdom can surface a plain Error with the
 * same name, and treating that as a crash would show "Something went wrong"
 * for a stop the user asked for.
 */
export function isAbortError(err: unknown): boolean {
  return (
    typeof err === "object" &&
    err !== null &&
    (err as { name?: unknown }).name === "AbortError"
  );
}
