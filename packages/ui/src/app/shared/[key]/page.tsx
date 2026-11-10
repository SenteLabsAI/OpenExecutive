import type { Metadata } from "next";
import { notFound } from "next/navigation";

import ArtifactViewer from "@/components/ArtifactViewer";
import type { ArtifactFormat } from "@/lib/api";

// A document someone shared by link (Documents → Share by link): a read-only
// page anyone with the link opens without signing in. The middleware lets
// exactly `/shared/<key>` through with no session (lib/middlewarePaths.ts),
// and the shell leaves its chrome off (AppShell EXEMPT_PREFIXES).
//
// The page asks the API itself, server to server, so the browser never
// reaches the API: `GET /shared-documents/{key}` answers the title, kind and
// text of that one document, or 404 for anything but a live link
// (api/routes/artifacts.py, orchestrator/artifact_shares.py). Nothing else
// about the company is on the page, and search engines are told to leave it
// out.

export const dynamic = "force-dynamic";

const BACKEND_BASE = process.env.BACKEND_BASE_URL ?? "http://localhost:8000";
const BACKEND_SHARED_SECRET = process.env.BACKEND_SHARED_SECRET ?? "";
// The shape the API mints (artifact_shares.KEY_PATTERN); anything else is
// not found without asking.
const KEY = /^[A-Za-z0-9_-]{32}$/;

interface SharedDocument {
  title: string;
  format: ArtifactFormat;
  format_label: string;
  created_at: string;
  body: string;
}

async function loadShared(key: string): Promise<SharedDocument | null> {
  if (!KEY.test(key)) return null;
  const headers: Record<string, string> = {};
  if (BACKEND_SHARED_SECRET) headers["x-api-key"] = BACKEND_SHARED_SECRET;
  try {
    const res = await fetch(`${BACKEND_BASE}/shared-documents/${key}`, {
      headers,
      cache: "no-store",
    });
    if (!res.ok) return null;
    return (await res.json()) as SharedDocument;
  } catch {
    return null;
  }
}

export const metadata: Metadata = {
  title: "Shared document",
  robots: { index: false, follow: false, nocache: true },
  referrer: "no-referrer",
};

export default async function SharedDocumentPage({
  params,
}: {
  params: Promise<{ key: string }>;
}) {
  const { key } = await params;
  const doc = await loadShared(key);
  if (!doc) notFound();
  const created = new Date(doc.created_at).toLocaleDateString("en-US", {
    year: "numeric",
    month: "long",
    day: "numeric",
  });
  return (
    <div className="min-h-full bg-surface text-fg">
      <main className="mx-auto max-w-4xl px-4 sm:px-6 py-8 sm:py-12 space-y-6">
        <header className="min-w-0">
          <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-fg mb-2 break-words">
            {doc.title}
          </h1>
          <p className="text-sm text-fg-muted">
            {doc.format_label} · {created} · shared with you by link
          </p>
        </header>
        <ArtifactViewer format={doc.format} body={doc.body} title={doc.title} />
      </main>
    </div>
  );
}
