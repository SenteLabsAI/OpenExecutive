"use client";

import type { ReactNode } from "react";

import { ReplyDisclosureSwitches } from "@/components/settings/ReplyDisclosureCard";
import SettingsCard from "@/components/settings/SettingsCard";
import type { DelegationSettings, ReplyDisclosure } from "@/lib/api";

// Delegate → Approvals, "Say it was written with AI": one place for the
// line it adds to what goes out. `asMe` is a row for what it sends in its
// own name, when the install has one; the As you row is the reader's own
// switches for replies sent as them (ReplyDisclosureSwitches). Nothing
// shows when there is neither.
export default function AiLineCard({
  disclosure,
  onSettings,
  asMe,
}: {
  disclosure: ReplyDisclosure | null;
  onSettings: (next: DelegationSettings) => void;
  asMe?: ReactNode;
}) {
  const asYou = disclosure?.enabled ? disclosure : null;
  if (!asYou && !asMe) return null;
  return (
    <SettingsCard title="Say it was written with AI" description="One place for the line it adds to what goes out.">
      <div className="flex flex-col divide-y divide-line rounded-xl border border-line">
        {asMe && <div className="px-4 py-3">{asMe}</div>}
        {asYou && (
          <div className="px-4 py-3">
            <h3 className="text-[15px] font-semibold text-fg">As you</h3>
            <p className="mt-0.5 mb-3 text-sm text-fg-muted leading-snug">
              Choose whether replies sent as you end with “{asYou.line}”, so people can tell your AI assistant wrote
              them.
            </p>
            <ReplyDisclosureSwitches disclosure={asYou} onSettings={onSettings} />
          </div>
        )}
      </div>
    </SettingsCard>
  );
}
