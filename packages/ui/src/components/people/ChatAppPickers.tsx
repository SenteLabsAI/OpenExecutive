"use client";

// Slack, Discord and Telegram for one person, picked by name (Add person and
// the person page). Slack suggests whoever has their work email.

import ChatPicker from "@/components/ui/ChatPicker";
import type { DirectoryListing, DirectoryOption } from "@/lib/chatDirectory";

export interface ChatAppIds {
  slack_user_id: string;
  discord_user_id: string;
  telegram_chat_id: string;
}

export function ChatAppPickers({
  ids,
  onChange,
  directory,
  slackMatch,
}: {
  ids: ChatAppIds;
  onChange: (field: keyof ChatAppIds, id: string) => void;
  /** From useChatDirectory("people"). */
  directory: DirectoryListing | null | undefined;
  /** From useSlackEmailMatch. */
  slackMatch: DirectoryOption | null;
}) {
  const discord = directory?.discord;
  return (
    <>
      <ChatPicker
        label="Slack"
        value={ids.slack_user_id}
        onChange={(id) => onChange("slack_user_id", id)}
        list={directory === undefined ? undefined : (directory?.slack ?? null)}
        suggestion={slackMatch ? { option: slackMatch, reason: "Found by email" } : null}
        rejectLabel="Not them?"
        rawLabel="Slack user ID"
        rawPlaceholder="U01ABC123"
      />
      <ChatPicker
        label="Discord"
        value={ids.discord_user_id}
        onChange={(id) => onChange("discord_user_id", id)}
        list={directory === undefined ? undefined : (discord ?? null)}
        unavailableText={
          discord?.members_available === false
            ? "Discord won't list server members until the bot's Server Members intent is on. Enter their ID by hand below."
            : "Nobody in the bot's servers yet."
        }
        rawLabel="Discord user ID"
        rawPlaceholder="123456789012345678"
        rawHint={<>Right-click their Discord username and &quot;Copy User ID&quot; (developer mode required).</>}
      />
      <ChatPicker
        label="Telegram"
        value={ids.telegram_chat_id}
        onChange={(id) => onChange("telegram_chat_id", id)}
        list={directory === undefined ? undefined : (directory?.telegram ?? null)}
        unavailableText="Nobody has messaged the bot yet."
        notListedHint="Not listed? Ask them to send the bot any message."
        rawLabel="Telegram chat ID"
        rawPlaceholder="123456789"
      />
    </>
  );
}
