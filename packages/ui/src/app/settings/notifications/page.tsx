"use client";

import NotificationsCard from "@/components/settings/NotificationsCard";
import SettingsSubpage from "@/components/settings/SettingsSubpage";

// Settings → Notifications: each person's own quiet hours, whether things
// reach them as they happen or in their briefs, and urgent things through.
export default function NotificationsSettingsPage() {
  return (
    <SettingsSubpage
      title="Notifications"
      description="Quiet hours, and whether things reach you as they happen or in your briefs. These are yours: everyone sets their own."
    >
      <NotificationsCard />
    </SettingsSubpage>
  );
}
