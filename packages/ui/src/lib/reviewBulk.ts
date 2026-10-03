// The review queue's per-domain bulk actions (approve what's queued, or start
// or stop curating a domain), worked out the way the server would act on them.
// Kept apart from ReviewQueue so `npm test` can check it
// (scripts/reviewBulk.test.mjs).

export interface BulkPendingItem {
  domain: string;
  trusted_default?: boolean;
  reviewed_at?: string | null;
}

export type BulkActionKind = "approve" | "curate-start" | "curate-stop";

export interface BulkAction {
  kind: BulkActionKind;
  domain: string;
  /** Items the action touches. */
  count: number;
  label: string;
  /** Longer explanation, for a hint under the menu. */
  detail: string;
}

/** The actions available for one domain. `pending` must be the unfiltered
 * pending list and `trustedDefaults` the shipped-and-unreviewed count per
 * domain, so a status filter on the list can't hide an action. */
export function domainBulkActions(
  domain: string,
  pending: BulkPendingItem[],
  trustedDefaults: Record<string, number>,
): BulkAction[] {
  const actions: BulkAction[] = [];
  const pendingInDomain = pending.filter((i) => i.domain === domain);
  if (pendingInDomain.length > 0) {
    actions.push({
      kind: "approve",
      domain,
      count: pendingInDomain.length,
      label: `Approve all pending in ${domain} (${pendingInDomain.length})`,
      detail: `Approve the ${pendingInDomain.length} pending item(s) in ${domain}.`,
    });
  }
  // "Stop curating" reverses queue_for_curation, whose selector is
  // `trusted_default = 1 AND reviewed_at IS NULL`. Mirror BOTH: a user's own
  // upload is pending with no timestamp too, and counting it would offer an
  // action that does nothing.
  const untouchedPending = pendingInDomain.filter(
    (i) => i.trusted_default && i.reviewed_at == null,
  ).length;
  const defaults = trustedDefaults[domain] ?? 0;
  if (untouchedPending > 0) {
    actions.push({
      kind: "curate-stop",
      domain,
      count: untouchedPending,
      label: `Stop curating ${domain}`,
      detail: `Return ${untouchedPending} unreviewed item(s) in ${domain} to trusted default.`,
    });
  } else if (defaults > 0) {
    actions.push({
      kind: "curate-start",
      domain,
      count: defaults,
      label: `Curate ${domain} (${defaults})`,
      detail: `Send ${defaults} shipped item(s) in ${domain} for review — they will be withheld from the Executive until approved.`,
    });
  }
  return actions;
}
