"use client";

import DelegatePage from "@/components/delegate/DelegatePage";
import TakeTheLeadCard from "@/components/settings/TakeTheLeadCard";

// Delegate → Take the lead: what the Executive does in its own name without
// waiting to be asked, with its Always asks first list (meetings asked for
// in chat included). Pause, on Settings → Your Executive, stops all of it.
export default function TakeTheLeadPage() {
  return (
    <DelegatePage
      feature="take_the_lead"
      description="What the Executive does in its own name, without waiting to be asked. People can see it's the Executive."
    >
      <TakeTheLeadCard />
    </DelegatePage>
  );
}
