import { redirect } from "next/navigation";

// Delegate opens on its first tab: what it may do without asking.
export default function DelegatePage() {
  redirect("/delegate/what-it-may-do");
}
