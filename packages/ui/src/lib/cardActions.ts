// In a frame, what the page changes beyond the host list asks first
// (components/embed/EmbedProvider.tsx, FrameConfirm.tsx).
//
// A page that frames this one (a host origin, or any page frame-ancestors
// lets in) can't read or click inside the frame, but it can make the frame
// invisible, lay its own buttons over it and catch clicks meant for those.
// So in a frame, approving, rejecting or cancelling a card, sending a chat
// message as the person, and any other change the host list doesn't already
// let the host make itself (a setting, Handle it for me, People) that follows
// a click, first asks inside the frame, naming what will happen; its Yes
// counts only once the browser has reported the question seen (lib/seen.ts).
// A browser that can't report it gets no Yes: the person goes to `page`, this
// app's own, top level. Outside a frame nothing changes.
//
// The embedded shell checks every request the page sends against
// `frameActionFor`, so no call site can forget.
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
  // A card action always asks in a frame. Any other change only when it
  // follows a click (it could have been lured); one the page makes on its
  // own can't have been.
  card: boolean;
  // The card's own page, where a browser that can't tell the frame is seen
  // answers it instead (null: the page that asked, opened on its own).
  page: string | null;
};

type HostCall = readonly [method: string, pattern: string];

const BACKEND = "/api/backend";
const READS = new Set(["GET", "HEAD", "OPTIONS"]);
// Chat messages are on the host list, but only a host's own are read-only:
// sent from this app's page in a frame, a turn is the person's own, with
// every tool. So in a frame they are refused like a card action.
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
    return { verb, decisionId: id, what, card: true, page: `/cards/${id}` };
  }
  m = /^\/alerts\/(\d+)\/approve$/.exec(path);
  if (m) {
    return {
      verb: "approve",
      decisionId: null,
      what: `Approve the proposal on alert ${m[1]}: what it proposes is carried out now, as you.`,
      card: true,
      page: null,
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
      page: null,
    };
  }
  m = /^\/people\/requests\/(\d+)\/(approve|decline)$/.exec(path);
  if (m) {
    return m[2] === "approve"
      ? { verb: "approve", decisionId: null, what: `Add the person in request ${m[1]} to People, as you answered it.`, card: true, page: null }
      : { verb: "reject", decisionId: null, what: `Decline request ${m[1]}: they are not added to People.`, card: true, page: null };
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
 * What a request from a framed page would change, or null when it may go
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
      // Always: a queued message or Try again may carry words that weren't
      // typed here.
      card: true,
      page: null,
    };
  }
  if (onHostList(hostCalls, verb, path)) return null;
  const named = NAMED_CHANGES.find(([re]) => re.test(path));
  return {
    verb: "change",
    decisionId: null,
    what: `${named ? named[1] : "Make this change in Open Executive, as you."} (${verb} ${path.slice(0, 120)})`,
    card: false,
    page: null,
  };
}

/**
 * Where a browser that can't tell the frame is seen answers `action`: the
 * card's own page, or else the page that asked (`pathname` + `search`), in
 * both cases without `embed`, so it opens as this app's own page.
 */
export function ownPageFor(action: CardAction, pathname: string, search: string): string {
  if (action.page) return action.page;
  const params = new URLSearchParams(search);
  params.delete("embed");
  const query = params.toString();
  const path = pathname.startsWith("/") && !pathname.startsWith("//") ? pathname : "/";
  return query ? `${path}?${query}` : path;
}

export class CardActionNotConfirmed extends Error {
  constructor() {
    super("Not confirmed, so nothing was done.");
    this.name = "CardActionNotConfirmed";
  }
}
