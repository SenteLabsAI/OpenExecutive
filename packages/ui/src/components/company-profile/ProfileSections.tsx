"use client";

// The company-profile section editors, extracted from app/company-profile/page.tsx
// so the onboarding draft-review screen can reuse the exact editing surface the
// user gets permanently afterwards.
//
// The seam is `onSave`: the page passes a function that PATCHes the backend,
// while onboarding passes one that merges into local draft state. Nothing else
// differs, so there is only ever one implementation of these nine sections.

import { useEffect, useState, useRef } from "react";
import { type CompanyProfile } from "@/lib/api";

// ── helpers ──────────────────────────────────────────────────────────────────

function listToText(items: string[]): string {
  return items.join("\n");
}

function textToList(text: string): string[] {
  return text
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);
}

// ── Ask OE plumbing ──────────────────────────────────────────────────────────
// The page registers one flat form descriptor covering every section; when
// Ask OE proposes values, they land here as `pending` and each section's
// effect merges its own keys into its draft state and flips into edit mode —
// so the user reviews through the section's normal Save button.

export interface PendingValues {
  seq: number;
  values: Record<string, unknown>;
}

/** Accepts a string[] or a newline-joined string (models send either). */
export function coerceList(raw: unknown): string[] | null {
  if (Array.isArray(raw)) return raw.filter((s): s is string => typeof s === "string");
  if (typeof raw === "string") return textToList(raw);
  return null;
}

export const TEXT_FIELDS = new Set([
  "name", "industry", "stage", "mission", "vision",
  "target_customer_profile", "north_star_metric",
]);
export const NUM_FIELDS = new Set([
  "founding_year", "headcount", "annual_revenue_arr",
  "burn_rate_monthly", "runway_months",
]);
export const LIST_FIELDS = new Set([
  "pain_points", "primary_competitors", "competitive_advantages",
  "priorities", "culture_values", "operating_principles",
  "departments", "leadership_team", "vendors", "tickers",
]);

/** Flat snapshot of the SAVED profile — feeds both the Ask OE form
 * descriptor (getFields) and the undo restore values. */
export function snapshotProfile(profile: CompanyProfile): Record<string, unknown> {
  return {
    name: profile.name,
    industry: profile.industry,
    stage: profile.stage,
    founding_year: profile.founding_year,
    headcount: profile.headcount,
    annual_revenue_arr: profile.annual_revenue_arr,
    mission: profile.mission,
    vision: profile.vision,
    target_customer_profile: profile.target_customer.profile,
    pain_points: profile.target_customer.pain_points,
    primary_competitors: profile.competitive_landscape.primary_competitors,
    competitive_advantages: profile.competitive_landscape.competitive_advantages,
    vendors: profile.vendors ?? [],
    tickers: profile.tickers ?? [],
    priorities: profile.strategic_priorities.current_year,
    north_star_metric: profile.strategic_priorities.north_star_metric,
    culture_values: profile.culture.values,
    operating_principles: profile.culture.operating_principles,
    departments: profile.org_structure.departments,
    leadership_team: profile.org_structure.leadership_team,
    burn_rate_monthly: profile.financials.burn_rate_monthly,
    runway_months: profile.financials.runway_months,
  };
}

// ── sub-components ───────────────────────────────────────────────────────────

function FieldLabel({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-xs text-fg-muted font-medium uppercase tracking-wide mb-1">
      {children}
    </p>
  );
}

function FieldValue({ children }: { children: React.ReactNode }) {
  return <p className="text-sm text-fg">{children || <span className="text-fg-subtle italic">Not set</span>}</p>;
}

function Pills({ items }: { items: string[] }) {
  if (!items.length) return <span className="text-sm text-fg-subtle italic">Not set</span>;
  return (
    <div className="flex flex-wrap gap-1.5">
      {items.map((item, i) => (
        <span key={i} className="inline-block px-2 py-0.5 bg-surface-overlay text-fg text-xs rounded-md">
          {item}
        </span>
      ))}
    </div>
  );
}

