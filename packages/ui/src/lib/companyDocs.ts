/**
 * The knowledge page's single list of company documents: files uploaded in
 * the app plus files the Google Drive and Notion syncs pulled in. Pure, so
 * the merge, ordering and labels are unit-tested (scripts/companyDocs.test.mjs).
 */

export type DocSource = "upload" | "drive" | "notion";

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
}

export const SOURCE_LABELS: Record<DocSource, string> = {
  upload: "Uploaded",
  drive: "Google Drive",
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
    })),
    ...[
      ["drive", drive] as const,
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
