import assert from "node:assert/strict";
import test from "node:test";
import { suggestWatchSlug } from "../src/lib/watchSlug.ts";

const VALID = /^[a-z0-9][a-z0-9-]{0,60}$/;

test("suggestWatchSlug names a monitor from its type and target", () => {
  assert.equal(suggestWatchSlug("stock", "AAPL"), "stock-aapl");
  assert.equal(suggestWatchSlug("edgar", "320193"), "edgar-320193");
  assert.equal(
    suggestWatchSlug("rss", "https://www.example.com/blog/feed.xml"),
    "rss-example-com-blog-feed-xml",
  );
  assert.equal(suggestWatchSlug("query", "Acme Corp layoffs OR funding"), "search-acme-corp-layoffs-or-funding");
  assert.equal(suggestWatchSlug("vendor_status", "https://status.vendor.io/history.atom"), "status-status-vendor-io-history-atom");
});

test("suggestWatchSlug is empty without a target", () => {
  assert.equal(suggestWatchSlug("stock", "   "), "");
});

test("suggestWatchSlug always fits the API's slug rule", () => {
  const long = suggestWatchSlug("page_watch", "https://example.com/" + "pricing-page/".repeat(20));
  assert.ok(long.length <= 61);
  assert.match(long, VALID);
  assert.ok(!long.endsWith("-"));
  assert.match(suggestWatchSlug("Custom_Kind", "Ünïcode — target!"), VALID);
});
