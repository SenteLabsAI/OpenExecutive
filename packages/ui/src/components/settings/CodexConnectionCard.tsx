"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import Icon from "@/components/Icon";
import {
  cancelCodexDeviceLogin,
  CodexAuthRequestError,
  CodexAuthStatus,
  getCodexAuthStatus,
  startCodexDeviceLogin,
} from "@/lib/api";

const STATUS_POLL_INTERVAL_MS = 2_000;
const STATUS_REFRESH_INTERVAL_MS = 30_000;
const ACCESS_RECHECK_INTERVAL_MS = 30_000;

function useCodexConnection() {
  const [connection, setConnection] = useState<CodexAuthStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [forbidden, setForbidden] = useState(false);
  // Only the newest status request may update the UI. Polls can overlap a
  // cancel/manual refresh and otherwise resurrect stale "pending" state.
  const requestSequence = useRef(0);

  const refresh = useCallback(async () => {
    const requestId = ++requestSequence.current;
    try {
      const next = await getCodexAuthStatus();
      if (requestId !== requestSequence.current) return;
      setConnection(next);
      setError(null);
      setForbidden(false);
    } catch (err) {
      if (requestId !== requestSequence.current) return;
      if (err instanceof CodexAuthRequestError && err.status === 403) {
        setForbidden(true);
        setConnection(null);
        setError(null);
        return;
      }
      setError(err instanceof Error ? err.message : "Could not read Codex status.");
    } finally {
      if (requestId === requestSequence.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (connection?.state !== "pending") return;
    const timer = window.setInterval(() => void refresh(), STATUS_POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [connection?.state, refresh]);

  useEffect(() => {
    if (!forbidden) return;
    const timer = window.setInterval(() => void refresh(), ACCESS_RECHECK_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [forbidden, refresh]);

  useEffect(() => {
    if (forbidden || connection?.state === "pending") return;
    const timer = window.setInterval(() => void refresh(), STATUS_REFRESH_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [connection?.state, forbidden, refresh]);

  const connect = useCallback(async () => {
    // Reserve a user-initiated tab before awaiting the API response. Opening
    // after an await is commonly blocked as an unsolicited popup.
    const signInWindow = window.open("", "_blank");
    if (signInWindow) signInWindow.opener = null;
    ++requestSequence.current;
    setBusy(true);
    setError(null);
    try {
      const login = await startCodexDeviceLogin();
      setConnection({ state: "pending", ...login });
      setLoading(false);
      if (signInWindow && !signInWindow.closed) {
        signInWindow.location.replace(login.verification_url);
      }
    } catch (err) {
      signInWindow?.close();
      await refresh();
      if (err instanceof CodexAuthRequestError) {
        if (err.status === 403) setForbidden(true);
        // A different tab may have completed or started the login. The fresh
        // status is authoritative, so do not overlay its state with a stale
        // conflict message.
        if (err.status === 403 || err.status === 409) return;
      }
      setError(err instanceof Error ? err.message : "Could not start ChatGPT sign-in.");
    } finally {
      setBusy(false);
    }
  }, [refresh]);

  const cancel = useCallback(async () => {
    ++requestSequence.current;
    setBusy(true);
    setError(null);
    try {
      await cancelCodexDeviceLogin();
      await refresh();
    } catch (err) {
      await refresh();
      if (err instanceof CodexAuthRequestError) {
        if (err.status === 403) setForbidden(true);
        if (err.status === 403 || err.status === 409) return;
      }
      setError(err instanceof Error ? err.message : "Could not cancel ChatGPT sign-in.");
    } finally {
      setBusy(false);
    }
  }, [refresh]);

  return { connection, loading, busy, error, forbidden, connect, cancel, refresh };
}

function PendingLogin({
  connection,
  busy,
  onCancel,
}: {
  connection: CodexAuthStatus;
  busy: boolean;
  onCancel: () => Promise<void>;
}) {
  return (
    <div className="mt-4 rounded-lg border border-indigo-500/30 bg-indigo-500/10 p-4">
      <p className="text-xs font-medium text-indigo-200">Finish signing in</p>
      <p className="mt-1 text-xs text-fg-muted">
        Open the ChatGPT sign-in page and enter this one-time code:
      </p>
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <code className="select-all rounded-md border border-line-strong bg-surface px-3 py-2 text-base tracking-[0.2em] text-fg">
          {connection.user_code}
        </code>
        {connection.verification_url && (
          <a
            href={connection.verification_url}
            target="_blank"
            rel="noopener noreferrer"
            className="rounded-lg bg-indigo-500 px-3.5 py-2 text-xs font-medium text-white hover:bg-indigo-400"
          >
            Open sign-in page
          </a>
        )}
        <button
          type="button"
          onClick={() => void onCancel()}
          disabled={busy}
          className="rounded-lg border border-line px-3.5 py-2 text-xs text-fg-muted hover:border-line-strong hover:text-fg disabled:opacity-50"
        >
          {busy ? "Cancelling…" : "Cancel"}
        </button>
      </div>
      <p className="mt-3 text-[11px] text-fg-muted">
        This page checks for completion automatically. The code expires if sign-in is not completed.
      </p>
    </div>
  );
}

function ConnectedAccount({ connection }: { connection: CodexAuthStatus }) {
  const chatgpt = connection.auth_mode === "chatgpt";
  return (
    <div className="mt-4 rounded-lg border border-line bg-surface px-4 py-3 text-xs">
      <p className="font-medium text-fg">
        {chatgpt ? "ChatGPT connected" : "Codex is already authenticated"}
      </p>
      <p className="mt-1 text-fg-muted">
        {[connection.email, connection.plan_type, connection.auth_mode]
          .filter(Boolean)
          .join(" · ")}
      </p>
    </div>
  );
}

function ConnectionAction({
  connection,
  busy,
  onConnect,
  onRefresh,
}: {
  connection: CodexAuthStatus;
  busy: boolean;
  onConnect: () => Promise<void>;
  onRefresh: () => Promise<void>;
}) {
  if (connection.state === "disconnected") {
    return (
      <div className="mt-4">
        <button
          type="button"
          onClick={() => void onConnect()}
          disabled={busy}
          className="rounded-lg bg-indigo-500 px-3.5 py-2 text-xs font-medium text-white hover:bg-indigo-400 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {busy ? "Starting…" : "Connect ChatGPT"}
        </button>
        <p className="mt-2 text-[11px] text-fg-muted">
          Principal-only. Model routing is not enabled until the later provider step.
        </p>
      </div>
    );
  }

  if (connection.state === "error") {
    return (
      <div className="mt-4">
        <p className="text-xs text-rose-300">
          {connection.error ?? "ChatGPT sign-in did not complete."}
        </p>
        <button
          type="button"
          onClick={() => void onConnect()}
          disabled={busy}
          className="mt-2 rounded-lg border border-line px-3 py-1.5 text-xs text-fg-muted hover:text-fg disabled:opacity-50"
        >
          {busy ? "Starting…" : "Try sign-in again"}
        </button>
      </div>
    );
  }

  if (connection.state === "unavailable") {
    return (
      <div className="mt-4">
        <p className="text-xs text-rose-300">
          {connection.error ?? "Codex connection is unavailable."}
        </p>
        <button
          type="button"
          onClick={() => void onRefresh()}
          className="mt-2 rounded-lg border border-line px-3 py-1.5 text-xs text-fg-muted hover:text-fg"
        >
          Retry
        </button>
      </div>
    );
  }

  return null;
}

export default function CodexConnectionCard() {
  const state = useCodexConnection();
  const connectedWithChatGPT =
    state.connection?.state === "connected" && state.connection.auth_mode === "chatgpt";

  if (state.forbidden) return null;

  return (
    <section className="mt-6 rounded-xl border border-line bg-surface-elevated p-5">
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2.5">
            <span className="text-fg-muted"><Icon name="bolt" size="w-5 h-5" /></span>
            <h2 className="text-sm font-medium text-fg">ChatGPT subscription</h2>
          </div>
          <p className="mt-2 max-w-xl text-xs leading-relaxed text-fg-muted">
            Connect the principal&apos;s ChatGPT account through OpenAI&apos;s official
            Codex device sign-in. Your password is never sent to Open Executive; OAuth tokens stay inside Codex App Server.
          </p>
        </div>
        {connectedWithChatGPT && (
          <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2.5 py-1 text-xs text-emerald-300">
            <Icon name="check-circle" size="w-3.5 h-3.5" /> Connected
          </span>
        )}
      </div>

      {state.loading && <p className="mt-4 text-xs text-fg-muted">Checking connection…</p>}
      {!state.loading && state.connection?.state === "pending" && (
        <PendingLogin connection={state.connection} busy={state.busy} onCancel={state.cancel} />
      )}
      {!state.loading && state.connection?.state === "connected" && (
        <ConnectedAccount connection={state.connection} />
      )}
      {!state.loading && state.connection && (
        <ConnectionAction
          connection={state.connection}
          busy={state.busy}
          onConnect={state.connect}
          onRefresh={state.refresh}
        />
      )}
      {!state.loading && !state.connection && (
        <button
          type="button"
          onClick={() => void state.refresh()}
          className="mt-3 rounded-lg border border-line px-3 py-1.5 text-xs text-fg-muted hover:text-fg"
        >
          Retry status
        </button>
      )}
      {state.error && <p role="alert" className="mt-3 text-xs text-rose-300">{state.error}</p>}
    </section>
  );
}
