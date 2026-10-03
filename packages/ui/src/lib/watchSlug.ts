/**
 * A monitor's slug when the person adding it leaves the field empty: the
 * signal type and target, kebab-cased to the rule the API enforces
 * (`[a-z0-9][a-z0-9-]{0,60}`). Tested in scripts/watchSlug.test.mjs.
 */

const PREFIX: Record<string, string> = {
  stock: "stock",
  rss: "rss",
  vendor_status: "status",
  edgar: "edgar",
  page_watch: "page",
  query: "search",
};

const MAX_LEN = 61;

export function suggestWatchSlug(signalType: string, target: string): string {
  let t = target.trim().toLowerCase();
  if (!t) return "";
  // A URL is named by its host and path, not its scheme or "www.".
  t = t.replace(/^[a-z][a-z0-9+.-]*:\/\//, "").replace(/^www\./, "");
  const prefix = PREFIX[signalType] ?? signalType.toLowerCase().replace(/[^a-z0-9]+/g, "-");
  const body = t.replace(/[^a-z0-9]+/g, "-");
  const slug = `${prefix}-${body}`.replace(/-+/g, "-").replace(/^-+/, "");
  return slug.slice(0, MAX_LEN).replace(/-+$/, "");
}
