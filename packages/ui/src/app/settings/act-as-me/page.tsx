"use client";

import ActAsMeCard, { ACT_AS_ME_INTRO } from "@/components/settings/ActAsMeCard";
import SettingsSubpage from "@/components/settings/SettingsSubpage";

// Settings → Act as me: drafts written as you, in your own mailbox, and
// whether the Executive keeps notes of the replies you send.
export default function ActAsMeSettingsPage() {
  return (
    <SettingsSubpage title="Act as me" description={ACT_AS_ME_INTRO}>
      <ActAsMeCard />
    </SettingsSubpage>
  );
}