function Input({
  value,
  onChange,
  type = "text",
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  type?: string;
  placeholder?: string;
}) {
  return (
    <input
      type={type}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      className="w-full rounded-lg border border-line-strong bg-surface-overlay px-3 py-2 text-sm text-fg placeholder-fg-subtle focus:outline-none focus:ring-2 focus:ring-indigo-500/50 focus:border-indigo-500/50 transition-colors"
    />
  );
}

function Textarea({
  value,
  onChange,
  rows = 3,
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  rows?: number;
  placeholder?: string;
}) {
  return (
    <textarea
      value={value}
      onChange={(e) => onChange(e.target.value)}
      rows={rows}
      placeholder={placeholder}
      className="w-full rounded-lg border border-line-strong bg-surface-overlay px-3 py-2 text-sm text-fg placeholder-fg-subtle focus:outline-none focus:ring-2 focus:ring-indigo-500/50 focus:border-indigo-500/50 resize-none transition-colors"
    />
  );
}

interface SectionProps {
  title: string;
  saving: boolean;
  onSave: () => Promise<void>;
  viewContent: React.ReactNode;
  editContent: React.ReactNode;
  // Optional controlled editing — used by Ask OE to flip a section into
  // edit mode when it applies suggested values. Uncontrolled by default.
  editing?: boolean;
  onEditingChange?: (v: boolean) => void;
}

function Section({
  title,
  saving,
  onSave,
  viewContent,
  editContent,
  editing: editingProp,
  onEditingChange,
}: SectionProps) {
  const [editingState, setEditingState] = useState(false);
  const editing = editingProp ?? editingState;
  const setEditing = onEditingChange ?? setEditingState;
  const [error, setError] = useState<string | null>(null);

  async function handleSave() {
    setError(null);
    try {
      await onSave();
      setEditing(false);
    } catch {
      setError("Save failed. Please try again.");
    }
  }

  return (
    <div className="bg-surface-elevated border border-line rounded-xl p-5">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-sm font-semibold text-fg">{title}</h2>
        {!editing && (
          <button
            onClick={() => setEditing(true)}
            className="text-xs text-indigo-400 hover:text-indigo-300 transition-colors"
          >
            Edit
          </button>
        )}
      </div>

      {editing ? editContent : viewContent}

      {editing && (
        <>
          {error && <p className="text-xs text-red-400 mt-3">{error}</p>}
          <div className="flex gap-2 mt-4">
            <button
              onClick={handleSave}
              disabled={saving}
              className="px-3 py-1.5 bg-indigo-500 hover:bg-indigo-600 disabled:opacity-40 text-white text-xs font-medium rounded-lg transition-colors"
            >
              {saving ? "Saving…" : "Save"}
            </button>
            <button
              onClick={() => { setEditing(false); setError(null); }}
              disabled={saving}
              className="px-3 py-1.5 border border-line-strong text-fg-muted hover:text-fg text-xs rounded-lg transition-colors disabled:opacity-40"
            >
              Cancel
            </button>
          </div>
        </>
      )}
    </div>
  );
}

// Merges Ask OE `pending` values into a section's draft state (values are
// already validated/coerced page-side) and flips the section into edit
// mode so the suggestion is visible behind the normal Save button.
// Returns the controlled editing pair for <Section>.
function usePendingSection(
  pending: PendingValues | null,
  appliers: Record<string, (v: unknown) => void>
): [boolean, (v: boolean) => void] {
  const [editing, setEditing] = useState(false);
  const appliersRef = useRef(appliers);
  appliersRef.current = appliers;
  useEffect(() => {
    if (!pending) return;
    let touched = false;
    for (const [key, apply] of Object.entries(appliersRef.current)) {
      if (key in pending.values) {
        apply(pending.values[key]);
        touched = true;
      }
    }
    if (touched) setEditing(true);
  }, [pending]);
  return [editing, setEditing];
}
// ── section components ────────────────────────────────────────────────────────

