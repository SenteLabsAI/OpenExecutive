"use client";

import ActAsMeCard, { ACT_AS_ME_INTRO } from "@/components/settings/ActAsMeCard";
import HandleItCard from "@/components/settings/HandleItCard";
import SettingsSubpage from "@/components/settings/SettingsSubpage";

// Settings → Act as me: drafts written as you, in your own mailbox, whether
// the Executive keeps notes of the replies you send, and how much it sends as
// you on its own (Handle it for me, one dial up to Take the lead).
export default function ActAsMeSettingsPage() {
  return (
    <SettingsSubpage title="Act as me" description={ACT_AS_ME_INTRO}>
      <ActAsMeCard />
      <HandleItCard />
    </SettingsSubpage>
  );
}
