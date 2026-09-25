// The principal's role, as the onboarding role step and Settings edit it:
// what kind of principal they are and what they do. Stored with the workspace
// settings (PUT /workspace); solo mode tells the Executive and its
// specialists, so a VP inside a large company and a business owner get advice
// that fits.
//
// Type-only imports, so `npm test` can exercise this under
// `node --experimental-strip-types` (see scripts/principalRole.test.mjs).

import type { PrincipalRole, RoleKind, WorkspaceUpdate } from "@/lib/api";

export const ROLE_KIND_OPTIONS: { kind: RoleKind; label: string; hint: string }[] = [
  { kind: "owner", label: "Owner / founder", hint: "It's your own business." },
  {
    kind: "in_house",
    label: "Executive inside an organisation",
    hint: "You lead a function or team in a company you don't own.",
  },
  {
    kind: "independent",
    label: "Independent or fractional",
    hint: "You advise or lead for clients.",
  },
  { kind: "other", label: "Other", hint: "Something else — your title says it." },
];

// The server's caps, checked after trimming (memory/workspace_settings.py
// ROLE_TEXT_MAX). Checked here too so the form can say so before saving.
export const ROLE_TEXT_MAX = {
  role_title: 120,
  reports_to: 120,
  remit: 500,
  measured_on: 300,
} as const;

export type RoleTextField = keyof typeof ROLE_TEXT_MAX;

export const ROLE_TEXT_FIELDS = Object.keys(ROLE_TEXT_MAX) as RoleTextField[];

// The form's state: every text field a string ("" = not set).
export interface RoleForm {
  role_kind: RoleKind | null;
  role_title: string;
  reports_to: string;
  remit: string;
  measured_on: string;
}

export const EMPTY_ROLE_FORM: RoleForm = {
  role_kind: null,
  role_title: "",
  reports_to: "",
  remit: "",
  measured_on: "",
};

export function roleFormFrom(role: Partial<PrincipalRole> | null | undefined): RoleForm {
  return {
    role_kind: role?.role_kind ?? null,
    role_title: role?.role_title ?? "",
    reports_to: role?.reports_to ?? "",
    remit: role?.remit ?? "",
    measured_on: role?.measured_on ?? "",
  };
}

function clean(value: string): string | null {
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

// The PUT /workspace body for a form: trimmed, blank as null (which clears
// the field). With `current`, only the fields that differ from it — an
// unchanged form sends {}.
export function roleUpdate(form: RoleForm, current?: Partial<PrincipalRole> | null): WorkspaceUpdate {
  const next: PrincipalRole = {
    role_kind: form.role_kind,
    role_title: clean(form.role_title),
    reports_to: clean(form.reports_to),
    remit: clean(form.remit),
    measured_on: clean(form.measured_on),
  };
  if (!current) return next;
  const out: WorkspaceUpdate = {};
  for (const key of Object.keys(next) as (keyof PrincipalRole)[]) {
    if ((current[key] ?? null) !== next[key]) {
      (out as Record<string, unknown>)[key] = next[key];
    }
  }
  return out;
}

// Fields over their cap, as messages ready to show; empty when the form can
// be saved.
export function roleFormErrors(form: RoleForm): string[] {
  const labels: Record<RoleTextField, string> = {
    role_title: "Title",
    reports_to: "Reports to",
    remit: "What you're responsible for",
    measured_on: "What you're measured on",
  };
  return ROLE_TEXT_FIELDS.filter((f) => form[f].trim().length > ROLE_TEXT_MAX[f]).map(
    (f) => `${labels[f]} is too long (at most ${ROLE_TEXT_MAX[f]} characters).`,
  );
}

export function isRoleEmpty(role: Partial<PrincipalRole> | null | undefined): boolean {
  if (!role) return true;
  return !role.role_kind && ROLE_TEXT_FIELDS.every((f) => !(role[f] ?? "").trim());
}

// One line for a saved role, e.g. "Director of Operations · Executive inside
// an organisation · reports to Dana Ruiz". Empty when nothing is set.
export function describeRole(role: Partial<PrincipalRole> | null | undefined): string {
  if (!role) return "";
  const parts: string[] = [];
  const title = role.role_title?.trim();
  if (title) parts.push(title);
  const option = ROLE_KIND_OPTIONS.find((o) => o.kind === role.role_kind);
  if (option && option.kind !== "other") parts.push(option.label);
  const boss = role.reports_to?.trim();
  if (boss) parts.push(`reports to ${boss}`);
  return parts.join(" · ");
}