function CompanyBasicsSection({ profile, saving, onSave, pending }: SectionComponentProps) {
  const [name, setName] = useState(profile.name);
  const [industry, setIndustry] = useState(profile.industry);
  const [stage, setStage] = useState(profile.stage);
  const [foundingYear, setFoundingYear] = useState(profile.founding_year?.toString() ?? "");
  const [headcount, setHeadcount] = useState(profile.headcount?.toString() ?? "");
  const [arr, setArr] = useState(profile.annual_revenue_arr?.toString() ?? "");

  useEffect(() => {
    setName(profile.name); setIndustry(profile.industry); setStage(profile.stage);
    setFoundingYear(profile.founding_year?.toString() ?? "");
    setHeadcount(profile.headcount?.toString() ?? "");
    setArr(profile.annual_revenue_arr?.toString() ?? "");
  }, [profile]);

  const [editing, setEditing] = usePendingSection(pending, {
    name: (v) => setName(String(v)),
    industry: (v) => setIndustry(String(v)),
    stage: (v) => setStage(String(v)),
    founding_year: (v) => setFoundingYear(v == null ? "" : String(v)),
    headcount: (v) => setHeadcount(v == null ? "" : String(v)),
    annual_revenue_arr: (v) => setArr(v == null ? "" : String(v)),
  });

  return (
    <Section
      title="Company Basics"
      editing={editing}
      onEditingChange={setEditing}
      saving={saving}
      onSave={() => onSave({
        name, industry, stage,
        founding_year: foundingYear ? parseInt(foundingYear) : null,
        headcount: headcount ? parseInt(headcount) : null,
        annual_revenue_arr: arr ? parseFloat(arr) : null,
      })}
      viewContent={
        <div className="grid grid-cols-2 gap-x-8 gap-y-4">
          <div><FieldLabel>Name</FieldLabel><FieldValue>{profile.name}</FieldValue></div>
          <div><FieldLabel>Industry</FieldLabel><FieldValue>{profile.industry}</FieldValue></div>
          <div><FieldLabel>Stage</FieldLabel><FieldValue>{profile.stage}</FieldValue></div>
          <div><FieldLabel>Founded</FieldLabel><FieldValue>{profile.founding_year?.toString()}</FieldValue></div>
          <div><FieldLabel>Headcount</FieldLabel><FieldValue>{profile.headcount?.toString()}</FieldValue></div>
          <div><FieldLabel>ARR</FieldLabel><FieldValue>{profile.annual_revenue_arr != null ? `$${profile.annual_revenue_arr.toLocaleString()}` : undefined}</FieldValue></div>
        </div>
      }
      editContent={
        <div className="grid grid-cols-2 gap-3">
          <div><FieldLabel>Name</FieldLabel><Input value={name} onChange={setName} placeholder="Acme Corp" /></div>
          <div><FieldLabel>Industry</FieldLabel><Input value={industry} onChange={setIndustry} placeholder="B2B SaaS" /></div>
          <div><FieldLabel>Stage</FieldLabel><Input value={stage} onChange={setStage} placeholder="Series A" /></div>
          <div><FieldLabel>Founded</FieldLabel><Input value={foundingYear} onChange={setFoundingYear} type="number" placeholder="2022" /></div>
          <div><FieldLabel>Headcount</FieldLabel><Input value={headcount} onChange={setHeadcount} type="number" placeholder="40" /></div>
          <div><FieldLabel>ARR ($)</FieldLabel><Input value={arr} onChange={setArr} type="number" placeholder="500000" /></div>
        </div>
      }
    />
  );
}

