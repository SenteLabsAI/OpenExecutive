"use client";

// Pick a Slack, Discord or Telegram person or room by name instead of typing
// its id (GET /chat-directory/*). The id is still what gets saved; it shows
// only under "Enter an ID by hand", or when the saved one isn't in the list.
// With no bot for the provider (or for anyone but the principal) there is no
// list, and this is just the by-hand field.

import { type ReactNode, useEffect, useId, useRef, useState } from "react";

import { findSlackPersonByEmail, getChatDirectoryChannels, getChatDirectoryPeople } from "@/lib/api";
import {
  type DirectoryListing,
  type DirectoryOption,
  type ProviderList,
  filterOptions,
  findOption,
} from "@/lib/chatDirectory";

const INPUT_CLS =
  "w-full h-11 px-3 rounded-xl bg-surface-input/60 border border-line text-[15px] text-fg placeholder-fg-subtle focus:outline-none focus:border-accent";
const HINT_CLS = "text-[13px] text-fg-subtle";
const LINK_CLS = "text-sm text-accent hover:underline min-h-11 sm:min-h-0 px-1 -mx-1";

/** The directory for people or rooms: undefined while loading, null when
 * there is none (no access, or it could not be read). */
export function useChatDirectory(
  kind: "people" | "channels",
  enabled = true,
): DirectoryListing | null | undefined {
  const [listing, setListing] = useState<DirectoryListing | null | undefined>(undefined);
  useEffect(() => {
    if (!enabled) return;
    let live = true;
    (kind === "people" ? getChatDirectoryPeople() : getChatDirectoryChannels()).then((l) => {
      if (live) setListing(l);
    });
    return () => {
      live = false;
    };
  }, [kind, enabled]);
  return listing;
}

