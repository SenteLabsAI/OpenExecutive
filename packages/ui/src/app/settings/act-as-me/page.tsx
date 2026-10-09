"use client";

import ActAsMeCard, { ACT_AS_ME_INTRO } from "@/components/settings/ActAsMeCard";
import HandleItCard, { useDelegation } from "@/components/settings/HandleItCard";
import SettingsSubpage from "@/components/settings/SettingsSubpage";
import { LearnedCard, SuggestedActionsCard, TrainingNote } from "@/components/settings/TrainingCard";

// Settings → Act as me: drafts written as you, in your own mailbox, and how
// much it sends as you on its own (Handle it for me, up to Take the lead).
// Like someone new, each job can be in training (Off / In training / On on
// its own card, as on Take the lead), and what it learned there is listed at
// the bottom.
export default function ActAsMeSettingsPage() {
  const load = useDelegation();
  const training = load.state === "ready" ? load.settings?.training : null;
  return (
    <SettingsSubpage title="Act as me" description={ACT_AS_ME_INTRO}>
      {training && <TrainingNote />}
      <ActAsMeCard onSettings={load.setSettings}>
        <HandleItCard load={load} />
        {training && <SuggestedActionsCard training={training} onSettings={load.setSettings} />}
        {training && <LearnedCard training={training} onSettings={load.setSettings} />}
      </ActAsMeCard>
    </SettingsSubpage>
  );
}
