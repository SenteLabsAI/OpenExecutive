"use client";

import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import Chat from "@/components/Chat";
import { getSession, getSessionMessages, type ChatMessage, type SessionSummary } from "@/lib/api";
import { sessionTitle } from "@/lib/sessionChannel";

type Loaded =
  | { id: string; state: "ok"; session: SessionSummary; messages: ChatMessage[] }
  | { id: string; state: "gone" | "error" };

// A chat a teammate shared with the viewer: they write in it with the
// teammate, live, and can leave it. Opened from "Shared with you" on /chats.
export default function SharedChatPage() {
  const params = useParams();
  const id = typeof params.id === "string" ? decodeURIComponent(params.id) : "";
  const router = useRouter();
  // Keyed by the chat it is for, so a different id shows "Loading" until its
  // own answer arrives (no state set synchronously in the effect).
  const [loaded, setLoaded] = useState<Loaded | null>(null);

  useEffect(() => {
    if (!id) return;
    let live = true;
    Promise.all([getSession(id), getSessionMessages(id)])
      .then(([session, messages]) => {
        if (live) setLoaded({ id, state: "ok", session, messages });
      })
      .catch((err: unknown) => {
        if (!live) return;
        const gone = err instanceof Error && err.message === "not_found";
        setLoaded({ id, state: gone ? "gone" : "error" });
      });
    return () => {
      live = false;
    };
  }, [id]);

  const current = loaded && loaded.id === id ? loaded : null;
  const chat = current?.state === "ok" ? current : null;

  return (
    <div className="flex flex-col h-full bg-surface text-fg">
      <div className="border-b border-line px-4 sm:px-6 py-3 flex-shrink-0">
        <div className="max-w-3xl mx-auto min-w-0">
          <Link href="/chats" className="text-sm text-fg-muted hover:text-fg">
            ← Chats
          </Link>
          {current?.state === "ok" && (
            <h1 className="mt-1 text-lg font-semibold text-fg truncate">{sessionTitle(current.session)}</h1>
          )}
        </div>
      </div>
      {!current ? (
        <p className="max-w-3xl mx-auto w-full px-4 sm:px-6 py-8 text-[15px] text-fg-muted">Loading chat…</p>
      ) : current.state === "gone" ? (
        <p className="max-w-3xl mx-auto w-full px-4 sm:px-6 py-8 text-[15px] text-fg-muted">
          This chat isn&apos;t shared with you any more.
        </p>
      ) : !chat ? (
        <p className="max-w-3xl mx-auto w-full px-4 sm:px-6 py-8 text-[15px] text-fg-muted">
          Couldn&apos;t load this chat. Try again in a moment.
        </p>
      ) : chat.session.shared_with_you ? (
        <div className="flex-1 min-h-0">
          <Chat
            initialMessages={chat.messages}
            initialSessionId={id}
            member={{
              sharedBy: chat.session.shared_by ?? "someone on your team",
              onLeft: () => router.push("/chats"),
            }}
          />
        </div>
      ) : (
        // The viewer's own chat: it opens where they can write in it.
        <p className="max-w-3xl mx-auto w-full px-4 sm:px-6 py-8 text-[15px] text-fg-muted">
          This is your own chat.{" "}
          <Link href={`/?session=${encodeURIComponent(id)}`} className="font-medium text-accent hover:underline">
            Open it
          </Link>
        </p>
      )}
    </div>
  );
}
