"use client";

import ActAsMeCard, { ACT_AS_ME_INTRO } from "@/components/settings/ActAsMeCard";
import { useDelegation } from "@/components/settings/HandleItCard";
import DelegatePage from "@/components/delegate/DelegatePage";
import { LearnedCard, SuggestedActionsCard, TrainingNote } from "@/components/settings/TrainingCard";

// Delegate → Act as me: drafts written as you, in your own mailbox, and how
// you write. Like someone new, each job can be in training (Off / In training
// / On on its own card, as on Take the lead), and what it learned there is
// listed at the bottom. What it sends as you on its own is the next tab,
// Handle it for me.
export default function ActAsMePage() {
  const load = useDelegation();
  const training = load.state === "ready" ? load.settings?.training : null;
  return (
    <DelegatePage feature="act_as_me" description={ACT_AS_ME_INTRO}>
      {training && <TrainingNote />}
      <ActAsMeCard onSettings={load.setSettings}>
        {training && <SuggestedActionsCard training={training} onSettings={load.setSettings} />}
        {training && <LearnedCard training={training} onSettings={load.setSettings} />}
      </ActAsMeCard>
    </DelegatePage>
  );
}
