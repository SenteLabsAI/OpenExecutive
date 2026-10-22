// In a frame, what the page changes beyond the host list asks first, in a
// window of its own (components/embed/EmbedProvider.tsx, app/confirm).
//
// A page that frames this one (a host origin, or any page frame-ancestors
// lets in) can't read or click inside the frame, but it can make the frame
// invisible, lay its own buttons over it and catch clicks meant for those:
// two lured clicks beat any confirmation drawn inside the frame. So in a
// frame, approving, rejecting or cancelling a card, and any other change the
// host list doesn't already let the host make itself (a setting, Handle it
// for me, People), first opens a small window of this app's own, which no
// page can cover, naming what will happen; only its Yes goes ahead. Outside
// a frame nothing changes.
//
// The embedded shell checks every request the page sends against
// `frameActionFor`, so no call site can forget to ask.
// scripts/cardActions.test.mjs fails when lib/api.ts calls a card action this
// doesn't name.
//
// No imports, so `npm test` can exercise this.

export type CardAction = {
  verb: "approve" | "reject" | "cancel" | "change";
  // The card's decision id, when it has one (its details are shown too).
  decisionId: number | null;
  // What will happen, in a sentence.
  what: string;
  // A card action always asks. Any other change asks only when it follows
  // a click (it could have been lured); one the page makes on its own
  // can't have been.
  card: boolean;
};

type HostCall = readonly [method: string, pattern: string];

