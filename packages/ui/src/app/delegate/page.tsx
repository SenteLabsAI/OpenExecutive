import { redirect } from "next/navigation";

// Delegate opens on its first tab.
export default function DelegatePage() {
  redirect("/delegate/act-as-me");
}
