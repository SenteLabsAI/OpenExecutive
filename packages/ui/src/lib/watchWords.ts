/**
 * A watch's settings in plain words, and the "raise the bar" change the
 * noisy-watch question offers. The trigger words mirror trigger_line() in
 * packages/core/openexecutive/monitoring/describe.py (the draft the API
 * writes). Tested in scripts/watchWords.test.mjs.
 */

export type Severity = "low" | "medium" | "high" | "urgent";

const SEVERITIES: Severity[] = ["low", "medium", "high", "urgent"];

/** The adapter's own defaults when the trigger is empty. */
export const STOCK_DEFAULT_PCT = 5;
const EDGAR_DEFAULT_FORMS = ["8-K", "10-K", "10-Q"];

const FORM_NAMES: Record<string, string> = {
  "8-K": "8-K (major news)",
  "10-K": "10-K (annual report)",
  "10-Q": "10-Q (quarterly report)",
  "4": "Form 4 (insider trade)",
  "S-1": "S-1 (IPO filing)",
};

function joinOr(items: string[]): string {
  if (items.length <= 1) return items.join("");
  return `${items.slice(0, -1).join(", ")} or ${items[items.length - 1]}`;
}

function strings(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((v): v is string => typeof v === "string" && v.trim() !== "")
    : [];
}

function pctOf(trigger: Record<string, unknown>): number {
  const v = trigger.abs_change_pct_gte;
  return typeof v === "number" && Number.isFinite(v) && v > 0 ? v : STOCK_DEFAULT_PCT;
}

/** When a watch tells you, in words: "It moves 5% or more in a day, up or down". */
export function triggerInWords(signalType: string, trigger: Record<string, unknown> | null | undefined): string {
  const t = trigger ?? {};
  const keywords = strings(t.keywords);
  const quoted = joinOr(keywords.map((k) => `“${k}”`));
  const mention = keywords.length ? ` that mentions ${quoted}` : "";
  switch (signalType) {
    case "stock":
      return `It moves ${pctOf(t)}% or more in a day, up or down`;
    case "edgar": {
      const forms = strings(t.forms).map((f) => f.toUpperCase());
      const list = forms.length ? forms : EDGAR_DEFAULT_FORMS;
      return `They file a ${joinOr(list.map((f) => FORM_NAMES[f] ?? f))}`;
    }
    case "vendor_status":
      return `They post an incident or update${mention}`;
    case "rss":
      return `There's a new post${mention}`;
    case "page_watch":
      return `The page changes${keywords.length ? ` and mentions ${quoted}` : ""}`;
    case "query":
      return `A new result turns up${mention}`;
    default:
      return "Something new turns up";
  }
}

const CADENCE_WORDS: Record<string, string> = {
  real_time: "Right away",
  "15min": "About every 15 minutes",
  hourly: "About once an hour",
  daily: "Once a day",
  weekly: "Once a week",
};

export function cadenceInWords(cadence: string): string {
  return CADENCE_WORDS[cadence] ?? cadence;
}

const FLOOR_WORDS: Record<string, string> = {
  low: "Everything, even small changes",
  medium: "Only things that matter",
  high: "Only big things",
  urgent: "Only urgent things",
};

export function loudnessInWords(floor: string, ceiling: string): string {
  const line = FLOOR_WORDS[floor] ?? floor;
  return ceiling !== "urgent" && floor !== "urgent" ? `${line}, and never as urgent` : line;
}

export interface RaiseTheBar {
  /** PATCH body for /watchlist/{slug}. */
  patch: { trigger?: Record<string, unknown>; severity_floor?: Severity };
  /** Button text. */
  label: string;
  /** What changes, said after it is done. */
  done: string;
}

/**
 * The quieter version of a watch: a stock's move threshold doubled (whole
 * percent, at most 100), otherwise its severity floor one step up. Null when
 * it can't get any quieter.
 */
export function raiseTheBar(watch: {
  signal_type: string;
  trigger_json: Record<string, unknown> | null | undefined;
  severity_floor: string;
}): RaiseTheBar | null {
  const trigger = watch.trigger_json ?? {};
  if (watch.signal_type === "stock") {
    const now = pctOf(trigger);
    const next = Math.min(100, Math.max(Math.round(now * 2), Math.floor(now) + 1));
    if (next > now) {
      return {
        patch: { trigger: { ...trigger, abs_change_pct_gte: next } },
        label: `Only moves of ${next}%+`,
        done: `You'll only hear about moves of ${next}% or more.`,
      };
    }
  }
  const at = SEVERITIES.indexOf(watch.severity_floor as Severity);
  if (at < 0 || at >= SEVERITIES.length - 1) return null;
  const next = SEVERITIES[at + 1];
  return {
    patch: { severity_floor: next },
    label: FLOOR_WORDS[next],
    done: `You'll only hear about ${FLOOR_WORDS[next].replace(/^Only /, "").toLowerCase()} from this.`,
  };
}
