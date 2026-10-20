"use client";

import Link from "next/link";
import { useId, useState } from "react";

import Icon from "@/components/Icon";
import {
  answerSourcesFrom,
  groupSources,
  missingNote,
  sourceLink,
  sourceSite,
  type AnswerSource,
  type AnswerSources,
} from "@/lib/answerSources";

function SourceItem({ source }: { source: AnswerSource }) {
  const link = sourceLink(source.url);
  const site = sourceSite(source.url);
  if (link?.external) {
    return (
      <a
        href={link.href}
        target="_blank"
        rel="noopener noreferrer nofollow"
        className="text-fg hover:underline break-words"
      >
        {source.title}
        {site && site !== source.title ? <span className="text-fg-subtle"> · {site}</span> : null}
      </a>
    );
  }
  if (link) {
    return (
      <Link href={link.href} className="text-fg hover:underline break-words">
        {source.title}
      </Link>
    );
  }
  return <span className="text-fg break-words">{source.title}</span>;
}

/**
 * Under a finished reply: a note when part of the analysis couldn't be
 * included, and a collapsed "Sources (N)" list of what the answer looked at.
 */
export default function AnswerSourcesFooter({ sources }: { sources: AnswerSources }) {
  const [open, setOpen] = useState(false);
  const listId = useId();
  // Saved replies come back as stored, so read them as carefully as the event.
  const { sources: listed, unavailable } = answerSourcesFrom(sources);
  const groups = groupSources(listed);
  const count = groups.reduce((n, group) => n + group.items.length, 0);
  const note = missingNote(unavailable);
  if (count === 0 && !note) return null;

  return (
    <div className="mt-3 space-y-1.5">
      {note ? (
        // Themed text with an amber marker: plain amber text is too faint on
        // the light theme to read.
        <p className="flex items-start gap-1.5 text-xs text-fg-muted">
          <span className="mt-1 inline-block w-1.5 h-1.5 rounded-full bg-amber-400 flex-shrink-0" aria-hidden="true" />
          {note}
        </p>
      ) : null}
      {count > 0 ? (
        <div>
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
            aria-controls={listId}
            className="inline-flex items-center gap-1 text-xs text-fg-muted hover:text-fg transition-colors cursor-pointer"
          >
            <Icon name="book" size="w-3.5 h-3.5" />
            Sources ({count})
            <Icon
              name="chevron-right"
              size="w-3.5 h-3.5"
              className={`transition-transform ${open ? "rotate-90" : ""}`}
            />
          </button>
          {open ? (
            <div id={listId} className="mt-2 rounded-lg border border-line bg-surface-elevated/40 px-3 py-2 space-y-2">
              <p className="text-[11px] text-fg-subtle">Looked at for this answer</p>
              {groups.map((group) => (
                <div key={group.kind}>
                  <div className="text-[11px] font-medium text-fg-muted">{group.label}</div>
                  <ul className="mt-0.5 space-y-0.5">
                    {group.items.map((source, i) => (
                      <li key={`${group.kind}-${i}`} className="text-xs">
                        <SourceItem source={source} />
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
