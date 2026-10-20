"use client";

import ActAsMeCard, { ACT_AS_ME_INTRO } from "@/components/settings/ActAsMeCard";
import { useDelegation } from "@/components/settings/HandleItCard";
import DelegatePage from "@/components/delegate/DelegatePage";
import ReplyDisclosureCard from "@/components/settings/ReplyDisclosureCard";
import { LearnedCard, SuggestedActionsCard } from "@/components/settings/TrainingCard";

// Delegate → Act as me: drafts written as you, in your own mailbox, that you
// send, and how you write. What it writes as you (WritesCard, inside
// ActAsMeCard) is its picker; what it sends on its own is on the Handle it
// for me tab. Suggested actions can be in training, and what it learned
// from your edits and approvals here is listed after them.
export default function ActAsMePage() {
  const load = useDelegation();
  const training = load.state === "ready" ? load.settings?.training : null;
  const disclosure = load.state === "ready" ? load.settings?.disclosure : null;
  return (
    <DelegatePage feature="act_as_me" description={ACT_AS_ME_INTRO}>
      <ActAsMeCard
        onSettings={load.setSettings}
        advanced={
          disclosure?.enabled && (
            <ReplyDisclosureCard
              disclosure={disclosure}
              onSettings={load.setSettings}
              only="approved_sends"
              title="AI line on drafts you send"
            />
          )
        }
        advancedLabel="AI line on drafts you send"
      >
        {training && <SuggestedActionsCard training={training} onSettings={load.setSettings} />}
        {training && (
          <LearnedCard
            training={training}
            onSettings={load.setSettings}
            jobs={["drafts", "actions"]}
            title="What it's learned"
            description="From your edits and your approvals here. Yours alone. Remove one and it asks you again."
          />
        )}
      </ActAsMeCard>
    </DelegatePage>
  );
}
