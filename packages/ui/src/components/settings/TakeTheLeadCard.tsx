"use client";

import Link from "next/link";

import SettingsCard from "@/components/settings/SettingsCard";

// Take the lead has no switch of its own: when the Executive looks over your
// day it acts on what it finds, and Approvals (Settings → Your Executive)
// decides each step. Hands-off lets it go; Balanced and Careful put it on an
// Approve card first.
export default function TakeTheLeadCard() {
  return (
    <SettingsCard title="Take the lead">
      <div className="flex flex-col gap-3 text-[15px] leading-snug">
        <p>
          When it looks over your day it acts on what it finds: it messages people, books meetings, starts workflows
          and makes changes in your connected apps.
        </p>
        <p className="text-fg-muted">
          Approvals decides each step. On Hands-off it goes ahead. On Balanced or Careful each one comes to you as an
          Approve card first.
        </p>
        <p>
          <Link href="/settings/executive" className="font-medium text-accent underline-offset-2 hover:underline">
            Change Approvals
          </Link>
        </p>
      </div>
    </SettingsCard>
  );
}