function MissionSection({ profile, saving, onSave, pending }: SectionComponentProps) {
  const [mission, setMission] = useState(profile.mission);
  const [vision, setVision] = useState(profile.vision);
  useEffect(() => { setMission(profile.mission); setVision(profile.vision); }, [profile]);

  const [editing, setEditing] = usePendingSection(pending, {
    mission: (v) => setMission(String(v)),
    vision: (v) => setVision(String(v)),
  });

  return (
    <Section
      title="Mission & Vision"
      editing={editing}
      onEditingChange={setEditing}
      saving={saving}
      onSave={() => onSave({ mission, vision })}
      viewContent={
        <div className="space-y-4">
          <div><FieldLabel>Mission</FieldLabel><FieldValue>{profile.mission}</FieldValue></div>
          <div><FieldLabel>Vision</FieldLabel><FieldValue>{profile.vision}</FieldValue></div>
        </div>
      }
      editContent={
        <div className="space-y-3">
          <div><FieldLabel>Mission</FieldLabel><Textarea value={mission} onChange={setMission} rows={2} placeholder="Why does this company exist?" /></div>
          <div><FieldLabel>Vision</FieldLabel><Textarea value={vision} onChange={setVision} rows={2} placeholder="Where are you in 5 years?" /></div>
        </div>
      }
    />
  );
}

function TargetCustomerSection({ profile, saving, onSave, pending }: SectionComponentProps) {
  const [customerProfile, setCustomerProfile] = useState(profile.target_customer.profile);
  const [painPoints, setPainPoints] = useState(listToText(profile.target_customer.pain_points));
  useEffect(() => {
    setCustomerProfile(profile.target_customer.profile);
    setPainPoints(listToText(profile.target_customer.pain_points));
  }, [profile]);

  const [editing, setEditing] = usePendingSection(pending, {
    target_customer_profile: (v) => setCustomerProfile(String(v)),
    pain_points: (v) => setPainPoints(listToText(v as string[])),
  });

  return (
    <Section
      title="Target Customer"
      editing={editing}
      onEditingChange={setEditing}
      saving={saving}
      onSave={() => onSave({ target_customer: { profile: customerProfile, pain_points: textToList(painPoints) } })}
      viewContent={
        <div className="space-y-4">
          <div><FieldLabel>Customer Profile</FieldLabel><FieldValue>{profile.target_customer.profile}</FieldValue></div>
          <div><FieldLabel>Pain Points</FieldLabel><Pills items={profile.target_customer.pain_points} /></div>
        </div>
      }
      editContent={
        <div className="space-y-3">
          <div><FieldLabel>Customer Profile</FieldLabel><Textarea value={customerProfile} onChange={setCustomerProfile} rows={2} placeholder="Who is your ideal customer?" /></div>
          <div><FieldLabel>Pain Points (one per line)</FieldLabel><Textarea value={painPoints} onChange={setPainPoints} rows={3} placeholder={"Too slow to onboard\nNo visibility into data"} /></div>
        </div>
      }
    />
  );
}

function CompetitiveSection({ profile, saving, onSave, pending }: SectionComponentProps) {
  const [competitors, setCompetitors] = useState(listToText(profile.competitive_landscape.primary_competitors));
  const [advantages, setAdvantages] = useState(listToText(profile.competitive_landscape.competitive_advantages));
  useEffect(() => {
    setCompetitors(listToText(profile.competitive_landscape.primary_competitors));
    setAdvantages(listToText(profile.competitive_landscape.competitive_advantages));
  }, [profile]);

  const [editing, setEditing] = usePendingSection(pending, {
    primary_competitors: (v) => setCompetitors(listToText(v as string[])),
    competitive_advantages: (v) => setAdvantages(listToText(v as string[])),
  });

  return (
    <Section
      title="Competitive Landscape"
      editing={editing}
      onEditingChange={setEditing}
      saving={saving}
      onSave={() => onSave({ competitive_landscape: { primary_competitors: textToList(competitors), competitive_advantages: textToList(advantages) } })}
      viewContent={
        <div className="space-y-4">
          <div><FieldLabel>Primary Competitors</FieldLabel><Pills items={profile.competitive_landscape.primary_competitors} /></div>
          <div><FieldLabel>Our Advantages</FieldLabel><Pills items={profile.competitive_landscape.competitive_advantages} /></div>
        </div>
      }
      editContent={
        <div className="space-y-3">
          <div><FieldLabel>Competitors (one per line)</FieldLabel><Textarea value={competitors} onChange={setCompetitors} rows={3} placeholder={"Salesforce\nHubSpot"} /></div>
          <div><FieldLabel>Our Advantages (one per line)</FieldLabel><Textarea value={advantages} onChange={setAdvantages} rows={3} placeholder={"10x faster onboarding\nOpen source"} /></div>
        </div>
      }
    />
  );
}

