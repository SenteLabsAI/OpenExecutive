// Whether a button inside a frame was really seen when it was clicked
// (components/embed/FrameConfirm.tsx).
//
// A page that frames this app (a host origin; frame-ancestors lets no other
// site) can't read or script the frame, but it can place it, move it, make
// it invisible, cover it and lure clicks onto it. IntersectionObserver v2
// (`trackVisibility: true`, Chromium) answers, from inside the frame, whether
// an element is on screen uncovered, at full opacity, untransformed and
// unfiltered, and the page around can't fake that answer: the browser
// computes it. So a Yes in a frame counts only when:
//
// - the panel holding it has been reported whole on screen and visible,
//   without a break, for at least SEEN_MS. A lured second click on a hidden
//   frame fails this; so does a cover that lets clicks through
//   (`pointer-events: none`), and a frame cropped to show the Yes without
//   the words that say what it does;
// - the real pointer has been inside the panel, without leaving, for at
//   least DWELL_MS, and has moved there (a cursor drawn by the page
//   elsewhere doesn't move it). A frame slid under the pointer between the
//   two clicks of a lured double-click fails this: the pointer has only just
//   entered. Leaving the panel, or the frame losing focus, starts it again;
// - the press began on Yes itself after all of that held, and the click that
//   follows is trusted and on Yes too. A key press on a focused Yes has no
//   pointer press, so it never counts; the panel offers this app's own page
//   instead;
// - the panel is still seen, and the pointer still inside, SETTLE_MS after
//   the click. The browser reports a change at most every OBSERVE_DELAY_MS,
//   so a cover laid on just before the click shows up by then; reports not
//   yet delivered are taken at each look (`takeRecords`).
//
// A browser without v2 (Firefox, Safari) can't tell, so the frame never
// offers a Yes there: it sends the person to this app's own page, top level
// in the same tab, where nothing can frame it. Never fail open.
//
// The answer stays in the browser. It guards against code on the host's
// origin, which can't run in this one; the approval is still this app's own
// same-origin request with its usual checks.
//
// No imports, so `npm test` can exercise this with faked entries and events.

export const SEEN_MS = 700;
// Longer than a double-click's interval.
export const DWELL_MS = 500;
// The smallest delay trackVisibility accepts.
export const OBSERVE_DELAY_MS = 100;
// Two of the browser's reporting rounds, and a frame.
export const SETTLE_MS = 2 * OBSERVE_DELAY_MS + 50;

/** What the gate reads from an IntersectionObserverEntry. */
export type SeenEntry = { isIntersecting: boolean; intersectionRatio: number; isVisible?: boolean; time: number };

// Whole on screen, allowing for rounding.
const WHOLE = 0.99;

/** Whether this browser's IntersectionObserver can report visibility (v2). */
export function tracksVisibility(win: unknown): boolean {
  const w = win as { IntersectionObserver?: unknown; IntersectionObserverEntry?: { prototype?: object } } | null;
  if (!w || typeof w.IntersectionObserver !== "function") return false;
  const proto = w.IntersectionObserverEntry?.prototype;
  return typeof proto === "object" && proto !== null && "isVisible" in proto;
}

/** Times are on the observer's clock (`performance.now()`). */
export class SeenGate {
  // When the panel was last reported visible after a break, or null.
  private visibleSince: number | null = null;
  // When the pointer last came into the panel, or null while it's outside.
  private insideSince: number | null = null;
  // Whether it has moved in the panel, while seen, since it came in.
  private moved = false;
  // When a press on Yes began while armed, and the run it began in.
  private pressedAt: number | null = null;
  private pressRun: number | null = null;

  /** An observer entry for the panel: anything short of whole and visible breaks the run. */
  observe(entry: SeenEntry): void {
    if (entry.isIntersecting && entry.intersectionRatio >= WHOLE && entry.isVisible === true) {
      if (this.visibleSince === null) this.visibleSince = entry.time;
    } else {
      this.visibleSince = null;
      this.insideSince = null;
      this.moved = false;
      this.pressedAt = null;
    }
  }

  /** The pointer came into the panel (`pointerenter`), or moved in it. */
  pointerInside(isTrusted: boolean, now: number, moving: boolean): void {
    if (!isTrusted) return;
    if (this.insideSince === null) this.insideSince = now;
    if (moving && this.visibleSince !== null) this.moved = true;
  }

  /** The pointer left the panel, or the frame lost focus: start again. */
  pointerGone(): void {
    this.insideSince = null;
    this.moved = false;
    this.pressedAt = null;
  }

  /** Whether the panel has been visible for long enough. */
  seenFor(now: number): boolean {
    return this.visibleSince !== null && now - this.visibleSince >= SEEN_MS;
  }

