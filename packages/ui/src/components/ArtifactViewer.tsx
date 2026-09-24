"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import type { ArtifactFormat } from "@/lib/api";

// Blocks every network fetch and all script from inside an HTML artifact.
// The iframe sandbox (no allow-scripts, no allow-same-origin) is the real
// boundary; the CSP also stops remote images / fonts / CSS, so opening an
// artifact can't beacon out (e.g. a tracking pixel from quoted web content).
const HTML_CSP =
  "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:";

function withCsp(html: string): string {
  return `<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${HTML_CSP}">${html}`;
}

function hostOf(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return url;
  }
}

export function MarkdownArticle({ markdown }: { markdown: string }) {
  return (
    <article
      className="prose prose-invert prose-sm max-w-none rounded-lg border border-line bg-surface/40 p-6
        prose-headings:text-fg prose-headings:font-semibold
        prose-p:text-fg prose-p:leading-relaxed
        prose-strong:text-fg
        prose-code:text-indigo-300 prose-code:bg-surface-overlay prose-code:px-1.5 prose-code:py-0.5 prose-code:rounded prose-code:before:content-none prose-code:after:content-none
        prose-pre:bg-surface-overlay prose-pre:border prose-pre:border-line-strong
        prose-blockquote:border-line-strong prose-blockquote:text-fg-muted
        prose-ul:text-fg prose-ol:text-fg
        prose-li:marker:text-fg-muted
        prose-hr:border-line-strong
        prose-a:text-indigo-400 prose-a:no-underline hover:prose-a:underline
        prose-table:text-fg prose-th:text-fg prose-th:border-line-strong prose-td:border-line-strong"
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ node: _node, ...props }) => (
            <a {...props} rel="noopener noreferrer nofollow" target="_blank" />
          ),
        }}
      >
        {markdown}
      </ReactMarkdown>
    </article>
  );
}

interface Props {
  format: ArtifactFormat;
  body: string;
  title: string;
  externalUrl?: string | null;
  linkLabel?: string | null;
}

// Renders an artifact body by format. The API sends sanitized HTML for
// "html" and Markdown for everything else (a spreadsheet arrives as tables,
// a Word doc as its Markdown source, a link as its summary).
export default function ArtifactViewer({ format, body, title, externalUrl, linkLabel }: Props) {
  if (format === "html") {
    return (
      <iframe
        title={title}
        sandbox=""
        referrerPolicy="no-referrer"
        srcDoc={withCsp(body)}
        className="w-full h-[75vh] rounded-lg border border-line bg-white"
      />
    );
  }

  if (format === "link") {
    return (
      <div className="space-y-4">
        {externalUrl && (
          <a
            href={externalUrl}
            target="_blank"
            rel="noopener noreferrer nofollow"
            className="inline-flex items-center gap-2 rounded-md border border-indigo-500/40 bg-indigo-500/10 px-4 py-2 text-sm text-indigo-300 hover:bg-indigo-500/20"
          >
            Open {linkLabel || "link"} ↗
            <span className="text-xs text-fg-muted">({hostOf(externalUrl)})</span>
          </a>
        )}
        {body && <MarkdownArticle markdown={body} />}
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {format === "xlsx" && (
        <div className="text-xs text-fg-muted">
          Preview of the workbook. Download it for every row and native Excel formatting.
        </div>
      )}
      {format === "docx" && (
        <div className="text-xs text-fg-muted">
          Preview of the Word document. Download it for the .docx file.
        </div>
      )}
      <MarkdownArticle markdown={body} />
    </div>
  );
}
