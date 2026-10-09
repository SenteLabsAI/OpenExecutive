"use client";

import DelegatePage from "@/components/delegate/DelegatePage";
import HandleItCard, { useDelegation } from "@/components/settings/HandleItCard";
import SettingsCard from "@/components/settings/SettingsCard";

// Delegate → Handle it for me: the replies and follow-ups it sends as you on
// its own, from your own mailbox. It builds on Act as me (its inbox drafts),
// so someone who can't have Act as me is told so here.
export default function HandleItPage() {
  const load = useDelegation();
  return (
    <DelegatePage feature="handle_it" description="Replies and follow-ups it sends as you, from your own mailbox.">
      {load.state === "hidden" ? (
        <SettingsCard>
          <p className="text-[15px] text-fg-muted leading-relaxed">
            Handle it for me sends replies through Act as me, which isn&apos;t available to you here. The owner of this
            Open Executive can turn it on for team members.
          </p>
        </SettingsCard>
      ) : (
        <HandleItCard load={load} />
      )}
    </DelegatePage>
  );
}
