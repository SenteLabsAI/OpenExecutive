// The words the profile uses for itself, by what it is (`profileWording` in
// components/shell/navConfig.ts): a team's company, a solo owner's business,
// or, for anyone else using Open Executive just for themselves, their work.
// Copy only: every field keeps its name and meaning in all three.

import type { ProfileWording } from "@/components/shell/navConfig";

export interface ProfileCopy {
  /** The profile page when none exists yet. */
  missing: string;
  /** The chat home's banner when none exists yet. */
  missingBanner: string;
  /** A line under the page heading; null leaves the team page as it was. */
  intro: string | null;
  /** Points at Settings → Workspace, where the role lives; null in team. */
  roleNote: string | null;
  /** The step-by-step form's progress label. */
  progress: string;
  basicsTitle: string;
  missionPlaceholder: string;
  dependenciesNote: string;
  /** `org_structure.departments`: in solo a department is an area. */
  departmentsLabel: string;
}

const DEPENDENCIES_NOTE = (what: string) =>
  `Named here, a vendor or ticker counts as ${what}: the Executive will start watching its status page or filings on its own instead of asking you first.`;

export const PROFILE_COPY: Record<ProfileWording, ProfileCopy> = {
  company: {
    missing: "No company profile set up yet.",
    missingBanner: "No company profile — responses will be generic.",
    intro: null,
    roleNote: null,
    progress: "Setting up your company profile",
    basicsTitle: "Company Basics",
    missionPlaceholder: "Why does this company exist?",
    dependenciesNote: DEPENDENCIES_NOTE("company data"),
    departmentsLabel: "Departments",
  },
  business: {
    missing: "No business profile set up yet.",
    missingBanner: "No business profile — responses will be generic.",
    intro:
      "The Executive bases its advice on your business: what you offer, who you serve and what matters most right now.",
    roleNote: "Your title and role are in",
    progress: "Setting up your business profile",
    basicsTitle: "Business Basics",
    missionPlaceholder: "Why does this business exist?",
    dependenciesNote: DEPENDENCIES_NOTE("business data"),
    departmentsLabel: "Areas",
  },
  work: {
    missing: "Your work isn't set up yet.",
    missingBanner: "Your work isn't set up yet — responses will be generic.",
    intro:
      "The Executive bases its advice on your work: the organisation you work in, who it serves and what matters most right now.",
    roleNote: "Your title, who you report to and what you're responsible for are in",
    progress: "Setting up your work",
    basicsTitle: "Where You Work",
    missionPlaceholder: "Why does your organisation exist?",
    dependenciesNote: DEPENDENCIES_NOTE("part of your work"),
    departmentsLabel: "Areas",
  },
};
