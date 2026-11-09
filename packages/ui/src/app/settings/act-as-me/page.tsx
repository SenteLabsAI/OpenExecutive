import { redirect } from "next/navigation";

import { AS_YOU_HREF } from "@/lib/reach";

// Act as me moved to Delegate in the main menu.
export default function ActAsMeSettingsPage() {
  redirect(AS_YOU_HREF);
}
