import { redirect } from "next/navigation";

// On its own was folded into Your Executive; old links land there.
export default function OnItsOwnSettingsPage() {
  redirect("/settings/executive");
}
