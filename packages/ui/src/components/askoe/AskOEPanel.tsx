"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import Icon from "@/components/Icon";
import Message from "@/components/Message";
import { useAskOE } from "@/components/askoe/AskOEContext";
import type { ActionTaken, FormPatch } from "@/lib/api";
import { setMessageFeedback, streamChat } from "@/lib/api";
import { isAbortError, useStoppableTurn } from "@/lib/use-stoppable-turn";

// One proposal card per form_patch event: what was applied/skipped, the
// Executive's rationale, and an Undo that restores the pre-patch values.
interface PatchCardData {
  patch: FormPatch;
  applied: string[];
  skipped: string[];
  /** True when no matching form was on screen at delivery time. */
  stale: boolean;
  undone: boolean;
  undo?: () => void;
}

interface PanelMessage {
  role: "user" | "assistant";
  content: string;
  actions?: ActionTaken[];
  patches?: PatchCardData[];
  /** The user stopped this reply mid-stream. */
  stopped?: boolean;
  /** Persisted row id (from `done`), needed to rate the reply. */
  messageId?: number;
  feedback?: "up" | "down" | null;
}

function PatchCard({
  card,
  onUndo,
}: {
  card: PatchCardData;
  onUndo: () => void;
}) {
  if (card.stale) {
    return (
      <div className="mt-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
        <p className="font-medium mb-1">
          Suggested values arrived, but the form is no longer on screen.
        </p>
        <pre className="whitespace-pre-wrap break-all text-[11px] text-amber-200/80">
          {JSON.stringify(card.patch.fields, null, 2)}
        </pre>
      </div>
    );
  }
  return (
    <div className="mt-2 rounded-lg border border-indigo-500/30 bg-indigo-500/10 px-3 py-2 text-xs text-indigo-200">
      <div className="flex items-start justify-between gap-2">
        <p className="font-medium">
          {card.undone
            ? "Suggestions undone."
            : `Filled ${card.applied.length} field${card.applied.length === 1 ? "" : "s"} in the form — review and save.`}
        </p>
        {!card.undone && card.applied.length > 0 && (
          <button
            type="button"
            onClick={onUndo}
            className="flex-shrink-0 text-[11px] text-indigo-300 hover:text-indigo-100 underline"
          >
            Undo
          </button>
        )}
      </div>
      {card.applied.length > 0 && !card.undone && (
        <p className="mt-1 text-indigo-200/80">{card.applied.join(", ")}</p>
      )}
      {card.skipped.length > 0 && (
        <p className="mt-1 text-amber-200/80">
          Skipped (not recognized): {card.skipped.join(", ")}
        </p>
      )}
      {card.patch.rationale && (
        <p className="mt-1 text-indigo-200/60 italic">{card.patch.rationale}</p>
      )}
    </div>
  );
}

