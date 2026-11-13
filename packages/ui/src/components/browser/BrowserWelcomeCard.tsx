"use client";

// On a new chat once your browser is connected, what you can ask
// it to do there (lib/browserWelcome.ts says when). A pill puts its words
// in the box, never sends them; using one or closing the card hides it for
// good. The pills come from the company profile
// (browser/starters.py) and each opens a fresh page.

import Icon from "@/components/Icon";

export default function BrowserWelcomeCard({
  starters,
  onPick,
  onClose,
}: {
  starters: readonly string[];
  onPick: (text: string) => void;
  onClose: () => void;
}) {
  return (
    <section
      aria-label="Your browser is connected"
      className="relative mt-6 w-full rounded-2xl border border-accent/40 bg-accent/10 p-4 sm:p-5 text-left"
    >
      <button
        type="button"
        onClick={onClose}
        aria-label="Close"
        className="absolute right-2 top-2 min-h-touch min-w-touch sm:min-h-0 sm:min-w-0 sm:p-1.5 flex items-center justify-center rounded-lg text-fg-muted hover:text-fg hover:bg-surface-hover cursor-pointer"
      >
        <Icon name="close" size="w-4 h-4" />
      </button>
      <p className="pr-8 text-base font-semibold text-fg">Your browser is connected</p>
      <p className="mt-1 text-sm text-fg-muted">I can open sites, read them, click and type for you. Try one:</p>
      <div className="mt-3 flex flex-wrap gap-2">
        {starters.map((text) => (
          <button
            type="button"
            key={text}
            onClick={() => onPick(text)}
            className="min-h-touch sm:min-h-0 px-3.5 py-2 rounded-full border border-accent/40 bg-surface-elevated text-sm text-fg hover:border-accent hover:bg-surface-hover transition-colors cursor-pointer text-left"
          >
            {text}
          </button>
        ))}
      </div>
    </section>
  );
}
