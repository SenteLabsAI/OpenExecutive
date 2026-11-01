// Finding someone on the People page: search, the New / Needs a look filters,
// sorting (name, last contact, company) and the company groups.
//
// Type-only imports, so `npm test` can exercise this under
// `node --experimental-strip-types` (see scripts/peopleList.test.mjs).

import type { Person } from "@/lib/api";

export type PeopleFilter = "all" | "new" | "needs_look";
export type PeopleSort = "name" | "recent" | "company";

/** When the principal last emailed, heard from or met someone (GET /contacts/last-contact). */
export interface LastContact {
  person_id: number;
  at: string;
  how: "emailed" | "heard" | "met" | string;
}

type Row = Pick<Person, "id" | "full_name" | "role" | "email"> & { email_aliases?: string[] };

// Personal and ISP mail: a shared domain here says nothing about a company.
const PERSONAL_DOMAINS = new Set([
  "gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "live.com", "msn.com",
  "icloud.com", "me.com", "mac.com", "aol.com", "proton.me", "protonmail.com", "gmx.com",
  "comcast.net", "att.net", "verizon.net", "windstream.net", "bellsouth.net", "sbcglobal.net",
]);

// A part of a role that is only a legal suffix ("…, LLC") is not the company.
const SUFFIX_ONLY = /^(llc|l\.l\.c\.|inc\.?|p\.?c\.?|llp|ltd\.?|co\.?|corp\.?|plc|gmbh)$/i;

function addresses(p: Row): string[] {
  return [p.email ?? "", ...(p.email_aliases ?? [])].map((a) => a.trim().toLowerCase()).filter(Boolean);
}

/** Does the person match what was typed? Name, role or any of their addresses. */
export function matchesSearch(p: Row, query: string): boolean {
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return true;
  const hay = [p.full_name, p.role, ...addresses(p)].join(" ").toLowerCase();
  return words.every((w) => hay.includes(w));
}

/** People who share an address with someone else in the list: likely the same person twice. */
export function duplicateIds(people: Row[]): Set<number> {
  const byAddress = new Map<string, number[]>();
  for (const p of people) {
    for (const a of new Set(addresses(p))) byAddress.set(a, [...(byAddress.get(a) ?? []), p.id]);
  }
  const out = new Set<number>();
  for (const ids of byAddress.values()) if (ids.length > 1) ids.forEach((id) => out.add(id));
  return out;
}

/** The company in a role line: "VP Finance, CAHEC Management" → "CAHEC Management". */
export function companyFromRole(role: string): string {
  const parts = role
    .replace(/\([^)]*\)/g, "")
    .split(/[,·|–—]| - | at /)
    .map((s) => s.trim())
    .filter((s) => s && !SUFFIX_ONLY.test(s));
  return parts.length > 1 ? parts[parts.length - 1] : "";
}

/** The company's email domain, or "" for personal mail and no address. */
export function companyDomain(p: Row): string {
  const domain = (p.email ?? "").split("@")[1]?.trim().toLowerCase() ?? "";
  return domain && !PERSONAL_DOMAINS.has(domain) ? domain : "";
}

export interface PeopleGroup<P extends Row> {
  key: string;
  label: string;
  people: P[];
}

const byName = <P extends Row>(a: P, b: P) =>
  a.full_name.localeCompare(b.full_name, undefined, { sensitivity: "base" }) || a.id - b.id;

function nameFromDomain(domain: string): string {
  const base = domain.split(".").slice(0, -1).join(".") || domain;
  return base.charAt(0).toUpperCase() + base.slice(1);
}

/**
 * People grouped by company: the same work domain, else the company in their
 * role. A group is named by the company most of its people's roles give.
 * People with neither go last, under "Other".
 */
export function groupByCompany<P extends Row>(people: P[]): PeopleGroup<P>[] {
  const groups = new Map<string, P[]>();
  for (const p of people) {
    const domain = companyDomain(p);
    const fromRole = companyFromRole(p.role);
    const key = domain ? `d:${domain}` : fromRole ? `r:${fromRole.toLowerCase()}` : "";
    groups.set(key, [...(groups.get(key) ?? []), p]);
  }
  const named = [...groups.entries()].map(([key, members]) => {
    const counts = new Map<string, number>();
    for (const m of members) {
      const c = companyFromRole(m.role);
      if (c) counts.set(c, (counts.get(c) ?? 0) + 1);
    }
    const best = [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].length - b[0].length)[0]?.[0];
    const label = key === "" ? "Other" : best ?? nameFromDomain(key.slice(2));
    return { key, label, people: [...members].sort(byName) };
  });
  return named.sort((a, b) =>
    a.key === "" ? 1 : b.key === "" ? -1 : a.label.localeCompare(b.label, undefined, { sensitivity: "base" }),
  );
}

/** The list in the chosen order. "recent" puts people with no known contact last, by name. */
export function sortPeople<P extends Row>(people: P[], sort: PeopleSort, last: Map<number, LastContact>): P[] {
  const out = [...people].sort(byName);
  if (sort === "recent") {
    out.sort((a, b) => (last.get(b.id)?.at ?? "").localeCompare(last.get(a.id)?.at ?? ""));
  }
  if (sort === "company") return groupByCompany(out).flatMap((g) => g.people);
  return out;
}

/** "Emailed Oct 29", "Heard from Oct 30", "Met Oct 22", or "" when unknown. */
export function lastContactLabel(lc: LastContact | undefined): string {
  if (!lc) return "";
  const when = new Date(lc.at);
  if (Number.isNaN(when.getTime())) return "";
  const day = when.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  const verb = lc.how === "emailed" ? "Emailed" : lc.how === "met" ? "Met" : "Heard from";
  return `${verb} ${day}`;
}