/** The Slack person with this work email, looked up once typing pauses. */
export function useSlackEmailMatch(email: string, enabled: boolean): DirectoryOption | null {
  const [match, setMatch] = useState<{ email: string; option: DirectoryOption | null } | null>(null);
  const want = email.trim().toLowerCase();
  const usable = enabled && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(want);
  useEffect(() => {
    if (!usable) return;
    let live = true;
    const timer = setTimeout(() => {
      findSlackPersonByEmail(want).then((option) => {
        if (live) setMatch({ email: want, option });
      });
    }, 400);
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [want, usable]);
  return usable && match?.email === want ? match.option : null;
}

export interface ChatPickerProps {
  /** "Slack", "Discord channel", … */
  label: string;
  value: string;
  onChange: (id: string) => void;
  /** This provider's list; undefined while loading. */
  list: ProviderList | null | undefined;
  /** Shown under the list: how to get someone onto it. */
  notListedHint?: string;
  /** Shown instead of the list when the provider can't list anyone. */
  unavailableText?: string;
  /** A match to pre-select while nothing is saved, and why it was picked. */
  suggestion?: { option: DirectoryOption; reason: string } | null;
  /** The button that drops the suggestion ("Not them?"). */
  rejectLabel?: string;
  searchPlaceholder?: string;
  /** The by-hand id field. */
  rawLabel: string;
  rawPlaceholder: string;
  rawHint?: ReactNode;
}

function OptionText({ option }: { option: DirectoryOption }) {
  return (
    <span className="min-w-0">
      <span className="text-[15px] text-fg break-words">{option.name}</span>
      {option.detail && <span className="text-sm text-fg-muted break-words"> {option.detail}</span>}
    </span>
  );
}

export default function ChatPicker({
  label,
  value,
  onChange,
  list,
  notListedHint,
  unavailableText,
  suggestion,
  rejectLabel = "Pick another",
  searchPlaceholder = "Search by name",
  rawLabel,
  rawPlaceholder,
  rawHint,
}: ChatPickerProps) {
  const [searching, setSearching] = useState(false);
  const [query, setQuery] = useState("");
  const [byHand, setByHand] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  // The id this picker filled in from `suggestion`, so a changed suggestion
  // (a new email typed) replaces it but never something the person picked.
  const applied = useRef<string | null>(null);
  const searchId = useId();

  const hasList = !!list && list.configured;
  const suggestedId = hasList && !dismissed ? (suggestion?.option.id ?? null) : null;

  useEffect(() => {
    const current = value.trim();
    if (suggestedId) {
      if ((!current || current === applied.current) && current !== suggestedId) {
        applied.current = suggestedId;
        onChange(suggestedId);
      }
    } else if (applied.current && current === applied.current) {
      applied.current = null;
      onChange("");
    }
  }, [suggestedId, value, onChange]);

  const rawField = (
    <label className="text-sm text-fg-muted flex flex-col gap-1.5">
      {rawLabel}
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className={`${INPUT_CLS} font-mono`}
        placeholder={rawPlaceholder}
        autoComplete="off"
      />
      {rawHint && <span className={HINT_CLS}>{rawHint}</span>}
    </label>
  );

  // No bot for this provider (or no access): the by-hand field, as before.
  if (list === null || (list !== undefined && !list.configured)) return rawField;

  const options = list?.options ?? [];
  const selected = findOption(options, value);
  const showingSuggestion = !!selected && !!suggestion && selected.id === suggestion.option.id && !dismissed;
  const showSearch = searching || !value.trim();
  const matches = filterOptions(options, query);

  function pick(id: string) {
    applied.current = null;
    onChange(id);
    setSearching(false);
    setQuery("");
  }

  function reject() {
    applied.current = null;
    setDismissed(true);
    onChange("");
    setSearching(true);
  }

  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-sm text-fg-muted" id={`${searchId}-label`}>
        {label}
      </span>

      {list === undefined ? (
        <p className={HINT_CLS}>Loading…</p>
      ) : (
        <>
          {value.trim() && !showSearch && (
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2.5 rounded-xl border border-line bg-surface-elevated">
              <div className="min-w-0 flex-1">
                {selected ? (
                  <>
                    {showingSuggestion && (
                      <span className="block text-[13px] text-fg-subtle">{suggestion?.reason}</span>
                    )}
                    <OptionText option={selected} />
                  </>
                ) : (
                  <span className="text-[15px] text-fg break-words">
                    Not in the list: <span className="font-mono">{value}</span>
                  </span>
                )}
              </div>
              <div className="flex items-center gap-3">
                {showingSuggestion ? (
                  <button type="button" onClick={reject} className={LINK_CLS}>
                    {rejectLabel}
                  </button>
                ) : (
                  <button type="button" onClick={() => setSearching(true)} className={LINK_CLS}>
                    Change
                  </button>
                )}
                <button type="button" onClick={() => pick("")} className={LINK_CLS}>
                  Clear
                </button>
              </div>
            </div>
          )}

          {showSearch && (
            <div className="flex flex-col gap-1.5">
              {!list.ok ? (
                <p className={HINT_CLS}>Couldn&apos;t load the list just now. Try again in a minute.</p>
              ) : options.length === 0 ? (
                <p className={HINT_CLS}>{unavailableText ?? "Nobody to pick from yet."}</p>
              ) : (
                <>
                  <input
                    type="search"
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    placeholder={searchPlaceholder}
                    aria-labelledby={`${searchId}-label`}
                    className={INPUT_CLS}
                    autoComplete="off"
                  />
                  <ul className="max-h-60 overflow-y-auto rounded-xl border border-line divide-y divide-line">
                    {matches.length === 0 ? (
                      <li className="px-3 py-2.5 text-sm text-fg-muted">No matches.</li>
                    ) : (
                      matches.map((o) => (
                        <li key={o.id}>
                          <button
                            type="button"
                            onClick={() => pick(o.id)}
                            aria-pressed={o.id === value.trim()}
                            className="w-full text-left px-3 py-2.5 min-h-11 hover:bg-surface-elevated flex"
                          >
                            <OptionText option={o} />
                          </button>
                        </li>
                      ))
                    )}
                  </ul>
                  {list.truncated && <p className={HINT_CLS}>Showing the first part of a long list. Search to narrow it.</p>}
                </>
              )}
              {notListedHint && <p className={HINT_CLS}>{notListedHint}</p>}
              {searching && value.trim() && (
                <button type="button" onClick={() => setSearching(false)} className={`${LINK_CLS} self-start`}>
                  Cancel
                </button>
              )}
            </div>
          )}
        </>
      )}

      <button
        type="button"
        onClick={() => setByHand((v) => !v)}
        aria-expanded={byHand}
        className={`${LINK_CLS} self-start text-fg-muted`}
      >
        Enter an ID by hand
      </button>
      {byHand && rawField}
    </div>
  );
}
