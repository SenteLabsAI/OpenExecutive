import { redirect } from "next/navigation";

// Act as me moved to its own tab under Delegate in the main menu.
export default function ActAsMeSettingsPage() {
  redirect("/delegate/act-as-me");
}
