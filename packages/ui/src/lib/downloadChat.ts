// Saves a chat as a Markdown file (GET /sessions/{id}/export). Shared by the
// Chats list, the open chat's menu and the read-only view of a shared chat.

import { exportSession } from "@/lib/api";

/** Downloads the chat; throws when the export failed. */
export async function downloadChat(sessionId: string): Promise<void> {
  const { text, filename } = await exportSession(sessionId);
  const blob = new Blob([text], { type: "text/markdown" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  // Revoked on the next tick: some browsers start the download after click returns.
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
