"use client";

import { useEffect, useRef, useState } from "react";
import { useSession } from "next-auth/react";
import Message from "./Message";
import BrandMark from "./BrandMark";
import TurnStatusRow, { useTurnClock } from "./TurnStatusRow";
import Icon from "./Icon";
import InfoTip from "./InfoTip";
import OverflowMenu from "./ui/OverflowMenu";
import {
  MAX_FILES_PER_TURN,
  mergePickedFiles,
} from "@/lib/file-attachments";
import {
  ActionTaken,
  addChatMessage,
  ChatMessage,
  CommitteePhase,
  DebugEvent,
  FALLBACK_ACTIVITY_LABEL,
  getFollowupSuggestion,
  getSuggestedPrompts,
  setMessageFeedback,
  streamChat,
} from "@/lib/api";
import { answerSourcesFrom, type AnswerSources } from "@/lib/answerSources";
import { isAbortError, useStoppableTurn } from "@/lib/use-stoppable-turn";
import {
  composerText,
  markTaken,
  settleQueue,
  type QueuedMessage,
} from "@/lib/queuedMessages";
import { newClientTurnId } from "@/lib/turn-id";
import { turnStatus } from "@/lib/turnStatus";

interface ChatProps {
  onDebugEvent?: (event: DebugEvent) => void;
  initialMessages?: ChatMessage[];
  initialSessionId?: string;
  // Pre-populate the input box on first render. Used when entering chat
  // mode from a briefing item — the parent seeds a "Tell me about: …"
  // prompt that the user can edit before sending. Only consumed on mount
  // for a given session; subsequent changes are ignored to avoid clobbering
  // the user's typing.
  initialInput?: string;
  // When true AND `initialInput` is non-empty, submit it as the first turn
  // automatically instead of leaving it as a draft. Briefing handoffs
  // (Discuss / Approve / Dismiss / Edit&Approve) use this — the user has
  // already committed by clicking the action; making them hit Send again
  // is friction. One-shot per mount; ignored on subsequent prop updates.
  autoSubmitInitialInput?: boolean;
  // Sent with the auto-submitted `initialInput` only: the short line peer
  // memory records instead of the seed, which quotes the Executive's own
  // briefing card. Typed messages record exactly what was typed.
  initialMemoryText?: string;
  onTurnComplete?: (sessionId: string) => void;
  onTurnStart?: () => void;
}

// Static fallbacks used only when the /chat/suggested-prompts fetch fails
// entirely (network error, aborted, etc). The backend always returns these
// same values on its own failure paths, so the happy path never shows them.
const SUGGESTED_PROMPTS = [
  "Where did we land on this quarter's priorities?",
  "Pull the team in on a decision I'm sitting on.",
  "Let's review the board update before it goes out.",
  "What's changed since our last sync?",
];

const DEFAULT_PLACEHOLDER = "What's on your mind?";
const WORKING_PLACEHOLDER = "Add something while it works…";

// A follow-up is only worth suggesting when the conversation ends on a
// persisted reply — the backend keys its suggestion on that reply's id.
function endsOnReply(messages: ChatMessage[] | undefined): boolean {
  const last = messages?.[messages.length - 1];
  return last?.role === "assistant" && Boolean(last.id);
}

const FALLBACK_SUBTITLE =
  "Pick up where we left off — decisions to revisit, drafts to push forward, people to pull in.";

