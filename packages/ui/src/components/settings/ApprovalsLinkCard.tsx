"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import SettingsCard from "@/components/settings/SettingsCard";
import { getOutsideContent, type OutsideContentSetting } from "@/lib/api";
import { approvalsTitle, onApprovalsChanged } from "@/lib/approvalsLevel";
import { APPROVALS_HREF } from "@/lib/reach";

// Settings → Your Executive: the Approvals level in one line. It is changed
// on Delegate → Approvals (or from the side menu), so this
// only names it and links there. Hidden until it loads.
export default function ApprovalsLinkCard() {
  const [setting, setSetting] = useState<OutsideContentSetting | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    getOutsideContent(ctrl.signal).then(setSetting, () => setSetting(null));
    const stop = onApprovalsChanged<OutsideContentSetting>(setSetting);
    return () => {
      ctrl.abort();
      stop();
    };
  }, []);

  const level = setting?.mode ?? null;
  if (!level) return null;
  return (
    <SettingsCard
      title={`Approvals: ${approvalsTitle(level)}`}
      description="What waits for your yes, the same in the app, email and every chat app."
      action={
        <Link href={APPROVALS_HREF} className="text-sm font-medium text-accent underline-offset-2 hover:underline">
          {setting?.can_change ? "Change on Delegate" : "See on Delegate"}
        </Link>
      }
    />
  );
}
