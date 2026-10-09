import { redirect } from "next/navigation";

// Playbooks now live on the workflow that follows them ("How it's done").
export default function SkillsPage() {
  redirect("/jobs");
}
