import { redirect } from "next/navigation";

// Skills are managed as Playbooks, a tab on the Workflows page.
export default function SkillsPage() {
  redirect("/jobs?tab=playbooks");
}