  /** Whether a press on Yes would count now. */
  armed(now: number): boolean {
    return this.moved && this.seenFor(now) && this.insideSince !== null && now - this.insideSince >= DWELL_MS;
  }

  /** A press began (`pointerdown`): it counts only on Yes, trusted, while armed. */
  pressed(press: { isTrusted: boolean; onYes: boolean; now: number }): void {
    const counts = press.isTrusted && press.onYes && this.armed(press.now);
    this.pressedAt = counts ? press.now : null;
    this.pressRun = counts ? this.visibleSince : null;
  }

  /** Whether a click counts: trusted, on Yes, after a press that counted, still armed. */
  clickCounts(click: { isTrusted: boolean; onYes: boolean; now: number }): boolean {
    return click.isTrusted && click.onYes && this.pressedAt !== null && this.armed(click.now);
  }

  /** After SETTLE_MS: the same run of "seen" as at the press, and the pointer still in. */
  stillSeen(now: number): boolean {
    return this.pressRun !== null && this.visibleSince === this.pressRun && this.armed(now);
  }

  /** A press counts for one click at most; one released off Yes counts for none. */
  pressUsed(): void {
    this.pressedAt = null;
    this.pressRun = null;
  }
}

type Observer = { observe(el: unknown): void; disconnect(): void; takeRecords(): SeenEntry[] };
type ObserverCtor = new (
  cb: (entries: SeenEntry[]) => void,
  options: { threshold: number[]; trackVisibility: boolean; delay: number },
) => Observer;
type Listener = (event: PointerLike) => void;
type Target = {
  addEventListener(type: string, listener: Listener): void;
  removeEventListener(type: string, listener: Listener): void;
};
type PointerLike = { isTrusted: boolean; target: unknown };
type Win = {
  IntersectionObserver: ObserverCtor;
  performance: { now(): number };
  setTimeout(fn: () => void, ms: number): unknown;
} & Target;

export type Watch = {
  /** Whether `armed` now (for showing Yes as ready). */
  armed: () => boolean;
  /** Judge a click on Yes: resolves true only if it counts and still holds SETTLE_MS later. */
  judge: (click: PointerLike) => Promise<boolean>;
  stop: () => void;
};

/**
 * Watch `panel` with v2 and the pointer, feeding `gate`; `changed` is called
 * on every report. `isYes` says whether an event's target is the Yes button.
 * Null where v2 is missing: then nothing can arm.
 */
export function watchPanel(
  win: unknown,
  panel: Target,
  isYes: (target: unknown) => boolean,
  gate: SeenGate,
  changed: () => void,
): Watch | null {
  if (!tracksVisibility(win)) return null;
  const w = win as Win;
  const now = () => w.performance.now();
  const feed = (entries: SeenEntry[]) => {
    for (const entry of entries) gate.observe(entry);
    changed();
  };
  const observer = new w.IntersectionObserver(feed, { threshold: [1.0], trackVisibility: true, delay: OBSERVE_DELAY_MS });
  observer.observe(panel);
  const flush = () => feed(observer.takeRecords());

  const onEnter = (e: PointerLike) => gate.pointerInside(e.isTrusted, now(), false);
  const onMove = (e: PointerLike) => gate.pointerInside(e.isTrusted, now(), true);
  const onGone = () => {
    gate.pointerGone();
    changed();
  };
  const onDown = (e: PointerLike) => {
    flush();
    gate.pressed({ isTrusted: e.isTrusted, onYes: isYes(e.target), now: now() });
  };
  const onUp = (e: PointerLike) => {
    if (!isYes(e.target)) gate.pressUsed();
  };
  const panelEvents: [string, Listener][] = [
    ["pointerenter", onEnter],
    ["pointermove", onMove],
    ["pointerleave", onGone],
    ["pointercancel", onGone],
    ["pointerdown", onDown],
    ["pointerup", onUp],
  ];
  for (const [type, fn] of panelEvents) panel.addEventListener(type, fn);
  w.addEventListener("blur", onGone);

  return {
    armed: () => gate.armed(now()),
    judge: async (click) => {
      flush();
      if (!gate.clickCounts({ isTrusted: click.isTrusted, onYes: isYes(click.target), now: now() })) return false;
      await new Promise<void>((resolve) => w.setTimeout(resolve, SETTLE_MS));
      flush();
      const still = gate.stillSeen(now());
      gate.pressUsed();
      return still;
    },
    stop: () => {
      observer.disconnect();
      for (const [type, fn] of panelEvents) panel.removeEventListener(type, fn);
      w.removeEventListener("blur", onGone);
    },
  };
}