export default function Chat({ onDebugEvent, initialMessages, initialSessionId, initialInput, autoSubmitInitialInput, initialMemoryText, onTurnComplete, onTurnStart }: ChatProps) {
  const { data: session } = useSession();
  const firstName = session?.user?.name?.trim().split(/\s+/)[0];

  const [messages, setMessages] = useState<ChatMessage[]>(initialMessages ?? []);
  const [input, setInput] = useState(initialInput ?? "");
  const [isLoading, setIsLoading] = useState(false);
  const { isStopping, beginTurn, stop: handleStop, serverAcknowledgedStop, endTurn } =
    useStoppableTurn();
  const [sessionId, setSessionId] = useState<string | undefined>(initialSessionId);
  const [streamingContent, setStreamingContent] = useState("");
  // Inline action chips that arrived for the in-flight assistant message.
  // Reset on every turn; frozen onto the message at `done` event time.
  const [streamingActions, setStreamingActions] = useState<ActionTaken[]>([]);
  const [isConsulting, setIsConsulting] = useState(false);
  const [activityLabel, setActivityLabel] = useState<string | null>(null);
  const [committeeEnabled, setCommitteeEnabled] = useState(false);
  const [committeePhase, setCommitteePhase] = useState<CommitteePhase | null>(null);
  const turnClock = useTurnClock(isLoading);
  const [suggested, setSuggested] = useState<string[]>([]);
  const [subtitle, setSubtitle] = useState<string>(FALLBACK_SUBTITLE);
  const [isLoadingPrompts, setIsLoadingPrompts] = useState(true);
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [fileError, setFileError] = useState<string | null>(null);
  // Suggested next message, shown as the composer's greyed placeholder and
  // accepted with Tab / → or the inline chip. Refreshed after every reply.
  const [followup, setFollowup] = useState<string | null>(null);
  const followupCtrlRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const adoptedSessionIdRef = useRef<string | undefined>(initialSessionId);
  // Messages typed while a turn runs (see lib/queuedMessages). The ref is
  // what the turn reads when it ends; the state is what renders.
  const [queued, setQueuedState] = useState<QueuedMessage[]>([]);
  const queuedRef = useRef<QueuedMessage[]>([]);
  function setQueued(next: QueuedMessage[]) {
    queuedRef.current = next;
    setQueuedState(next);
  }
  // The running turn's id, which addresses it for POST /chat/add.
  const turnIdRef = useRef<string | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    getSuggestedPrompts(ctrl.signal)
      .then((r) => {
        if (r.prompts.length >= 4) setSuggested(r.prompts.slice(0, 4));
        else setSuggested(SUGGESTED_PROMPTS);
        if (r.subtitle) setSubtitle(r.subtitle);
        setIsLoadingPrompts(false);
      })
      .catch((err: unknown) => {
        // Abort on unmount is expected — don't flip loading off so we don't
        // briefly flash the static fallback before the component is gone.
        if (err instanceof DOMException && err.name === "AbortError") return;
        setSuggested(SUGGESTED_PROMPTS);
        setIsLoadingPrompts(false);
      });
    return () => ctrl.abort();
  }, []);

  function clearFollowup() {
    followupCtrlRef.current?.abort();
    followupCtrlRef.current = null;
    setFollowup(null);
  }

  function loadFollowup(id: string) {
    followupCtrlRef.current?.abort();
    const ctrl = new AbortController();
    followupCtrlRef.current = ctrl;
    getFollowupSuggestion(id, ctrl.signal)
      .then((suggestion) => {
        // A newer fetch (or a send) superseded this one.
        if (followupCtrlRef.current === ctrl) setFollowup(suggestion);
      })
      // Abort or network failure: keep the static placeholder.
      .catch(() => {});
  }

  // Opening an existing session suggests a follow-up to its last reply.
  // Later session switches are handled in the sync effect below.
  useEffect(() => {
    if (initialSessionId && endsOnReply(initialMessages)) loadFollowup(initialSessionId);
    return () => followupCtrlRef.current?.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Sync when parent selects a different session (or clears for new chat).
  // Skip when the prop change is the parent echoing back an id this turn
  // already adopted locally — otherwise we'd wipe the just-streamed reply.
  useEffect(() => {
    if (initialSessionId === adoptedSessionIdRef.current) return;
    adoptedSessionIdRef.current = initialSessionId;
    setMessages(initialMessages ?? []);
    setSessionId(initialSessionId);
    setStreamingContent("");
    clearFollowup();
    if (initialSessionId && endsOnReply(initialMessages)) loadFollowup(initialSessionId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialSessionId]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, streamingContent]);

  // One-shot auto-submit of the initialInput on mount when the parent
  // requests it (briefing handoffs). Guarded by a ref so prop churn can't
  // re-fire it — mirrors the existing initialInput "adopt-once" contract.
  // We pass the prompt explicitly into handleSend so the state-clearing in
  // handleSend doesn't race with React batching `setInput("")` after the
  // submit reads it back.
  // Escape stops the turn, wherever focus is: a document listener rather than
  // the textarea's onKeyDown, so it works with focus on the page too. Not
  // while something is being typed to add to the turn, where Escape throwing
  // away the answer would be a nasty surprise.
  useEffect(() => {
    if (!isLoading) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (textareaRef.current?.value.trim()) return;
      // An overlay that consumed this Escape (a tooltip, a dialog) calls
      // preventDefault. Stopping the turn as well would make one keypress do
      // two unrelated things.
      if (e.defaultPrevented) return;
      e.preventDefault();
      void handleStop();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [isLoading, handleStop]);

  const didAutoSubmitRef = useRef(false);
  // A handoff turn stopped before any output is returned to the composer as
  // its seed. Resent unchanged it is still the Executive's text, so it keeps
  // recording the short memory line; anything else sent next (an edit, a
  // new question, a suggestion chip) records its own text. Cleared on send.
  const restoredHandoffRef = useRef<{ seed: string; memoryText: string } | undefined>(undefined);
  useEffect(() => {
    if (didAutoSubmitRef.current) return;
    if (!autoSubmitInitialInput) return;
    const seed = (initialInput ?? "").trim();
    if (!seed) return;
    didAutoSubmitRef.current = true;
    handleSend(seed, initialMemoryText);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Every streamed event carries the resolved session id, not just `done`.
  // Adopting it as soon as it is seen means a turn that ends without `done`
  // (an aborted stream) still leaves the client pointing at the right session,
  // rather than falling back to "" and orphaning the conversation.
  function adoptSessionId(id: string) {
    if (adoptedSessionIdRef.current === id) return;
    adoptedSessionIdRef.current = id;
    setSessionId(id);
  }

  // A message typed while the Executive works goes to the running turn, which
  // folds it into the answer it is writing. If the turn can't take it any
  // more, it is sent as the next turn when this one ends.
  function addWhileWorking() {
    const text = input.trim();
    const turnId = turnIdRef.current;
    if (!text || !turnId) return;
    const id = newClientTurnId();
    setInput("");
    if (textareaRef.current) textareaRef.current.style.height = "auto";
    setQueued([...queuedRef.current, { id, text, taken: false }]);
    void addChatMessage(turnId, id, text);
  }

  // `queuedFollowup`: the leftovers of a turn, sent on their own. They carry
  // no files and leave alone whatever is being typed in the composer now.
  async function handleSend(text?: string, memoryText?: string, queuedFollowup = false) {
    if (isLoading && text === undefined) {
      addWhileWorking();
      return;
    }
    const message = (text ?? input).trim();
    if ((!message && pendingFiles.length === 0) || isLoading) return;
    const restored = restoredHandoffRef.current;
    restoredHandoffRef.current = undefined;
    const turnMemoryText =
      memoryText ??
      (text === undefined && restored?.seed === message ? restored.memoryText : undefined);

    const filesForTurn = queuedFollowup ? [] : pendingFiles;
    const userBubbleContent = filesForTurn.length
      ? `${message}${message ? "\n\n" : ""}📎 ${filesForTurn.length} file${filesForTurn.length === 1 ? "" : "s"} attached`
      : message;

    if (!queuedFollowup) {
      setInput("");
      setPendingFiles([]);
      setFileError(null);
    }
    clearFollowup();
    setMessages((prev) => [...prev, { role: "user", content: userBubbleContent }]);
    setIsLoading(true);
    turnClock.start();
    setStreamingContent("");
    setStreamingActions([]);
    setIsConsulting(false);
    setActivityLabel(null);
    setCommitteePhase(null);
    const { clientTurnId, signal } = beginTurn();
    turnIdRef.current = clientTurnId;
    setQueued([]);
    onTurnStart?.();

    if (textareaRef.current && !queuedFollowup) {
      textareaRef.current.style.height = "auto";
    }

    // Declared outside the try so the abort path in `catch` can still commit
    // whatever streamed before the stop.
    let accumulated = "";
    // Local mirror of streamingActions so the closure builds the final
    // message without depending on the async setState applying first.
    const turnActions: ActionTaken[] = [];
    // What the reply looked at; sent once, just before `done`.
    let turnSources: AnswerSources | undefined;
    let wasStopped = false;
    // Row id of the persisted reply, from `done`; it is what makes the reply
    // rateable with 👍/👎.
    let replyId: number | undefined;
    // Session id from `done`, used to fetch the follow-up for this reply.
    let doneSessionId: string | undefined;
    // What the queue left to send as the next turn, once this one settles.
    let nextTurn: string | null = null;
    // Messages the turn took, shown before its reply. Each settle path that
    // keeps the reply also keeps these.
    const settleWithReply = () => {
      const settled = settleQueue(queuedRef.current);
      setQueued([]);
      nextTurn = settled.next;
      return settled.taken.map((t): ChatMessage => ({ role: "user", content: t }));
    };

    try {
      // The ref, not the state: a turn sent straight after the previous one
      // (a message queued while it worked) runs before the state catches up,
      // and must still continue the conversation it was typed in.
      for await (const item of streamChat(message, adoptedSessionIdRef.current, {
        committeeReview: committeeEnabled,
        files: filesForTurn,
        clientTurnId,
        signal,
        memoryText: turnMemoryText,
      })) {
        turnClock.markEvent();
        if (item.type === "debug_event") {
          onDebugEvent?.(item);
          continue;
        }
        if (item.type === "chunk" && item.content) {
          if (item.session_id) adoptSessionId(item.session_id);
          accumulated += item.content;
          setIsConsulting(false);
          setActivityLabel(null);
          setStreamingContent(accumulated);
        } else if (item.type === "activity") {
          // Arrives immediately before `thinking`, so the label is in place
          // before the indicator turns on. Deliberately not cleared by
          // `action_taken`: a chip can land while the same round is still
          // running, and clearing there would flicker the line off and on.
          setActivityLabel(item.label);
        } else if (item.type === "thinking") {
          setIsConsulting(true);
        } else if (item.type === "phase" && item.phase) {
          setCommitteePhase(item.phase);
          setIsConsulting(false);
          setActivityLabel(null);
        } else if (item.type === "committee_critique") {
          // Severity preview only; full critique stays server-side.
          // Nothing to render yet — phase indicator already reflects the
          // reviewing step. Hook left for future debug-panel surfacing.
        } else if (item.type === "action_taken") {
          turnActions.push(item);
          setStreamingActions([...turnActions]);
        } else if (item.type === "sources") {
          turnSources = answerSourcesFrom(item);
        } else if (item.type === "message_added") {
          setQueued(markTaken(queuedRef.current, item.ids));
        } else if (item.type === "stopped") {
          // The server acknowledged the stop and is winding the turn down
          // itself; `done` follows over the same stream. Stand the abort
          // fallback down, or it would fire mid-wind-down and cost us the
          // terminal events — including the `session_id` a first turn needs.
          wasStopped = true;
          serverAcknowledgedStop();
          if (item.session_id) adoptSessionId(item.session_id);
        } else if (item.type === "done") {
          if (item.message_id) replyId = item.message_id;
          if (item.session_id) {
            doneSessionId = item.session_id;
            adoptSessionId(item.session_id);
            onTurnComplete?.(item.session_id);
          }
        } else if (item.type === "error") {
          throw new Error(item.message);
        }
      }

      // A stop before any output persists nothing server-side — not even the
      // user's message, because saving it alone would break the user/assistant
      // alternation the stored history relies on. So rather than leave a pair
      // of bubbles that silently vanish on reload, take the message back and
      // return the text to the composer: the user stopped before it started,
      // and can edit and resend. Skipped when files were attached — dropping a
      // file selection without saying so would be worse than the mismatch.
      if (wasStopped && !accumulated && turnActions.length === 0) {
        // Nothing was saved, so nothing typed while it worked was either.
        const extra = queuedRef.current;
        setQueued([]);
        if (filesForTurn.length === 0) {
          setMessages((prev) =>
            prev.length && prev[prev.length - 1].role === "user"
              ? prev.slice(0, -1)
              : prev,
          );
          setInput(composerText(message, extra));
          restoredHandoffRef.current = turnMemoryText && extra.length === 0
            ? { seed: message, memoryText: turnMemoryText }
            : undefined;
        } else if (extra.length > 0) {
          setInput(composerText("", extra));
        }
      } else if (accumulated || turnActions.length > 0) {
        const takenBubbles = settleWithReply();
        setMessages((prev) => [
          ...prev,
          ...takenBubbles,
          {
            role: "assistant",
            content: accumulated,
            actions: turnActions.length > 0 ? turnActions : undefined,
            stopped: wasStopped || undefined,
            sources: turnSources,
            id: replyId,
          },
        ]);
        // Only a complete, persisted reply gets a follow-up: a stopped one
        // is half an answer, and without an id there is nothing to key on.
        if (!wasStopped && replyId && doneSessionId) loadFollowup(doneSessionId);
      }
      setStreamingContent("");
      setStreamingActions([]);
    } catch (err) {
      if (isAbortError(err)) {
        // Our own safety-net abort fired (the server never sent `stopped`).
        // Keep whatever streamed; this is a stop, not a failure.
        if (accumulated || turnActions.length > 0) {
          const takenBubbles = settleWithReply();
          setMessages((prev) => [
            ...prev,
            ...takenBubbles,
            {
              role: "assistant",
              content: accumulated,
              actions: turnActions.length > 0 ? turnActions : undefined,
              stopped: true,
              sources: turnSources,
            },
          ]);
        } else if (queuedRef.current.length > 0) {
          setInput(composerText("", queuedRef.current));
          setQueued([]);
        }
        // `done` never arrived, so nothing else will clear the parent's
        // in-flight state or refresh the sidebar. Safe to call with an empty
        // id: the parent only adopts a truthy one (see handleTurnComplete).
        onTurnComplete?.(adoptedSessionIdRef.current ?? sessionId ?? "");
      } else {
        const detail = err instanceof Error ? err.message : String(err);
        // A failed turn saved nothing: give back what was typed while it
        // worked rather than sending it into the same failure.
        if (queuedRef.current.length > 0) {
          setInput(composerText("", queuedRef.current));
          setQueued([]);
        }
        setMessages((prev) => [
          ...prev,
          { role: "assistant", content: `Something went wrong: ${detail}` },
        ]);
      }
      setStreamingContent("");
      setStreamingActions([]);
    } finally {
      endTurn();
      turnIdRef.current = null;
      setIsLoading(false);
      setIsConsulting(false);
      setActivityLabel(null);
      setCommitteePhase(null);
      textareaRef.current?.focus();
    }
    // What was typed while it worked and the turn never got to: its own turn
    // now, in the same conversation.
    if (nextTurn) void handleSend(nextTurn, undefined, true);
  }

  // 👍/👎 on a persisted reply, applied optimistically and rolled back if
  // the save fails. Clicking the active rating again clears it.
  function handleFeedback(index: number, value: "up" | "down" | null) {
    const target = messages[index];
    if (!sessionId || target?.role !== "assistant" || !target.id) return;
    const previous = target.feedback ?? null;
    const apply = (v: "up" | "down" | null) =>
      setMessages((prev) => prev.map((m, i) => (i === index ? { ...m, feedback: v } : m)));
    apply(value);
    setMessageFeedback(sessionId, target.id, value).catch(() => apply(previous));
  }

  // The "still working" line: before the reply starts, and again under the
  // partial reply whenever the Executive goes quiet to run a tool.
  const status = turnStatus({
    isLoading,
    hasText: Boolean(streamingContent),
    isConsulting,
    activityLabel,
    inCommittee: committeePhase !== null,
    msSinceTurnStart: turnClock.msSinceTurnStart,
    msSinceLastEvent: turnClock.msSinceLastEvent,
    fallbackLabel: FALLBACK_ACTIVITY_LABEL,
  });

  // Fill the composer with the suggestion without sending it, so the user
  // can edit first. The suggestion is kept: clearing the box shows it again.
  function acceptFollowup() {
    if (!followup) return;
    setInput(followup);
    requestAnimationFrame(() => {
      const el = textareaRef.current;
      if (!el) return;
      el.focus();
      el.setSelectionRange(followup.length, followup.length);
      el.style.height = "auto";
      el.style.height = Math.min(el.scrollHeight, 160) + "px";
    });
  }

  const showFollowup = Boolean(followup) && !input && !isLoading;

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    // Tab / → take the suggestion only while the box is empty; otherwise
    // both keep their usual meaning (focus move, caret move).
    if (
      showFollowup &&
      ((e.key === "Tab" && !e.shiftKey) || e.key === "ArrowRight")
    ) {
      e.preventDefault();
      acceptFollowup();
      return;
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }

  function handleTextareaChange(e: React.ChangeEvent<HTMLTextAreaElement>) {
    setInput(e.target.value);
    e.target.style.height = "auto";
    // Emptied: drop the pixel height so the textarea stretches to its grid
    // cell again, which the suggestion ghost may have made taller.
    if (e.target.value) e.target.style.height = Math.min(e.target.scrollHeight, 160) + "px";
  }

  function handleFilesPicked(e: React.ChangeEvent<HTMLInputElement>) {
    const picked = Array.from(e.target.files ?? []);
    // Reset the input so re-picking the same file re-fires onChange.
    e.target.value = "";
    if (picked.length === 0) return;
    const result = mergePickedFiles(pendingFiles, picked);
    setPendingFiles(result.files);
    setFileError(result.rejected.length > 0 ? result.rejected.join(" ") : null);
  }

  function removePendingFile(index: number) {
    setPendingFiles((prev) => prev.filter((_, i) => i !== index));
  }

  function formatFileSize(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  const isEmpty = messages.length === 0 && !isLoading && !streamingContent;

  return (
    <div className="flex flex-col h-full">
      {/* Messages */}
      <div className="flex-1 overflow-y-auto">
        <div className="max-w-3xl mx-auto px-4 sm:px-6 py-6 sm:py-8">
          {isEmpty ? (
            /* Empty state */
            <div className="flex flex-col items-center justify-center min-h-[60vh] text-center">
              <div className="mb-6">
                <BrandMark size="lg" />
              </div>
              {/* Not the briefing's headline: Home already greets you. */}
              <h2 className="text-2xl font-bold tracking-tight text-fg mb-2">
                {firstName ? `What's on your mind, ${firstName}?` : "What's on your mind?"}
              </h2>
              <p className="text-fg-muted text-[15px] max-w-md mb-10">
                {subtitle}
              </p>

              <div
                className="grid grid-cols-1 sm:grid-cols-2 gap-2 w-full max-w-lg"
                role="status"
                aria-busy={isLoadingPrompts}
                aria-label={isLoadingPrompts ? "Loading suggested prompts" : "Suggested prompts"}
              >
                {isLoadingPrompts
                  ? [0, 1, 2, 3].map((i) => (
                      <div
                        key={i}
                        aria-hidden
                        className="min-h-[52px] rounded-2xl bg-surface-overlay/60 border border-line animate-pulse motion-reduce:animate-none"
                      />
                    ))
                  : suggested.map((prompt) => (
                      <button
                        type="button"
                        key={prompt}
                        onClick={() => handleSend(prompt)}
                        className="text-left px-4 py-3.5 min-h-[52px] rounded-2xl bg-surface-elevated border border-line hover:border-line-strong hover:bg-surface-hover text-fg-muted hover:text-fg text-[15px] transition-all duration-150 cursor-pointer"
                      >
                        {prompt}
                      </button>
                    ))}
              </div>
            </div>
          ) : (
            <>
              {messages.map((msg, i) => (
                <Message
                  key={i}
                  role={msg.role}
                  content={msg.content}
                  actions={msg.role === "assistant" ? msg.actions : undefined}
                  stopped={msg.role === "assistant" ? msg.stopped : undefined}
                  sources={msg.role === "assistant" ? msg.sources : undefined}
                  feedback={msg.role === "assistant" ? msg.feedback : undefined}
                  onFeedback={
                    msg.role === "assistant" && msg.id && sessionId
                      ? (v) => handleFeedback(i, v)
                      : undefined
                  }
                />
              ))}

              {queued.map((q) => (
                <div key={q.id} className="flex flex-col items-end mb-6">
                  <div className="max-w-xl px-4 py-3 rounded-2xl rounded-tr-sm bg-surface-overlay text-fg text-sm leading-relaxed">
                    <p className="whitespace-pre-wrap">{q.text}</p>
                  </div>
                  <p className="mt-1 text-xs text-fg-muted" aria-live="polite">
                    {q.taken ? "👀 Seen, taking it into this answer" : "Sent while it works"}
                  </p>
                </div>
              ))}

              {streamingContent && (
                <Message
                  role="assistant"
                  content={streamingContent}
                  isStreaming
                  actions={streamingActions.length > 0 ? streamingActions : undefined}
                  status={
                    status.show ? (
                      <TurnStatusRow status={status} committeePhase={committeePhase} />
                    ) : undefined
                  }
                />
              )}

              {status.show && !streamingContent && (
                <div className="flex gap-4 mb-8">
                  <div className="flex-shrink-0 mt-1">
                    <BrandMark size="md" />
                  </div>
                  <div className="flex-1 pt-1.5">
                    <div className="text-xs text-fg-muted mb-3 font-medium tracking-wide uppercase">Executive</div>
                    <TurnStatusRow status={status} committeePhase={committeePhase} />
                  </div>
                </div>
              )}
            </>
          )}
          <div ref={bottomRef} />
        </div>
      </div>

      {/* Input */}
      <div className="border-t border-line bg-surface px-4 sm:px-6 py-3 sm:py-4">
        <div className="max-w-3xl mx-auto">
          {/* Committee review is switched in the + menu; while it is on, this
              chip says so and turns it off. */}
          {committeeEnabled && (
            <div className="mb-2">
              <button
                type="button"
                onClick={() => setCommitteeEnabled(false)}
                disabled={isLoading}
                aria-pressed
                title="Committee review: slower, higher-quality response — adversarial review pass before sending. Click to turn off."
                className="inline-flex min-h-[32px] items-center gap-1.5 rounded-lg border border-accent/40 bg-accent/10 px-2.5 text-xs font-medium text-accent hover:bg-accent/15 disabled:opacity-50 cursor-pointer"
              >
                <Icon name="users" size="w-3.5 h-3.5" />
                Committee review on
                <Icon name="close" size="w-3 h-3" />
              </button>
            </div>
          )}
          {pendingFiles.length > 0 && (
            <div className="flex flex-wrap gap-2 mb-2" aria-label="Pending attachments">
              {pendingFiles.map((f, i) => (
                <div
                  key={`${f.name}-${f.size}-${i}`}
                  className="flex items-center gap-2 bg-surface-overlay border border-line-strong rounded-lg pl-2.5 pr-1 py-1 text-xs text-fg-muted max-w-xs"
                >
                  <Icon name="paperclip" size="w-3 h-3" className="text-fg-muted" />
                  <span className="truncate" title={f.name}>{f.name}</span>
                  <span className="text-fg-muted/70 flex-shrink-0">{formatFileSize(f.size)}</span>
                  <button
                    type="button"
                    onClick={() => removePendingFile(i)}
                    aria-label={`Remove ${f.name}`}
                    className="flex-shrink-0 w-5 h-5 rounded hover:bg-line-strong/50 flex items-center justify-center cursor-pointer"
                  >
                    <Icon name="close" size="w-3 h-3" />
                  </button>
                </div>
              ))}
            </div>
          )}
          {fileError && (
            <p className="text-sm text-red-400 mb-2" role="alert">
              {fileError}
            </p>
          )}
          <div className="relative flex items-end gap-1.5 sm:gap-2 bg-surface-elevated border border-line-strong rounded-2xl p-1.5 sm:p-2 shadow-sm focus-within:border-accent/60 transition-colors">
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept="image/png,image/jpeg,image/gif,image/webp,.pdf,.docx,.doc,.txt,.md,.csv"
              onChange={handleFilesPicked}
              className="hidden"
              aria-hidden="true"
            />
            {/* The + menu: attach files, and committee review (on/off). */}
            <OverflowMenu
              label="Add to your message"
              align="left"
              placement="up"
              icon={<Icon name="plus" size="w-5 h-5" />}
              items={[
                {
                  label:
                    pendingFiles.length >= MAX_FILES_PER_TURN
                      ? `Attach files (limit ${MAX_FILES_PER_TURN} per message)`
                      : "Attach files or photos",
                  onSelect: () => fileInputRef.current?.click(),
                  disabled: isLoading || pendingFiles.length >= MAX_FILES_PER_TURN,
                },
                {
                  label: committeeEnabled ? "Turn off committee review" : "Committee review (slower, reviewed)",
                  onSelect: () => setCommitteeEnabled((v) => !v),
                  disabled: isLoading,
                },
              ]}
            />
            {/* The suggestion is drawn by a ghost layer sharing the textarea's
                grid cell rather than by the native placeholder, which a
                one-row textarea clips to its first line. The cell grows to
                the ghost's wrapped height; the placeholder attribute still
                carries the text for screen readers, just painted transparent. */}
            <div className="grid flex-1 min-w-0 self-center py-2">
              {showFollowup && (
                <div
                  aria-hidden
                  className="col-start-1 row-start-1 pointer-events-none text-fg-muted text-sm sm:text-base leading-relaxed whitespace-pre-wrap break-words max-h-40 overflow-hidden"
                >
                  {followup}
                </div>
              )}
              <textarea
                ref={textareaRef}
                value={input}
                onChange={handleTextareaChange}
                onKeyDown={handleKeyDown}
                placeholder={isLoading ? WORKING_PLACEHOLDER : followup ?? DEFAULT_PLACEHOLDER}
                rows={1}
                aria-label="Message"
                className={
                  "col-start-1 row-start-1 w-full bg-transparent text-fg text-sm sm:text-base leading-relaxed resize-none focus:outline-none disabled:opacity-50 max-h-40 overflow-y-auto " +
                  (showFollowup ? "placeholder:text-transparent" : "placeholder:text-fg-muted")
                }
                style={{ minHeight: "24px" }}
              />
            </div>
            {isLoading && (
              <button
                type="button"
                onClick={handleStop}
                disabled={isStopping}
                aria-label="Stop the executive"
                title="Stop — whatever has been written so far is kept"
                className="flex-shrink-0 w-11 h-11 rounded-xl bg-surface-overlay border border-line-strong text-fg hover:border-fg-muted disabled:opacity-30 disabled:cursor-not-allowed transition-all duration-150 flex items-center justify-center cursor-pointer"
              >
                <Icon name="stop" size="w-3.5 h-3.5" fill="currentColor" />
              </button>
            )}
            {/* While it works, Send appears once there is something to add. */}
            {(!isLoading || input.trim()) && (
              <button
                type="button"
                onClick={() => handleSend()}
                disabled={!input.trim() && pendingFiles.length === 0}
                aria-label={isLoading ? "Add to what it's working on" : "Send message"}
                className="flex-shrink-0 w-11 h-11 rounded-xl bg-accent-strong hover:bg-accent-strong/90 disabled:opacity-30 disabled:cursor-not-allowed transition-all duration-150 flex items-center justify-center cursor-pointer"
              >
                <Icon name="arrow-send" size="w-4 h-4" className="text-white" />
              </button>
            )}
          </div>
          <p className="text-center text-xs text-fg-muted mt-2 inline-flex items-center justify-center gap-1.5 w-full">
            <span className="hidden sm:inline">Enter to send · Shift+Enter for new line</span>
            <span className="sm:hidden">Tap send</span>
            {/* The suggestion's hint is also its button: Tab on a keyboard,
                a tap or click anywhere else. */}
            {showFollowup && (
              <button
                type="button"
                onClick={acceptFollowup}
                title="Use the suggested follow-up (Tab)"
                aria-label={`Use suggested follow-up: ${followup}`}
                className="min-h-touch px-1 text-accent hover:underline font-medium cursor-pointer"
              >
                <span className="hidden sm:inline">· Tab ↹ to use suggestion</span>
                <span className="sm:hidden">· Use suggestion</span>
              </button>
            )}
            <InfoTip align="right">
              Your Executive routes your question to the right specialist
              behind the scenes — you don&apos;t pick which one.
            </InfoTip>
          </p>
        </div>
      </div>
    </div>
  );
}
