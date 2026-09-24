"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import {
  ARTIFACT_EXTENSIONS,
  ArtifactDetail,
  archiveArtifact,
  artifactDownloadUrl,
  deleteArtifact,
  getArtifact,
  restoreArtifact,
} from "@/lib/api";
import ArtifactViewer from "@/components/ArtifactViewer";
import Icon from "@/components/Icon";

// Seed for "Revise in chat": opens a fresh chat with the id pre-filled so the
// Executive can get_artifact → draft_artifact(supersedes=…).
function reviseHref(art: ArtifactDetail): string {
  const draft = `Revise artifact ${art.id} ("${art.title}"): `;
  return `/?new=1&draft=${encodeURIComponent(draft)}`;
}

function formatTimestamp(iso: string): string {
  return new Date(iso).toLocaleString();
}

export default function ArtifactDetailPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const id = params?.id ? decodeURIComponent(params.id) : undefined;
  const [art, setArt] = useState<ArtifactDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!id) return;
    getArtifact(id)
      .then(setArt)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, [id]);

  const handleCopy = useCallback(async () => {
    if (!art?.body) return;
    try {
      await navigator.clipboard.writeText(art.body);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // ignore — clipboard API may be unavailable
    }
  }, [art]);

  const handleToggleArchive = useCallback(async () => {
    if (!art) return;
    const archiving = !art.archived_at;
    setBusy(true);
    try {
      if (archiving) await archiveArtifact(art.id);
      else await restoreArtifact(art.id);
      // Reflect new state locally (timestamp is illustrative — the list is the
      // source of truth on next load).
      setArt({ ...art, archived_at: archiving ? new Date().toISOString() : null });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [art]);

  const handleDelete = useCallback(async () => {
    if (!art) return;
    if (
      !confirm(
        `Permanently delete "${art.title}"? This removes it everywhere and cannot be undone.`
      )
    )
      return;
    setBusy(true);
    try {
      await deleteArtifact(art.id);
      router.push("/artifacts");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  }, [art, router]);

  if (error) {
    return (
      <div className="flex flex-col h-full bg-surface text-fg items-center justify-center">
        <div className="text-sm text-red-400 mb-4">Error: {error}</div>
        <Link href="/artifacts" className="text-sm text-fg-muted hover:text-fg">
          ← Back to artifacts
        </Link>
      </div>
    );
  }

  if (!art) {
    return (
      <div className="flex flex-col h-full bg-surface text-fg-muted items-center justify-center text-sm">
        Loading…
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full bg-surface text-fg">
      <main className="flex-1 overflow-y-auto px-6 py-8">
        <div className="max-w-4xl mx-auto space-y-6">
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0">
              <h1 className="text-2xl font-semibold text-fg mb-1">{art.title}</h1>
              <div className="text-xs text-fg-muted">
                {art.format_label} · {art.source_label} · created{" "}
                {formatTimestamp(art.created_at)}
              </div>
              {art.supersedes_id && (
                <div className="text-xs text-fg-muted mt-1">
                  Replaces an{" "}
                  <Link
                    href={`/artifacts/${encodeURIComponent(art.supersedes_id)}`}
                    className="text-indigo-400 hover:underline"
                  >
                    earlier version
                  </Link>
                </div>
              )}
            </div>
            <div className="flex flex-wrap items-center justify-end gap-2 flex-shrink-0">
              <button
                type="button"
                onClick={handleCopy}
                className="text-xs text-fg-muted hover:text-fg transition px-3 py-1.5 rounded-md border border-line hover:bg-surface-overlay min-h-touch"
              >
                {copied ? "Copied!" : "Copy"}
              </button>
              <Link
                href={reviseHref(art)}
                className="text-xs text-indigo-300 hover:text-indigo-200 transition px-3 py-1.5 rounded-md border border-indigo-500/40 hover:bg-indigo-500/10 min-h-touch"
              >
                Revise in chat
              </Link>
              {art.downloads.map((target, i) => (
                <a
                  key={target}
                  href={artifactDownloadUrl(art.id, i === 0 ? undefined : target)}
                  download
                  className="text-xs text-fg-muted hover:text-fg transition px-3 py-1.5 rounded-md border border-line hover:bg-surface-overlay min-h-touch"
                >
                  Download .{ARTIFACT_EXTENSIONS[target]}
                </a>
              ))}
              <button
                type="button"
                onClick={handleToggleArchive}
                disabled={busy}
                className="inline-flex items-center gap-1.5 text-xs text-fg-muted hover:text-fg transition px-3 py-1.5 rounded-md border border-line hover:bg-surface-overlay min-h-touch disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer"
              >
                <Icon
                  name={art.archived_at ? "restore" : "archive"}
                  size="w-3.5 h-3.5"
                />
                {art.archived_at ? "Restore" : "Archive"}
              </button>
              <button
                type="button"
                onClick={handleDelete}
                disabled={busy}
                aria-label="Delete permanently"
                className="inline-flex items-center gap-1.5 text-xs text-fg-muted hover:text-red-400 transition px-3 py-1.5 rounded-md border border-line hover:bg-red-500/10 hover:border-red-500/30 min-h-touch disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer"
              >
                <Icon name="trash" size="w-3.5 h-3.5" />
                Delete
              </button>
            </div>
          </div>

          {art.rationale && (
            <div className="rounded-md border border-amber-500/30 bg-amber-500/5 px-4 py-3">
              <div className="text-xs font-medium text-amber-300 mb-1">
                Why this is worth your time
              </div>
              <div className="text-sm text-fg">{art.rationale}</div>
            </div>
          )}

          <ArtifactViewer
            format={art.format}
            body={art.body}
            title={art.title}
            externalUrl={art.external_url}
            linkLabel={art.link_label}
          />
        </div>
      </main>
    </div>
  );
}