const BACKEND = "/api/backend";
const READS = new Set(["GET", "HEAD", "OPTIONS"]);
// Chat messages are on the host list, but only a host's own are read-only:
// sent from this app's page in a frame, a turn is the person's own, with
// every tool. So in a frame they ask like any other change.
const CHAT_SENDS = new Set(["/chat", "/chat/upload", "/chat/add"]);
// The changes a lured click would most like to make, said plainly.
const NAMED_CHANGES: readonly (readonly [RegExp, string])[] = [
  [/^\/delegation\/handle-it/, "Change Handle it for me: what the Executive sends as you without asking."],
  [/^\/delegation\//, "Change what the Executive may do as you (Delegate)."],
  [/^\/executive\/(pause|resume)$/, "Pause or resume the Executive."],
  [/^\/people(\/|$)/, "Change People: who is on the roster and what they may do."],
  [/^\/departments(\/|$)/, "Change a department or who leads it."],
  [/^\/(settings|workspace|notifications)(\/|$)/, "Change a setting."],
  [/^\/(documents|knowledge)(\/|$)/, "Change the company knowledge the Executive relies on."],
];
// Writes a frame may make without asking: they change nothing anyone acts on.
const HARMLESS = /^\/sessions\/[^/]+\/typing$/;

function verbFromBody(body: unknown): "approve" | "reject" | null {
  if (typeof body !== "string") return null;
  try {
    const parsed = JSON.parse(body) as { decision?: unknown };
    return parsed.decision === "approve" || parsed.decision === "reject" ? parsed.decision : null;
  } catch {
    return null;
  }
}

function backendPath(url: string): string | null {
  let path: string;
  try {
    path = new URL(url, "http://this.app").pathname;
  } catch {
    return null;
  }
  return path.startsWith(`${BACKEND}/`) ? path.slice(BACKEND.length) : null;
}

/** The card action a request is, or null for anything else. */
export function cardActionFor(method: string, url: string, body?: unknown): CardAction | null {
  if (method.toUpperCase() !== "POST") return null;
  const path = backendPath(url);
  if (path === null) return null;

  let m = /^\/decisions\/(\d+)\/(approve|reject|cancel)$/.exec(path);
  if (m) {
    const id = Number(m[1]);
    const verb = m[2] as "approve" | "reject" | "cancel";
    const what =
      verb === "approve"
        ? `Approve card ${id}: what it shows is carried out now, as you.`
        : verb === "reject"
          ? `Dismiss card ${id}: nothing on it will happen.`
          : `Cancel card ${id}: what it started stops, and nothing more on it will happen.`;
    return { verb, decisionId: id, what, card: true };
  }
  m = /^\/alerts\/(\d+)\/approve$/.exec(path);
  if (m) {
    return {
      verb: "approve",
      decisionId: null,
      what: `Approve the proposal on alert ${m[1]}: what it proposes is carried out now, as you.`,
      card: true,
    };
  }
  m = /^\/workflows\/runs\/([^/]+)\/decision$/.exec(path);
  if (m) {
    const verb = verbFromBody(body) ?? "approve";
    const run = decodeURIComponent(m[1]);
    return {
      verb,
      decisionId: null,
      what:
        verb === "approve"
          ? `Sign off workflow run ${run}: it carries on with what it was waiting to do.`
          : `Turn down workflow run ${run}: it stops where it is waiting.`,
      card: true,
    };
  }
  m = /^\/people\/requests\/(\d+)\/(approve|decline)$/.exec(path);
  if (m) {
    return m[2] === "approve"
      ? { verb: "approve", decisionId: null, what: `Add the person in request ${m[1]} to People, as you answered it.`, card: true }
      : { verb: "reject", decisionId: null, what: `Decline request ${m[1]}: they are not added to People.`, card: true };
  }
  return null;
}

/** The chat message a send carries (JSON or a form), cut short, or "". */
export function messageOf(body: unknown): string {
  let message: unknown = null;
  if (typeof body === "string") {
    try {
      message = (JSON.parse(body) as { message?: unknown }).message;
    } catch {
      return "";
    }
  } else if (body !== null && typeof body === "object" && typeof (body as FormData).get === "function") {
    message = (body as FormData).get("message");
  }
  if (typeof message !== "string") return "";
  const text = message.replace(/\s+/g, " ").trim();
  return text.length > 160 ? `${text.slice(0, 159)}…` : text;
}

function onHostList(calls: readonly HostCall[], method: string, path: string): boolean {
  const segments = path.split("/").slice(1).map((s) => {
    try {
      return decodeURIComponent(s);
    } catch {
      return s;
    }
  });
  return calls.some(([callMethod, pattern]) => {
    if (callMethod !== method) return false;
    const parts = pattern.split("/").slice(1);
    return parts.length === segments.length && parts.every((p, i) => /^\{[a-z_]+\}$/.test(p) || p === segments[i]);
  });
}

/**
 * What a request from a framed page must confirm, or null when it may go
 * straight through: a read, anything outside the backend proxy, a typing
 * notice, and a call on the host list the host could make with the same
 * effect itself (not a chat message: the host's are read-only, this page's
 * are not).
 */
export function frameActionFor(
  method: string,
  url: string,
  body: unknown,
  hostCalls: readonly HostCall[],
): CardAction | null {
  const verb = method.toUpperCase();
  if (READS.has(verb)) return null;
  const card = cardActionFor(verb, url, body);
  if (card) return card;
  const path = backendPath(url);
  if (path === null || HARMLESS.test(path)) return null;
  if (verb === "POST" && CHAT_SENDS.has(path)) {
    const words = messageOf(body);
    return {
      verb: "change",
      decisionId: null,
      what:
        `Send ${words ? `"${words}"` : "this message"} to the Executive as you. ` +
        "It can act on it: send, schedule or change things.",
      // Always: a message sent after a Yes (a queued one, Try again) may
      // carry words that weren't typed here.
      card: true,
    };
  }
  if (onHostList(hostCalls, verb, path)) return null;
  const named = NAMED_CHANGES.find(([re]) => re.test(path));
  return {
    verb: "change",
    decisionId: null,
    what: `${named ? named[1] : "Make this change in Open Executive, as you."} (${verb} ${path.slice(0, 120)})`,
    card: false,
  };
}

/** A line naming the card, from its decision (GET /decisions/{id}): its kind and what it says. */
export function describeDecision(instance: { decision_class?: unknown; proposed_payload_json?: unknown }): string {
  const kind = typeof instance.decision_class === "string" ? instance.decision_class.replace(/[_-]+/g, " ") : "card";
  let detail = "";
  if (typeof instance.proposed_payload_json === "string") {
    try {
      const payload = JSON.parse(instance.proposed_payload_json) as Record<string, unknown>;
      for (const key of ["summary", "title", "subject", "why", "card"]) {
        const value = payload[key];
        if (typeof value === "string" && value.trim()) {
          detail = value.trim().replace(/\s+/g, " ").slice(0, 200);
          break;
        }
      }
    } catch {
      // No detail: the kind alone.
    }
  }
  return detail ? `${kind}: ${detail}` : kind;
}

// The confirmation window and the frame that opened it talk in these, on
// this app's own origin only (each side checks the origin and the window).
export const CONFIRM_PROTOCOL = "oe-confirm/v1";

export function isConfirmMessage(data: unknown, type: string, nonce: string): data is Record<string, unknown> {
  return (
    typeof data === "object" &&
    data !== null &&
    (data as Record<string, unknown>).protocol === CONFIRM_PROTOCOL &&
    (data as Record<string, unknown>).type === type &&
    (data as Record<string, unknown>).nonce === nonce
  );
}

export function parseAction(value: unknown): CardAction | null {
  if (typeof value !== "object" || value === null) return null;
  const a = value as Record<string, unknown>;
  if (!["approve", "reject", "cancel", "change"].includes(a.verb as string)) return null;
  if (typeof a.what !== "string" || a.what.length > 300) return null;
  const id = a.decisionId;
  if (id !== null && !(typeof id === "number" && Number.isInteger(id) && id > 0)) return null;
  return { verb: a.verb as CardAction["verb"], what: a.what, decisionId: id as number | null, card: a.card === true };
}

export class CardActionNotConfirmed extends Error {
  constructor() {
    super("Not confirmed, so nothing was done.");
    this.name = "CardActionNotConfirmed";
  }
}