function ExternalDependenciesSection({ profile, saving, onSave, pending }: SectionComponentProps) {
  const [vendors, setVendors] = useState(listToText(profile.vendors ?? []));
  const [tickers, setTickers] = useState(listToText(profile.tickers ?? []));
  useEffect(() => {
    setVendors(listToText(profile.vendors ?? []));
    setTickers(listToText(profile.tickers ?? []));
  }, [profile]);

  const [editing, setEditing] = usePendingSection(pending, {
    vendors: (v) => setVendors(listToText(v as string[])),
    tickers: (v) => setTickers(listToText(v as string[])),
  });

  return (
    <Section
      title="External Dependencies"
      editing={editing}
      onEditingChange={setEditing}
      saving={saving}
      onSave={() => onSave({ vendors: textToList(vendors), tickers: textToList(tickers) })}
      viewContent={
        <div className="space-y-4">
          <p className="text-xs text-fg-subtle">
            Named here, a vendor or ticker counts as company data: the Executive
            will start watching its status page or filings on its own instead of
            asking you first.
          </p>
          <div><FieldLabel>Vendors &amp; dependencies</FieldLabel><Pills items={profile.vendors ?? []} /></div>
          <div><FieldLabel>Tracked tickers</FieldLabel><Pills items={profile.tickers ?? []} /></div>
        </div>
      }
      editContent={
        <div className="space-y-3">
          <div><FieldLabel>Vendors (one per line)</FieldLabel><Textarea value={vendors} onChange={setVendors} rows={3} placeholder={"Stripe\nAWS"} /></div>
          <div><FieldLabel>Tickers (one per line — yours and competitors&apos;)</FieldLabel><Textarea value={tickers} onChange={setTickers} rows={3} placeholder={"CRM\nHUBS"} /></div>
        </div>
      }
    />
  );
}

function PrioritiesSection({ profile, saving, onSave, pending }: SectionComponentProps) {
  const [priorities, setPriorities] = useState(listToText(profile.strategic_priorities.current_year));
  const [northStar, setNorthStar] = useState(profile.strategic_priorities.north_star_metric);
  useEffect(() => {
    setPriorities(listToText(profile.strategic_priorities.current_year));
    setNorthStar(profile.strategic_priorities.north_star_metric);
  }, [profile]);

  const [editing, setEditing] = usePendingSection(pending, {
    priorities: (v) => setPriorities(listToText(v as string[])),
    north_star_metric: (v) => setNorthStar(String(v)),
  });

  return (
    <Section
      title="Strategic Priorities"
      editing={editing}
      onEditingChange={setEditing}
      saving={saving}
      onSave={() => onSave({ strategic_priorities: { current_year: textToList(priorities), north_star_metric: northStar } })}
      viewContent={
        <div className="space-y-4">
          <div><FieldLabel>This Year's Priorities</FieldLabel><Pills items={profile.strategic_priorities.current_year} /></div>
          <div><FieldLabel>North Star Metric</FieldLabel><FieldValue>{profile.strategic_priorities.north_star_metric}</FieldValue></div>
        </div>
      }
      editContent={
        <div className="space-y-3">
          <div><FieldLabel>Priorities (one per line)</FieldLabel><Textarea value={priorities} onChange={setPriorities} rows={3} placeholder={"Launch v1\nHire 3 engineers"} /></div>
          <div><FieldLabel>North Star Metric</FieldLabel><Input value={northStar} onChange={setNorthStar} placeholder="MRR or DAU" /></div>
        </div>
      }
    />
  );
}

