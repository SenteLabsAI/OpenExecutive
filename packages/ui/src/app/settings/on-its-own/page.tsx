"use client";

import ExecutiveRunSwitch from "@/components/executive/ExecutiveRunSwitch";
import HandleItCard from "@/components/settings/HandleItCard";
import SettingsCard from "@/components/settings/SettingsCard";
import SettingsSubpage from "@/components/settings/SettingsSubpage";
import { MeetingAutonomySwitch } from "@/components/settings/WorkspaceCard";

// Settings → On its own: everything the Executive does without asking first,
// in one place. Pause at the top stops all of it; then what it does as
// itself (booking meetings), then what it does as you, from your own mailbox
// (Handle it for me, with Careful / Balanced / Bold).
export default function OnItsOwnSettingsPage() {
  return (
    <SettingsSubpage
      title="On its own"
      description="What the Executive does without asking you first, and how much."
    >
      <SettingsCard title="Pause everything" description="Stops everything it does on its own, at once.">
        <ExecutiveRunSwitch />
      </SettingsCard>

      <section aria-labelledby="on-its-own-exec" className="space-y-3">
        <div>
          <h2 id="on-its-own-exec" className="text-lg font-semibold text-fg">As the Executive</h2>
          <p className="mt-1 text-[15px] text-fg-muted">In its own name. People can see it&apos;s the Executive.</p>
        </div>
        <MeetingAutonomySwitch />
      </section>

      <section aria-labelledby="on-its-own-you" className="space-y-3">
        <div>
          <h2 id="on-its-own-you" className="text-lg font-semibold text-fg">As you</h2>
          <p className="mt-1 text-[15px] text-fg-muted">From your own mailbox, in your name.</p>
        </div>
        <HandleItCard />
      </section>
    </SettingsSubpage>
  );
}
