'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import dynamic from 'next/dynamic';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeSanitize, { defaultSchema } from 'rehype-sanitize';

// Sanitizer for the section Markdown. Even though the content is
// authored and version-controlled, we keep defence-in-depth: tighten the
// default react-markdown safe schema to drop the `img` tag and restrict
// link targets to http(s) or fragment-only.
const SAFE_SCHEMA = {
  ...defaultSchema,
  tagNames: (defaultSchema.tagNames ?? []).filter((t) => t !== 'img'),
  attributes: {
    ...defaultSchema.attributes,
    a: [
      ['href', /^(https?:|#|\/)/],
      'title',
      ['target', '_blank'],
      ['rel', 'noopener', 'noreferrer'],
    ],
  },
};

const MermaidDiagram = dynamic(() => import('./MermaidDiagram'), { ssr: false });

interface SectionContent {
  section_id: string;
  markdown: string;
  mermaid: string | null;
  generated_at: string;
}

interface Props {
  id: string;
  title: string;
  sub: string;
  /**
   * Backend resource these sections are served from. Defaults to
   * `architecture`; the user guide passes `guide`. Both expose the same
   * `/{basePath}/sections/{id}` shape.
   */
  basePath?: string;
}

type Status = 'idle' | 'loading' | 'ready' | 'error';

export default function DynamicSection({ id, title, sub, basePath = 'architecture' }: Props) {
  const [content, setContent] = useState<SectionContent | null>(null);
  const [status, setStatus] = useState<Status>('idle');
  const [error, setError] = useState<string | null>(null);
  // Last source we fetched, keyed on basePath+id. Dedupes the dev
  // StrictMode double-invoke while still re-fetching if the source
  // changes in place (e.g. basePath flips between architecture/guide).
  const loadedKeyRef = useRef<string | null>(null);

  const load = useCallback(async () => {
    setStatus('loading');
    setError(null);
    try {
      const res = await fetch(`/api/backend/${basePath}/sections/${id}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: SectionContent = await res.json();
      setContent(data);
      setStatus('ready');
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setStatus('error');
    }
  }, [id, basePath]);

  const sectionRef = useRef<HTMLElement | null>(null);

  // Fetch only when the section enters (or nears) the viewport — the
  // architecture/guide pages mount every section at once, so an eager
  // fetch was ~20 parallel API calls on first paint. Re-fetches if the
  // source (basePath/id) changes; the key guard skips the redundant
  // second call React fires in StrictMode.
  useEffect(() => {
    const key = `${basePath}/${id}`;
    const el = sectionRef.current;
    if (!el) return;

    const start = () => {
      if (loadedKeyRef.current === key) return;
      loadedKeyRef.current = key;
      load();
    };

    if (typeof IntersectionObserver === 'undefined') {
      start();
      return;
    }

    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          start();
          observer.disconnect();
        }
      },
      { rootMargin: '200px 0px' },
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [load, basePath, id]);

  return (
    <section ref={sectionRef} className="space-y-4">
      <div className="mb-6 flex items-start justify-between gap-4" id={id}>
        <div>
          <h2 className="text-lg font-semibold text-fg">{title}</h2>
          <p className="text-sm text-fg-muted mt-1">{sub}</p>
        </div>
      </div>

      {status === 'loading' && !content && (
        <div className="rounded-lg bg-surface border border-line px-4 py-8 text-center text-xs text-fg-muted animate-pulse">
          Loading…
        </div>
      )}

      {status === 'error' && (
        <div className="rounded-lg bg-red-950/40 border border-red-900 px-4 py-3 text-xs text-red-300">
          <div className="font-medium mb-1">Failed to load</div>
          <div className="font-mono">{error}</div>
          <button
            type="button"
            onClick={load}
            className="mt-2 underline text-red-200 hover:text-red-100"
          >
            Retry
          </button>
        </div>
      )}

      {content && content.mermaid && (
        <MermaidDiagram id={id} definition={content.mermaid} />
      )}

      {content && (
        <div className="prose prose-invert prose-sm max-w-none prose-headings:text-fg prose-p:text-fg prose-li:text-fg prose-code:text-amber-300 prose-code:bg-surface-elevated prose-code:rounded prose-code:px-1 prose-code:py-0.5 prose-code:before:content-none prose-code:after:content-none prose-table:text-xs prose-th:text-fg-muted prose-td:text-fg-muted prose-a:text-indigo-400">
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            rehypePlugins={[[rehypeSanitize, SAFE_SCHEMA]]}
          >
            {content.markdown}
          </ReactMarkdown>
        </div>
      )}
    </section>
  );
}
