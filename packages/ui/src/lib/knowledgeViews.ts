// The Knowledge page's views: Company documents up front, and four views
// behind "Advanced" (review queue, built-in playbooks, reference library,
// query test). Kept apart from the components so `npm test` can check the
// mapping (scripts/knowledgeViews.test.mjs).

export type KnowledgeView = "company" | "review" | "playbooks" | "reference" | "query";

/** The views under "Advanced", in tab order. */
export const ADVANCED_VIEWS: readonly KnowledgeView[] = ["review", "playbooks", "reference", "query"];

export const VIEW_LABELS: Record<KnowledgeView, string> = {
  company: "Company documents",
  review: "Review queue",
  playbooks: "Built-in playbooks",
  reference: "Reference library",
  query: "Query test",
};

/** What the workspace has open. A built-in file and the new-file form sit
 * under the playbooks view. */
export type SelectionKind = "file" | "new" | "playbooks" | "company" | "reference" | "query" | "review";

export function viewForSelection(kind: SelectionKind | undefined | null): KnowledgeView {
  switch (kind) {
    case "file":
    case "new":
    case "playbooks":
      return "playbooks";
    case "review":
    case "reference":
    case "query":
      return kind;
    default:
      return "company";
  }
}

export function isAdvancedView(view: KnowledgeView): boolean {
  return view !== "company";
}

/** The view a `?view=` value opens: `/knowledge?view=review` (and the old
 * `/review` route) opens the review queue; anything unknown opens the
 * company documents. */
export function viewFromParam(param: string | null | undefined): KnowledgeView {
  return param && (ADVANCED_VIEWS as readonly string[]).includes(param)
    ? (param as KnowledgeView)
    : "company";
}
