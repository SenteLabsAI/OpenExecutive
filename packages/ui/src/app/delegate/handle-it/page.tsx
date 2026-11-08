"use client";

import DelegatePage from "@/components/delegate/DelegatePage";
import AdvancedFold from "@/components/settings/AdvancedFold";
import HandleItCard, { useDelegation } from "@/components/settings/HandleItCard";
import ReplyDisclosureCard from "@/components/settings/ReplyDisclosureCard";
import SettingsCard from "@/components/settings/SettingsCard";

// Delegate → Handle it for me: what it sends as you on its own, from your own
// mailbox (one switch; Approvals decides what goes), what it sent, and the
// AI line on those replies. It
// builds on Act as me (its inbox replies), so someone who can't have Act as
// me is told so here.
export default function HandleItPage() {
  const load = useDelegation();
  const disclosure = load.state === "ready" ? load.settings?.disclosure : null;
  return (
    <DelegatePage feature="handle_it" description="Replies and follow-ups it sends as you, on its own, from your mailbox.">
      {load.state === "hidden" ? (
        <SettingsCard>
          <p className="text-[15px] text-fg-muted leading-relaxed">
            Handle it for me sends replies through Act as me, which isn&apos;t available to you here. The owner of this
            Open Executive can turn it on for team members.
          </p>
        </SettingsCard>
      ) : (
        <>
          <HandleItCard load={load} />
          {disclosure?.enabled && (
            <AdvancedFold id="handle-it-advanced" summary="AI line on replies it sends">
              <ReplyDisclosureCard
                disclosure={disclosure}
                onSettings={load.setSettings}
                only="handle_it"
                title="AI line on replies it sends"
              />
            </AdvancedFold>
          )}
        </>
      )}
    </DelegatePage>
  );
}
