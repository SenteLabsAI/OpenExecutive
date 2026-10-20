// A shared chat as it happens: what the stream from GET /sessions/{id}/live
// (lib/api.ts `followChat`) does to the page. Kept apart from the component
// so `npm test` can check it (see scripts/liveChat.test.mjs).

import type { ActionTaken, LiveChatEvent } from "@/lib/api";

/** Someone else's turn, shown while the Executive answers it. */
export interface LiveTurn {
  turnKey: string;
  senderPersonId: number | null;
  senderName: string;
  content: string;
  reply: string;
  actions: ActionTaken[];
  /** Working on it with nothing new to show (a tool running). */
  thinking: boolean;
  activity: string | null;
}

export interface Typist {
  personId: number;
  name: string;
  /** Until when (ms) the line shows without another `typing`. */
  until: number;
}

export interface LiveState {
  turn: LiveTurn | null;
  typing: Typist[];
}

export const EMPTY_LIVE: LiveState = { turn: null, typing: [] };

/** How long "is typing" shows after the last `typing` event. The sender
 * repeats it every few seconds while they type (TYPING_EVERY_MS). */
export const TYPING_SHOWS_MS = 6000;
export const TYPING_EVERY_MS = 3000;

export interface LiveStep {
  state: LiveState;
  /** Read the saved chat again (a turn ended, or the page reconnected). */
  reload: boolean;
  /** Read who is in the chat again. */
  people: boolean;
  /** The stream is over for good: the viewer left the chat or it is gone. */
  closed: "removed" | "deleted" | null;
}

/** What one live event does. `mine` says whether a turn is one this page
 * sent itself, whose own stream already shows it; `you` is the viewer. */
export function applyLiveEvent(
  state: LiveState,
  event: LiveChatEvent,
  opts: { mine: (turnKey: string) => boolean; you: number | null; now: number },
): LiveStep {
  const step = (next: Partial<LiveStep>): LiveStep => ({
    state,
    reload: false,
    people: false,
    closed: null,
    ...next,
  });
  const typing = state.typing.filter((t) => t.until > opts.now);

  switch (event.type) {
    case "ready": {
      const t = event.turn;
      const turn =
        t && !opts.mine(t.turn_key)
          ? {
              turnKey: t.turn_key,
              senderPersonId: t.sender_person_id,
              senderName: t.sender_name,
              content: t.content,
              reply: t.reply,
              actions: t.actions,
              thinking: !t.reply,
              activity: null,
            }
          : null;
      return step({ state: { turn, typing }, reload: true });
    }
    case "turn_started": {
      if (opts.mine(event.turn_key)) return step({ state: { ...state, typing } });
      return step({
        state: {
          turn: {
            turnKey: event.turn_key,
            senderPersonId: event.sender_person_id,
            senderName: event.sender_name,
            content: event.content,
            reply: "",
            actions: [],
            thinking: true,
            activity: null,
          },
          // They sent it: they're no longer typing.
          typing: typing.filter((t) => t.personId !== event.sender_person_id),
        },
      });
    }
    case "chunk":
    case "thinking":
    case "activity":
    case "action_taken":
    case "phase": {
      const turn = state.turn;
      if (!turn || turn.turnKey !== event.turn_key) return step({});
      if (event.type === "chunk") {
        return step({ state: { ...state, turn: { ...turn, reply: turn.reply + event.content, thinking: false, activity: null } } });
      }
      if (event.type === "thinking") return step({ state: { ...state, turn: { ...turn, thinking: true } } });
      if (event.type === "activity") return step({ state: { ...state, turn: { ...turn, activity: event.label } } });
      if (event.type === "action_taken") {
        const { turn_key: _key, ...action } = event;
        void _key;
        return step({ state: { ...state, turn: { ...turn, actions: [...turn.actions, action] } } });
      }
      return step({});
    }
    case "turn_ended": {
      const ours = opts.mine(event.turn_key);
      const turn = state.turn && state.turn.turnKey === event.turn_key ? null : state.turn;
      // This page's own turn already shows its reply; anyone else's is read
      // from the saved chat, which has its id, sources and added messages.
      return step({ state: { turn, typing }, reload: !ours });
    }
    case "typing": {
      if (event.person_id === opts.you) return step({});
      const others = typing.filter((t) => t.personId !== event.person_id);
      return step({
        state: {
          ...state,
          typing: [...others, { personId: event.person_id, name: event.name, until: opts.now + TYPING_SHOWS_MS }],
        },
      });
    }
    case "people":
      return step({ people: true });
    case "resync":
      return step({ reload: true });
    case "removed":
    case "deleted":
      return step({ state: EMPTY_LIVE, closed: event.type });
    default:
      return step({});
  }
}

/** First names of whoever is typing now, as one line, or null. */
export function typingLine(typing: Typist[], now: number): string | null {
  const names = typing.filter((t) => t.until > now).map((t) => t.name.trim().split(/\s+/)[0] || t.name);
  if (names.length === 0) return null;
  if (names.length === 1) return `${names[0]} is typing…`;
  if (names.length === 2) return `${names[0]} and ${names[1]} are typing…`;
  return "Several people are typing…";
}

/** Who else is in a shared chat, as one line next to their faces. */
export function peopleLine(others: string[]): string {
  const first = (n: string) => n.trim().split(/\s+/)[0] || n;
  if (others.length === 0) return "Only you";
  if (others.length === 1) return `You and ${first(others[0])}`;
  if (others.length === 2) return `You, ${first(others[0])} and ${first(others[1])}`;
  return `You and ${others.length} others`;
}
