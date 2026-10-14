// The line the People page's "Assign a task" form shows about whether the
// person was told (the `notified` the assign endpoint returns). Kept free of
// React so scripts/assignNotice.test.mjs can import it.

export interface AssignNoticeLike {
  status: string;
  channel: string | null;
}

const CHANNEL_NAMES: Record<string, string> = {
  slack_dm: "Slack",
  discord_dm: "Discord",
  telegram: "Telegram",
  email: "email",
};

export function assignNoticeLine(notice: AssignNoticeLike | null | undefined): string {
  const followUp = "They'll be followed up if it isn't done by then.";
  if (!notice || notice.status === "skipped") return followUp;
  if (notice.status === "sent") {
    const where = notice.channel ? CHANNEL_NAMES[notice.channel] : undefined;
    return `${where ? `They were told on ${where}.` : "They were told."} ${followUp}`;
  }
  if (notice.status === "held") {
    return `They'll be told when their quiet hours end, or in their brief. ${followUp}`;
  }
  return `They couldn't be reached, so they haven't been told yet. ${followUp}`;
}