function CultureSection({ profile, saving, onSave, pending }: SectionComponentProps) {
  const [values, setValues] = useState(listToText(profile.culture.values));
  const [principles, setPrinciples] = useState(listToText(profile.culture.operating_principles));
  useEffect(() => {
    setValues(listToText(profile.culture.values));
    setPrinciples(listToText(profile.culture.operating_principles));
  }, [profile]);

  const [editing, setEditing] = usePendingSection(pending, {
    culture_values: (v) => setValues(listToText(v as string[])),
    operating_principles: (v) => setPrinciples(listToText(v as string[])),
  });

  return (
    <Section
      title="Culture & Values"
      editing={editing}
      onEditingChange={setEditing}
      saving={saving}
      onSave={() => onSave({ culture: { values: textToList(values), operating_principles: textToList(principles) } })}
      viewContent={
        <div className="space-y-4">
          <div><FieldLabel>Values</FieldLabel><Pills items={profile.culture.values} /></div>
          <div><FieldLabel>Operating Principles</FieldLabel><Pills items={profile.culture.operating_principles} /></div>
        </div>
      }
      editContent={
        <div className="space-y-3">
          <div><FieldLabel>Values (one per line)</FieldLabel><Textarea value={values} onChange={setValues} rows={3} placeholder={"Transparency\nBias for action"} /></div>
          <div><FieldLabel>Operating Principles (one per line)</FieldLabel><Textarea value={principles} onChange={setPrinciples} rows={3} placeholder={"Default to async\nWrite it down"} /></div>
        </div>
      }
    />
  );
}

function OrgSection({ profile, saving, onSave, pending }: SectionComponentProps) {
  const [departments, setDepartments] = useState(listToText(profile.org_structure.departments));
  const [leadership, setLeadership] = useState(listToText(profile.org_structure.leadership_team));
  useEffect(() => {
    setDepartments(listToText(profile.org_structure.departments));
    setLeadership(listToText(profile.org_structure.leadership_team));
  }, [profile]);

  const [editing, setEditing] = usePendingSection(pending, {
    departments: (v) => setDepartments(listToText(v as string[])),
    leadership_team: (v) => setLeadership(listToText(v as string[])),
  });

  return (
    <Section
      title="Org Structure"
      editing={editing}
      onEditingChange={setEditing}
      saving={saving}
      onSave={() => onSave({ org_structure: { departments: textToList(departments), leadership_team: textToList(leadership) } })}
      viewContent={
        <div className="space-y-4">
          <div><FieldLabel>Departments</FieldLabel><Pills items={profile.org_structure.departments} /></div>
          <div><FieldLabel>Leadership Team</FieldLabel><Pills items={profile.org_structure.leadership_team} /></div>
        </div>
      }
      editContent={
        <div className="space-y-3">
          <div><FieldLabel>Departments (one per line)</FieldLabel><Textarea value={departments} onChange={setDepartments} rows={3} placeholder={"Engineering\nProduct\nGTM"} /></div>
          <div><FieldLabel>Leadership Team (one per line)</FieldLabel><Textarea value={leadership} onChange={setLeadership} rows={3} placeholder={"Alice Chen, CEO\nBob Smith, CTO"} /></div>
        </div>
      }
    />
  );
}

