"use client";

// A roster request on the briefing: someone not on the People list wrote in,
// was told their message arrived, and is held until the principal says who
// they are. Answered here (never through chat): add them as a new person,
// say they are someone already on the list, or ignore them. Their held
// messages are answered once they are added.

import { useEffect, useState } from "react";

import {
  approveRosterRequest,
  declineRosterRequest,
  listPeople,
  type Person,
  type PersonKind,
  type RosterRequestCard as RosterRequest,
} from "@/lib/api";

const CHANNEL_LABEL: Record<string, string> = {
  email: "Email",
  slack: "Slack",
  discord: "Discord",
  telegram: "Telegram",
};

type Mode = "new" | "link";

export default function RosterRequestCard({
  request,
  onResolved,
  emphasized = false,
}: {
  request: RosterRequest;
  // Called once the request is answered, so the briefing can drop the card.
  onResolved: () => void;
  emphasized?: boolean;
}) {
  const [mode, setMode] = useState<Mode>(request.suggested_person_id != null ? "link" : "new");
  const [name, setName] = useState(request.display_name);
  // No default for a stranger: the principal says team or contact. A sender
  // on the company's own domain is pre-filled as a teammate.
  const [kind, setKind] = useState<PersonKind | "">(request.suggested_kind ?? "");
  const [people, setPeople] = useState<Person[] | null>(null);
  const [linkId, setLinkId] = useState<number | "">(request.suggested_person_id ?? "");
  const [replace, setReplace] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const channel = CHANNEL_LABEL[request.channel] ?? request.channel;
  const isChat = request.channel !== "email";

  // The picker's list, fetched the first time "someone already on the list"
  // is chosen.
  useEffect(() => {
    if (mode !== "link" || people !== null) return;
    let live = true;
    listPeople({ includeContacts: true })
      .then((list) => live && setPeople(list))
      .catch(() => live && setPeople([]));
    return () => {
      live = false;
    };
  }, [mode, people]);

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await action();
      onResolved();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong.");
    } finally {
      setBusy(false);
    }
  }

  const canAdd = mode === "new" ? name.trim() !== "" && kind !== "" : linkId !== "";

  function approve() {
    if (!canAdd) return;
    void run(() =>
      mode === "new"
        ? approveRosterRequest(request.id, { full_name: name.trim(), kind: kind as PersonKind })
        : approveRosterRequest(request.id, {
            link_person_id: Number(linkId),
            replace_channel_id: replace,
          }),
    );
  }

  const rowAccent = emphasized ? " border-l-2 border-indigo-500 pl-3" : "";
  return (
    <div className={`py-3${rowAccent}`}>
      <div className="flex items-start justify-between gap-2 mb-1">
        <div className="min-w-0">
          <div className="text-sm font-medium text-fg break-words">
            {request.display_name ? `Who is ${request.display_name}?` : "Someone new wrote in"}
          </div>
          <div className="mt-0.5 text-xs text-fg-muted break-all">
            {channel} · {request.channel_ref}
            {request.profile_email ? ` · ${request.profile_email}` : ""}
          </div>
        </div>
        <div className="flex flex-shrink-0 flex-wrap justify-end gap-1">
          {request.on_company_domain && (
            <span className="inline-block px-1.5 py-0.5 rounded border text-[10px] bg-emerald-500/15 text-emerald-300 border-emerald-500/30">
              your domain
            </span>
          )}
          <span className="inline-block px-1.5 py-0.5 rounded border text-[10px] text-fg-muted border-line">
            {request.message_count} waiting
          </span>
        </div>
      </div>

      <p className="text-xs text-fg-muted leading-snug mb-2">
        Not on your People list.{" "}
        {request.ack_sent
          ? "They were told their message arrived and is waiting for you."
          : "They haven't been answered."}{" "}
        The name is the one they gave — not verified.
      </p>

      {request.previews.length > 0 && (
        <div className="mb-2 rounded-lg border border-line bg-surface/40 px-3 py-2 space-y-1">
          {request.previews.slice(0, 3).map((line, i) => (
            <p key={i} className="text-xs text-fg-muted whitespace-pre-wrap break-words">
              {line}
            </p>
          ))}
        </div>
      )}

      <div className="flex gap-3 text-xs mb-2" role="radiogroup" aria-label="Who they are">
        <label className="flex items-center gap-1 text-fg">
          <input
            type="radio"
            checked={mode === "new"}
            onChange={() => setMode("new")}
            disabled={busy}
          />
          Add as a new person
        </label>
        <label className="flex items-center gap-1 text-fg">
          <input
            type="radio"
            checked={mode === "link"}
            onChange={() => setMode("link")}
            disabled={busy}
          />
          Someone already on the list
        </label>
      </div>

      {mode === "new" ? (
        <div className="flex flex-wrap items-center gap-2 mb-2">
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Their name"
            maxLength={200}
            disabled={busy}
            className="min-w-0 flex-1 text-xs text-fg bg-surface border border-line rounded px-2 py-1 focus:outline-none focus:ring-1 focus:ring-indigo-500/40"
          />
          <select
            value={kind}
            onChange={(e) => setKind(e.target.value as PersonKind | "")}
            disabled={busy}
            className="text-xs text-fg bg-surface border border-line rounded px-2 py-1"
          >
            <option value="">Team or contact?</option>
            <option value="team">Team — can sign in and message you</option>
            <option value="contact">Contact — emailed only when you ask</option>
          </select>
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-2 mb-2">
          <select
            value={linkId}
            onChange={(e) => setLinkId(e.target.value === "" ? "" : Number(e.target.value))}
            disabled={busy || people === null}
            className="min-w-0 flex-1 text-xs text-fg bg-surface border border-line rounded px-2 py-1"
          >
            <option value="">{people === null ? "Loading…" : "Choose who they are"}</option>
            {(people ?? []).map((p) => (
              <option key={p.id} value={p.id}>
                {p.full_name}
                {p.kind === "contact" ? " (contact)" : ""}
              </option>
            ))}
          </select>
          {isChat && (
            <label className="flex items-center gap-1 text-[11px] text-fg-muted">
              <input
                type="checkbox"
                checked={replace}
                onChange={(e) => setReplace(e.target.checked)}
                disabled={busy}
              />
              Replace their current {channel} account
            </label>
          )}
        </div>
      )}

      {error && <p className="text-xs text-rose-300 mb-2">{error}</p>}

      <div className="flex justify-end gap-2">
        <button
          type="button"
          onClick={() => void run(() => declineRosterRequest(request.id))}
          disabled={busy}
          className="text-xs text-fg-muted hover:text-fg px-2 py-1 rounded transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          Ignore
        </button>
        <button
          type="button"
          onClick={approve}
          disabled={busy || !canAdd}
          className="text-xs font-medium text-emerald-300 hover:text-emerald-200 px-2 py-1 rounded transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {mode === "new" ? "✓ Add them" : "✓ That's them"}
        </button>
      </div>
    </div>
  );
}
