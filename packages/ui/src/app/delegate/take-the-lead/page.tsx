"use client";

import DelegatePage from "@/components/delegate/DelegatePage";
import TakeTheLeadCard from "@/components/settings/TakeTheLeadCard";
import { MeetingAutonomySwitch } from "@/components/settings/WorkspaceCard";

// Delegate → Take the lead: what the Executive does in its own name without
// waiting to be asked, and booking meetings without asking. Pause, on
// Settings → Your Executive, stops all of it.
export default function TakeTheLeadPage() {
  return (
    <DelegatePage
      feature="take_the_lead"
      description="What the Executive does in its own name, without waiting to be asked. People can see it's the Executive."
    >
      <TakeTheLeadCard />
      <MeetingAutonomySwitch />
    </DelegatePage>
  );
}
