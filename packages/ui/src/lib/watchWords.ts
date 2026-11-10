/**
 * When a watch tells you, in plain words, and the "raise the bar" change the
 * noisy-watch question offers. The trigger words mirror trigger_line() in
 * packages/core/openexecutive/monitoring/describe.py (the draft the API
 * writes). Tested in scripts/watchWords.test.mjs.
 */

/** What kind of thing a watch is, as the screens name it. */
export const KIND_LABELS: Record<string, string> = {
  stock: "Stock",
  rss: "News",
  vendor_status: "Vendor status",
  edgar: "SEC filings",
  page_watch: "Page change",
  query: "Web search",
};

/** The filing types the "Tells you when" editor offers, with what each is. */
export const FILING_CHOICES: { value: string; label: string }[] = [
  { value: "8-K", label: "8-K (major news)" },
  { value: "10-K", label: "10-K (annual report)" },
  { value: "10-Q", label: "10-Q (quarterly report)" },
  { value: "4", label: "Form 4 (insider trade)" },
  { value: "S-1", label: "S-1 (IPO filing)" },
];

/** The adapter's own defaults when the trigger is empty. */
export const STOCK_DEFAULT_PCT = 5;
export const EDGAR_DEFAULT_FORMS = ["8-K", "10-K", "10-Q"];

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
  const line = triggerBase(signalType, t);
  return t.only_big === true && signalType !== "stock" ? `${line}, and only when it is a big change` : line;
}

function triggerBase(signalType: string, t: Record<string, unknown>): string {
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

/** How often to check, as the by-hand form offers it. */
export const CADENCE_CHOICES: { value: string; label: string }[] = [
  { value: "real_time", label: "As often as it can" },
  { value: "15min", label: "Every 15 minutes" },
  { value: "hourly", label: "Every hour" },
  { value: "daily", label: "Once a day" },
  { value: "weekly", label: "Once a week" },
];

export interface RaiseTheBar {
  /** PATCH body for /watchlist/{slug}. */
  patch: { trigger: Record<string, unknown> };
  /** Button text. */
  label: string;
  /** What changes, said after it is done. */
  done: string;
}

/**
 * The quieter version of a watch: a stock's move threshold doubled (whole
 * percent, at most 100), otherwise big changes only. Null when it can't get
 * any quieter. Either way the change is to the trigger, so "Tells you when"
 * says it.
 */
export function raiseTheBar(watch: {
  signal_type: string;
  trigger_json: Record<string, unknown> | null | undefined;
}): RaiseTheBar | null {
  const trigger = watch.trigger_json ?? {};
  if (watch.signal_type === "stock") {
    const now = pctOf(trigger);
    const next = Math.min(100, Math.max(Math.round(now * 2), Math.floor(now) + 1));
    if (next <= now) return null;
    return {
      patch: { trigger: { ...trigger, abs_change_pct_gte: next } },
      label: `Only moves of ${next}%+`,
      done: `You'll only hear about moves of ${next}% or more.`,
    };
  }
  if (trigger.only_big === true) return null;
  return {
    patch: { trigger: { ...trigger, only_big: true } },
    label: "Only big changes",
    done: "You'll only hear about big changes from this.",
  };
}