function FinancialsSection({ profile, saving, onSave, pending }: SectionComponentProps) {
  const [burn, setBurn] = useState(profile.financials.burn_rate_monthly?.toString() ?? "");
  const [runway, setRunway] = useState(profile.financials.runway_months?.toString() ?? "");
  useEffect(() => {
    setBurn(profile.financials.burn_rate_monthly?.toString() ?? "");
    setRunway(profile.financials.runway_months?.toString() ?? "");
  }, [profile]);

  const [editing, setEditing] = usePendingSection(pending, {
    burn_rate_monthly: (v) => setBurn(v == null ? "" : String(v)),
    runway_months: (v) => setRunway(v == null ? "" : String(v)),
  });

  return (
    <Section
      title="Financials"
      editing={editing}
      onEditingChange={setEditing}
      saving={saving}
      onSave={() => onSave({
        financials: {
          burn_rate_monthly: burn ? parseFloat(burn) : null,
          runway_months: runway ? parseFloat(runway) : null,
          key_metrics: profile.financials.key_metrics,
        }
      })}
      viewContent={
        <div className="grid grid-cols-2 gap-x-8 gap-y-4">
          <div><FieldLabel>Monthly Burn</FieldLabel><FieldValue>{profile.financials.burn_rate_monthly != null ? `$${profile.financials.burn_rate_monthly.toLocaleString()}/mo` : undefined}</FieldValue></div>
          <div><FieldLabel>Runway</FieldLabel><FieldValue>{profile.financials.runway_months != null ? `${profile.financials.runway_months} months` : undefined}</FieldValue></div>
        </div>
      }
      editContent={
        <div className="grid grid-cols-2 gap-3">
          <div><FieldLabel>Monthly Burn ($)</FieldLabel><Input value={burn} onChange={setBurn} type="number" placeholder="50000" /></div>
          <div><FieldLabel>Runway (months)</FieldLabel><Input value={runway} onChange={setRunway} type="number" placeholder="18" /></div>
        </div>
      }
    />
  );
}

interface SectionComponentProps {
  profile: CompanyProfile;
  saving: boolean;
  onSave: (patch: Partial<CompanyProfile>) => Promise<void>;
  // Ask OE suggested values (flat keys) — sections merge their own keys
  // into draft state and flip into edit mode when one lands.
  pending: PendingValues | null;
}

// ── composed section list ────────────────────────────────────────────────────

export type SectionId =
  | "basics"
  | "mission"
  | "customer"
  | "competitive"
  | "dependencies"
  | "priorities"
  | "culture"
  | "org"
  | "financials";

const SECTION_ORDER: { id: SectionId; Component: (p: SectionComponentProps) => React.ReactElement }[] = [
  { id: "basics", Component: CompanyBasicsSection },
  { id: "mission", Component: MissionSection },
  { id: "customer", Component: TargetCustomerSection },
  { id: "competitive", Component: CompetitiveSection },
  // vendors + tickers the research policy may watch on its own
  { id: "dependencies", Component: ExternalDependenciesSection },
  { id: "priorities", Component: PrioritiesSection },
  { id: "culture", Component: CultureSection },
  { id: "org", Component: OrgSection },
  { id: "financials", Component: FinancialsSection },
];

export interface ProfileSectionsProps {
  profile: CompanyProfile;
  saving: boolean;
  /** PATCHes the backend on the profile page; merges into local state on the
   * onboarding draft screen. That swap is the whole reason this is a component. */
  onSave: (patch: Partial<CompanyProfile>) => Promise<void>;
  /** Ask OE suggested values. Only the profile page registers a form, so the
   * onboarding draft screen leaves this null. */
  pending?: PendingValues | null;
  /** Sections to leave out. Onboarding omits "org" because org_structure is
   * derived from its people and department tables at commit time — rendering
   * it here too would give the user two places to edit the same thing. */
  omit?: SectionId[];
}

export function ProfileSections({
  profile,
  saving,
  onSave,
  pending = null,
  omit = [],
}: ProfileSectionsProps) {
  const hidden = new Set(omit);
  return (
    <div className="flex flex-col gap-4">
      {SECTION_ORDER.filter(({ id }) => !hidden.has(id)).map(({ id, Component }) => (
        <Component key={id} profile={profile} saving={saving} onSave={onSave} pending={pending} />
      ))}
    </div>
  );
}
