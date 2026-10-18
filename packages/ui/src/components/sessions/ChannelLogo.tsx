import { channelLogoSrc } from "@/lib/channelLogos";
import { CHANNEL_LABELS, type SessionChannel } from "@/lib/sessionChannel";

/** The app a chat came from, as its logo. `label` reads it out; leave it off
 *  where the channel's name is already written beside it. */
export function ChannelLogo({
  channel,
  label = false,
  className = "w-4 h-4",
}: {
  channel: SessionChannel;
  label?: boolean;
  className?: string;
}) {
  return (
    // eslint-disable-next-line @next/next/no-img-element -- a data URL, nothing for next/image to optimize
    <img
      src={channelLogoSrc(channel)}
      alt={label ? CHANNEL_LABELS[channel] : ""}
      title={label ? CHANNEL_LABELS[channel] : undefined}
      aria-hidden={label ? undefined : true}
      className={`inline-block flex-shrink-0 object-contain ${className}`}
    />
  );
}
