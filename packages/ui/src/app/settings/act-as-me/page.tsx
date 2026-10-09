"use client";

import ActAsMeCard, { ACT_AS_ME_INTRO } from "@/components/settings/ActAsMeCard";
import HandleItCard, { useDelegation } from "@/components/settings/HandleItCard";
import SettingsSubpage from "@/components/settings/SettingsSubpage";
import TrainingSection from "@/components/settings/TrainingCard";

// Settings → Act as me: which settings are in training (and what it learned
// there), drafts written as you, in your own mailbox, whether the Executive
// keeps notes of the replies you send, and how much it sends as you on its
// own (Handle it for me, one dial up to Take the lead).
export default function ActAsMeSettingsPage() {
  const load = useDelegation();
  const training = load.state === "ready" ? load.settings?.training : null;
  return (
    <SettingsSubpage title="Act as me" description={ACT_AS_ME_INTRO}>
      {training && <TrainingSection training={training} onSettings={load.setSettings} />}
      <ActAsMeCard />
      <HandleItCard load={load} />
    </SettingsSubpage>
  );
}
