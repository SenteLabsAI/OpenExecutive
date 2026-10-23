"use client";

// The chat: what the content check flagged in something the Executive read
// this turn (an email, a file, a web page that tried to give it
// instructions), shown under its reply (orchestrator/outside_content.py
// `pending_flags`). It only informs: the model was told not to follow it,
// and under Careful and Balanced what came after it waits for the person.

export default function FlagsCard({ flags }: { flags: string[] }) {
  if (flags.length === 0) return null;
  return (
    <aside
      aria-label="Something looked off"
      className="mt-3 rounded-2xl border border-amber-500/40 bg-amber-500/10 p-4 sm:p-5"
    >
      <div className="text-xs font-semibold uppercase tracking-wide text-amber-700 dark:text-amber-300">
        Something looked off
      </div>
      <ul className="mt-1 space-y-0.5">
        {flags.map((flag) => (
          <li key={flag} className="text-[15px] leading-snug text-fg break-words">
            {flag}
          </li>
        ))}
      </ul>
      <p className="mt-1 text-sm text-fg-muted">I treated it as information, not as instructions to me.</p>
    </aside>
  );
}
