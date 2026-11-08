/**
 * The knowledge page's single list of company documents: files uploaded in
 * the app plus files the Google Drive, OneDrive and Notion syncs pulled in. Pure, so
 * the merge, ordering and labels are unit-tested (scripts/companyDocs.test.mjs).
 */

export type DocSource = "upload" | "drive" | "onedrive" | "notion";

export interface UploadedDocIn {
  filename: string;
  size_bytes: number;
  /** Unix seconds (file mtime). */
  modified_at: number;
  domain?: string | null;
}

export interface SyncedDocIn {
  id: string;
  name: string;
  url: string | null;
  /** When it last changed in Drive / Notion (ISO), if known. */
  modified_at: string | null;
  /** When the knowledge base last re-read it (ISO). */
  synced_at: string | null;
  /** False when the file had no readable text, so nothing was indexed. */
  indexed: boolean;
  /** Why it had no readable text, when the sync knows. */
  unreadable_reason?: string | null;
}

export interface DocRow {
  key: string;
  source: DocSource;
  /** Upload: the filename. Drive / Notion: the file or page id. */
  ref: string;
  name: string;
  url: string | null;
  sizeBytes: number | null;
  /** Upload: when it was added. Drive / Notion: when it was last synced. */
  addedAt: string | null;
  editedAt: string | null;
  domain: string | null;
  indexed: boolean;
  /** Why a synced file has no readable text, when known. */
  unreadableReason: string | null;
}

export const SOURCE_LABELS: Record<DocSource, string> = {
  upload: "Uploaded",
  drive: "Google Drive",
  onedrive: "OneDrive",
  notion: "Notion",
};

/** "general" is what an untagged upload gets, so it is not worth a tag. */
function shownDomain(domain: string | null | undefined): string | null {
  return domain && domain !== "general" ? domain : null;
}

export function mergeDocs(
  uploads: UploadedDocIn[],
  drive: SyncedDocIn[],
  notion: SyncedDocIn[],
  onedrive: SyncedDocIn[] = [],
): DocRow[] {
  const rows: DocRow[] = [
    ...uploads.map((d) => ({
      key: `upload:${d.filename}`,
      source: "upload" as const,
      ref: d.filename,
      name: d.filename,
      url: null,
      sizeBytes: d.size_bytes,
      addedAt: new Date(d.modified_at * 1000).toISOString(),
      editedAt: null,
      domain: shownDomain(d.domain),
      indexed: true,
      unreadableReason: null,
    })),
    ...[
      ["drive", drive] as const,
      ["onedrive", onedrive] as const,
      ["notion", notion] as const,
    ].flatMap(([source, files]) =>
      files.map((f) => ({
        key: `${source}:${f.id}`,
        source,
        ref: f.id,
        name: f.name,
        url: f.url,
        sizeBytes: null,
        addedAt: f.synced_at,
        editedAt: f.modified_at,
        domain: null,
        indexed: f.indexed,
        unreadableReason: f.indexed ? null : (f.unreadable_reason ?? null),
      })),
    ),
  ];
  // Most recently added or edited first; ties (and rows with no date) by name.
  const when = (r: DocRow) =>
    Math.max(Date.parse(r.editedAt ?? "") || 0, Date.parse(r.addedAt ?? "") || 0);
  return rows.sort((a, b) => when(b) - when(a) || a.name.localeCompare(b.name));
}

export function filterDocs(rows: DocRow[], source: DocSource | "all", query: string): DocRow[] {
  const q = query.trim().toLowerCase();
  return rows.filter(
    (r) => (source === "all" || r.source === source) && (!q || r.name.toLowerCase().includes(q)),
  );
}

export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** "every 60 min" / "every 2 h" — how often a connected source re-syncs. */
export function formatInterval(minutes: number): string {
  if (minutes % 60 === 0 && minutes >= 60) {
    const h = minutes / 60;
    return h === 1 ? "every hour" : `every ${h} hours`;
  }
  return `every ${minutes} min`;
}

/** The bit of a DOM node the drop rules need, so they test without a DOM. */
export interface NodeLike {
  contains(other: unknown): boolean;
}

/**
 * Files can be dropped anywhere on the documents panel. React bubbles events
 * from a portal to this panel too, so a drop counts only when its target is
 * inside the panel's own DOM. (The side panels are rendered outside it.)
 */
export function dropLandsOnPanel(panel: NodeLike, target: unknown): boolean {
  return target != null && panel.contains(target);
}

/** A drag leaves the panel only when the pointer goes outside it, not when it crosses a child. */
export function dragLeavesPanel(panel: NodeLike, relatedTarget: unknown): boolean {
  return relatedTarget == null || !panel.contains(relatedTarget);
}

/** One upload batch at a time: a drop while files are still being added is ignored. */
export function acceptsDrop(uploading: boolean, fileCount: number): boolean {
  return !uploading && fileCount > 0;
}

/** The big "Add your first documents" box: only with no documents, and not mid-upload. */
export function showEmptyDropBox({
  loaded,
  count,
  uploading,
  canAdd = true,
}: {
  loaded: boolean;
  count: number;
  uploading: boolean;
  canAdd?: boolean;
}): boolean {
  return loaded && count === 0 && !uploading && canAdd;
}