export default function AskOEPanel() {
  const ctx = useAskOE();
  const [messages, setMessages] = useState<PanelMessage[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [streamingContent, setStreamingContent] = useState("");
  const [activityLabel, setActivityLabel] = useState<string | null>(null);
  const [sessionId, setSessionId] = useState<string | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  // Synchronous in-flight guard: `streaming` state updates async, so two
  // rapid Enter presses could both pass the state check before re-render.
  const sendingRef = useRef(false);
  const { isStopping, beginTurn, stop, serverAcknowledgedStop, endTurn } =
    useStoppableTurn();

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages, streamingContent]);

  useEffect(() => {
    if (ctx.open) inputRef.current?.focus();
  }, [ctx.open]);

  const handlePatch = useCallback(
    (item: FormPatch): PatchCardData => {
      const form = ctx.getForm();
      if (form && form.formId === item.form_id) {
        const result = form.applyPatch(item.fields);
        ctx.markSuggested(result.applied);
        return {
          patch: item,
          applied: result.applied,
          skipped: result.skipped,
          stale: false,
          undone: false,
          undo: result.undo,
        };
      }
      return {
        patch: item,
        applied: [],
        skipped: Object.keys(item.fields),
        stale: true,
        undone: false,
      };
    },
    [ctx]
  );

  const send = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || streaming || sendingRef.current) return;
      sendingRef.current = true;
      setError(null);
      setInput("");
      setMessages((prev) => [...prev, { role: "user", content: trimmed }]);
      setStreaming(true);
      setStreamingContent("");
      setActivityLabel(null);
      const { clientTurnId, signal } = beginTurn();

      let content = "";
      let wasStopped = false;
      let messageId: number | undefined;
      const actions: ActionTaken[] = [];
      const patches: PatchCardData[] = [];
      try {
        // Page context is snapshotted per turn — current route, form
        // descriptor, and live field values at the moment of sending.
        const pageContext = ctx.buildPageContext();
        for await (const item of streamChat(trimmed, sessionId, {
          pageContext,
          clientTurnId,
          signal,
        })) {
          if (item.type === "chunk" && item.content) {
            content += item.content;
            setActivityLabel(null);
            setStreamingContent(content);
          } else if (item.type === "activity") {
            setActivityLabel(item.label);
          } else if (item.type === "form_patch") {
            patches.push(handlePatch(item));
          } else if (item.type === "action_taken") {
            actions.push(item);
          } else if (item.type === "error") {
            setError(item.message ?? "Something went wrong.");
          } else if (item.type === "stopped") {
            // The server is winding the turn down itself and will close with
            // `done`; stand the abort fallback down so it can.
            wasStopped = true;
            serverAcknowledgedStop();
            if (item.session_id) setSessionId(item.session_id);
          } else if (
            (item.type === "done" || item.type === "chunk") &&
            item.session_id
          ) {
            // Adopt the id from any event that carries one, not only `done` —
            // a turn that ends without `done` would otherwise leave the panel
            // pointing at no session.
            setSessionId(item.session_id);
            if (item.type === "done" && item.message_id) messageId = item.message_id;
          }
          // thinking / phase / debug_event: no panel surface needed.
          // `activity` is surfaced — it fills the streaming placeholder.
        }
      } catch (e) {
        if (isAbortError(e)) {
          // Our own safety-net abort. A stop is not an error.
          wasStopped = true;
        } else {
          setError(e instanceof Error ? e.message : "Request failed.");
        }
      } finally {
        // Don't append an empty assistant turn when the request failed
        // before producing anything — the error box is the only signal.
        if (content || actions.length > 0 || patches.length > 0) {
          setMessages((prev) => [
            ...prev,
            {
              role: "assistant",
              content,
              actions: actions.length ? actions : undefined,
              patches: patches.length ? patches : undefined,
              stopped: wasStopped || undefined,
              messageId,
            },
          ]);
        }
        endTurn();
        setStreamingContent("");
        setActivityLabel(null);
        setStreaming(false);
        sendingRef.current = false;
      }
    },
    [beginTurn, ctx, endTurn, handlePatch, serverAcknowledgedStop, sessionId, streaming]
  );

  const rate = useCallback(
    (msgIdx: number, value: "up" | "down" | null) => {
      const target = messages[msgIdx];
      if (!sessionId || !target?.messageId) return;
      const previous = target.feedback ?? null;
      const apply = (v: "up" | "down" | null) =>
        setMessages((prev) =>
          prev.map((m, i) => (i === msgIdx ? { ...m, feedback: v } : m))
        );
      apply(value);
      setMessageFeedback(sessionId, target.messageId, value).catch(() => {
        apply(previous);
        setError("Couldn't save your rating.");
      });
    },
    [messages, sessionId]
  );

  const undoPatch = useCallback(
    (msgIdx: number, patchIdx: number) => {
      // Side effects (restoring form values, clearing highlights) run here,
      // outside the state updater, so React StrictMode's double-invoke of
      // updaters can't undo twice.
      const card = messages[msgIdx]?.patches?.[patchIdx];
      if (!card || card.undone) return;
      card.undo?.();
      ctx.clearSuggested();
      setMessages((prev) =>
        prev.map((m, i) =>
          i === msgIdx && m.patches
            ? {
                ...m,
                patches: m.patches.map((p, j) =>
                  j === patchIdx ? { ...p, undone: true } : p
                ),
              }
            : m
        )
      );
    },
    [ctx, messages]
  );

  const newChat = useCallback(() => {
    setMessages([]);
    setSessionId(undefined);
    setError(null);
    setStreamingContent("");
    setActivityLabel(null);
  }, []);

  if (!ctx.open) return null;

  const emptyPrompts = [
    "What does this page do?",
    ...(ctx.formMeta ? ["Fill this form in for me: "] : []),
  ];

  return (
    <>
      {/* Mobile backdrop — the panel is a right sheet below lg. */}
      <div
        className="fixed top-8 bottom-0 left-0 right-0 bg-black/50 z-30 lg:hidden"
        onClick={() => ctx.setOpen(false)}
        aria-hidden="true"
      />
      <aside
        aria-label="Ask OE"
        className="fixed top-8 bottom-0 right-0 z-40 w-[min(24rem,100vw)] lg:static lg:z-auto lg:w-[380px] flex-shrink-0 border-l border-line bg-surface-elevated flex flex-col"
      >
        <div className="h-14 px-4 border-b border-line flex items-center justify-between flex-shrink-0">
          <div className="flex items-center gap-2 min-w-0">
            <Icon name="bolt" size="w-4 h-4" className="text-indigo-300 flex-shrink-0" />
            <span className="text-sm font-semibold text-fg truncate">Ask OE</span>
          </div>
          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={newChat}
              title="New conversation"
              aria-label="New conversation"
              className="min-h-touch min-w-touch flex items-center justify-center text-fg-muted hover:text-fg rounded-lg hover:bg-surface-overlay transition-colors"
            >
              <Icon name="plus" size="w-4 h-4" />
            </button>
            <button
              type="button"
              onClick={() => ctx.setOpen(false)}
              title="Close (Ctrl/Cmd + .)"
              aria-label="Close Ask OE"
              className="min-h-touch min-w-touch flex items-center justify-center text-fg-muted hover:text-fg rounded-lg hover:bg-surface-overlay transition-colors"
            >
              <Icon name="close" size="w-4 h-4" />
            </button>
          </div>
        </div>

        <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-4">
          {messages.length === 0 && !streaming && (
            <div className="text-sm text-fg-muted space-y-3">
              <p>
                Ask about this page
                {ctx.formMeta ? " — or describe what you want and I'll fill the form for you to review" : ""}
                .
              </p>
              <div className="flex flex-col gap-1.5">
                {emptyPrompts.map((p) => (
                  <button
                    key={p}
                    type="button"
                    onClick={() => {
                      setInput(p);
                      inputRef.current?.focus();
                    }}
                    className="text-left text-xs px-3 py-2 rounded-lg border border-line text-fg-muted hover:text-fg hover:bg-surface-overlay transition-colors"
                  >
                    {p.trim()}
                  </button>
                ))}
              </div>
            </div>
          )}

          {messages.map((m, i) => (
            <div key={i}>
              <Message
                role={m.role}
                content={m.content}
                actions={m.actions}
                stopped={m.stopped}
                feedback={m.feedback}
                onFeedback={
                  m.role === "assistant" && m.messageId && sessionId
                    ? (v) => rate(i, v)
                    : undefined
                }
              />
              {m.patches?.map((card, j) => (
                <PatchCard key={j} card={card} onUndo={() => undoPatch(i, j)} />
              ))}
            </div>
          ))}

          {streaming && (
            <Message
              role="assistant"
              content={streamingContent || activityLabel || "…"}
              isStreaming
            />
          )}

          {error && (
            <div className="mt-2 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
              {error}
            </div>
          )}
        </div>

        <div className="border-t border-line p-3 flex-shrink-0">
          <div className="flex items-end gap-2">
            <textarea
              ref={inputRef}
              rows={2}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  void send(input);
                }
              }}
              placeholder={
                ctx.formMeta
                  ? "Describe what you want — I'll fill the form…"
                  : "Ask about this page…"
              }
              className="flex-1 px-3 py-2 text-sm rounded-lg bg-surface-input border border-line text-fg placeholder:text-fg-subtle resize-none focus:outline-none focus:border-indigo-500"
            />
            {streaming ? (
              <button
                type="button"
                disabled={isStopping}
                onClick={() => void stop()}
                aria-label="Stop the executive"
                title="Stop — whatever has been written so far is kept"
                className="min-h-touch min-w-touch flex items-center justify-center rounded-lg bg-surface-overlay border border-line-strong text-fg hover:border-fg-muted disabled:opacity-40 transition-colors"
              >
                <Icon name="stop" size="w-3.5 h-3.5" fill="currentColor" />
              </button>
            ) : (
              <button
                type="button"
                disabled={!input.trim()}
                onClick={() => void send(input)}
                aria-label="Send"
                className="min-h-touch min-w-touch flex items-center justify-center rounded-lg bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white transition-colors"
              >
                <Icon name="arrow-send" size="w-4 h-4" />
              </button>
            )}
          </div>
        </div>
      </aside>
    </>
  );
}
