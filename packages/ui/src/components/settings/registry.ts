import { IconName } from "@/components/Icon";

export type SettingsPhase = 1 | 2 | 3 | 4;
export type SettingsRole = "owner" | "admin" | "agent";
export type ControlKind =
  | "text"
  | "number"
  | "toggle"
  | "select"
  | "link"
  | "secret-status"
  | "readonly"
  | "list";

export interface SettingsControl {
  id: string;
  label: string;
  hint: string;
  kind: ControlKind;
  liveInPhase: SettingsPhase;
  role: SettingsRole;
  storage: "env" | "settings.yaml" | "profile.yaml" | "manifest" | "derived";
  restartRequired?: boolean;
  locked?: boolean;
  lockedReason?: string;
  href?: string;
  options?: string[];
  defaultValue?: string | number | boolean;
}

export interface SettingsTab {
  id: string;
  label: string;
  description: string;
  icon: IconName;
  phase: SettingsPhase;
  controls: SettingsControl[];
}

export const SETTINGS_TABS: SettingsTab[] = [
  {
    id: "general",
    label: "General",
    description: "Instance name, timezone, language.",
    icon: "cog",
    phase: 1,
    controls: [
      { id: "instance_name", label: "Instance name", hint: "Shown in the UI chrome.", kind: "text", liveInPhase: 1, role: "owner", storage: "settings.yaml", defaultValue: "Open Executive" },
      { id: "timezone", label: "Timezone", hint: "Scheduler and briefing clock.", kind: "select", liveInPhase: 1, role: "admin", storage: "env", options: ["Europe/Bucharest", "UTC", "Europe/London", "America/New_York"], defaultValue: "Europe/Bucharest" },
      { id: "locale", label: "UI language", hint: "Interface copy.", kind: "select", liveInPhase: 1, role: "admin", storage: "settings.yaml", options: ["ro", "en"], defaultValue: "ro" },
    ],
  },
  {
    id: "company",
    label: "Company",
    description: "Identity and strategy.",
    icon: "building",
    phase: 1,
    controls: [
      { id: "profile", label: "Company profile", hint: "Existing OE profile editor.", kind: "link", liveInPhase: 1, role: "admin", storage: "profile.yaml", href: "/company-profile" },
    ],
  },
  {
    id: "people",
    label: "People & access",
    description: "Roster and roles.",
    icon: "users",
    phase: 1,
    controls: [
      { id: "roster", label: "People roster", hint: "Who the Executive coordinates with.", kind: "link", liveInPhase: 1, role: "admin", storage: "derived", href: "/people" },
    ],
  },
  {
    id: "packs",
    label: "Packs",
    description: "Pack 0 is Executive.",
    icon: "grid",
    phase: 1,
    controls: [
      { id: "active_pack", label: "Active pack", hint: "F1 only ships executive.", kind: "select", liveInPhase: 1, role: "owner", storage: "settings.yaml", options: ["executive"], defaultValue: "executive", locked: true, lockedReason: "More packs in Phase 3." },
      { id: "voice", label: "Voice", hint: "One voice per pack.", kind: "readonly", liveInPhase: 1, role: "owner", storage: "manifest", defaultValue: "single", locked: true, lockedReason: "voice=multi is not a setting." },
    ],
  },
  {
    id: "models",
    label: "Models",
    description: "Default, deep, routing.",
    icon: "bolt",
    phase: 1,
    controls: [
      { id: "council", label: "Agent Council", hint: "Per-specialist models.", kind: "link", liveInPhase: 1, role: "owner", storage: "derived", href: "/council" },
      { id: "openrouter", label: "OpenRouter", hint: "Off in Phase 1.", kind: "toggle", liveInPhase: 2, role: "owner", storage: "env", defaultValue: false, locked: true, lockedReason: "Phase 2+" },
    ],
  },
  {
    id: "memory",
    label: "Memory",
    description: "Ambient writeback off.",
    icon: "activity",
    phase: 1,
    controls: [
      { id: "ambient_writeback", label: "Ambient writeback", hint: "Default off.", kind: "toggle", liveInPhase: 1, role: "owner", storage: "settings.yaml", defaultValue: false, locked: true, lockedReason: "Off until HITL inbox." },
      { id: "pulse", label: "Pulse", hint: "OE memory surface.", kind: "link", liveInPhase: 1, role: "agent", storage: "derived", href: "/memories" },
    ],
  },
  {
    id: "knowledge",
    label: "Knowledge",
    description: "RAG chain.",
    icon: "book",
    phase: 1,
    controls: [
      { id: "kb", label: "Knowledge base", hint: "Upload documents.", kind: "link", liveInPhase: 1, role: "admin", storage: "derived", href: "/knowledge" },
    ],
  },
  {
    id: "metrics",
    label: "Metrics",
    description: "Scorecard — Phase 2.",
    icon: "activity",
    phase: 2,
    controls: [
      { id: "scorecard", label: "Scorecard", hint: "Cash, GTM, product, risk.", kind: "readonly", liveInPhase: 2, role: "admin", storage: "settings.yaml", defaultValue: "reserved" },
    ],
  },
  {
    id: "tools",
    label: "Tools / MCP",
    description: "Outbound MCP. Off in Phase 1.",
    icon: "bolt",
    phase: 1,
    controls: [
      { id: "mcp_enabled", label: "MCP enabled", hint: "Must stay false in Phase 1.", kind: "toggle", liveInPhase: 1, role: "owner", storage: "env", defaultValue: false, locked: true, lockedReason: "Phase 1 freeze.", restartRequired: true },
    ],
  },
  {
    id: "inbox",
    label: "Inbox / HITL",
    description: "Unread / Action / Done.",
    icon: "clipboard",
    phase: 2,
    controls: [
      { id: "queues", label: "Queues", hint: "Phase 2.", kind: "readonly", liveInPhase: 2, role: "admin", storage: "derived", defaultValue: "reserved" },
    ],
  },
  {
    id: "guardrails",
    label: "Guardrails",
    description: "Five rails.",
    icon: "check-circle",
    phase: 1,
    controls: [
      { id: "profile", label: "Profile", hint: "standard or strict.", kind: "select", liveInPhase: 1, role: "owner", storage: "settings.yaml", options: ["standard", "strict"], defaultValue: "standard" },
    ],
  },
  {
    id: "budget",
    label: "Budget",
    description: "Daily cap.",
    icon: "activity",
    phase: 1,
    controls: [
      { id: "usd_day", label: "Daily USD cap", hint: "Hard stop.", kind: "number", liveInPhase: 1, role: "owner", storage: "settings.yaml", defaultValue: 25 },
      { id: "usage", label: "Token usage", hint: "OE usage page.", kind: "link", liveInPhase: 1, role: "admin", storage: "derived", href: "/audit/usage" },
    ],
  },
  {
    id: "channels",
    label: "Channels",
    description: "Slack, Discord, email — unset in F1.",
    icon: "users",
    phase: 1,
    controls: [
      { id: "slack", label: "Slack", hint: "Unset in Phase 1.", kind: "secret-status", liveInPhase: 1, role: "owner", storage: "env" },
    ],
  },
  {
    id: "cadence",
    label: "Cadence",
    description: "Scheduler.",
    icon: "doc",
    phase: 1,
    controls: [
      { id: "jobs", label: "Jobs", hint: "OE workflows.", kind: "link", liveInPhase: 1, role: "admin", storage: "derived", href: "/jobs" },
    ],
  },
  {
    id: "embed",
    label: "Embed / Host",
    description: "Inbound MCP — Phase 3.",
    icon: "grid",
    phase: 3,
    controls: [
      { id: "inbound", label: "Inbound MCP", hint: "Phase 3.", kind: "toggle", liveInPhase: 3, role: "owner", storage: "settings.yaml", defaultValue: false },
    ],
  },
  {
    id: "ledger",
    label: "Ledger",
    description: "Audit snapshots.",
    icon: "doc-search",
    phase: 2,
    controls: [
      { id: "audit", label: "Audit log", hint: "OE event log.", kind: "link", liveInPhase: 1, role: "admin", storage: "derived", href: "/audit" },
    ],
  },
  {
    id: "ops",
    label: "Ops / Doctor",
    description: "Health and MCP off check.",
    icon: "eye",
    phase: 1,
    controls: [
      { id: "mcp_must_be_false", label: "MCP_ENABLED is false", hint: "F1 lock.", kind: "readonly", liveInPhase: 1, role: "owner", storage: "env", defaultValue: "false" },
    ],
  },
];

export function getTab(id: string | undefined): SettingsTab {
  return SETTINGS_TABS.find((t) => t.id === id) ?? SETTINGS_TABS[0];
}
