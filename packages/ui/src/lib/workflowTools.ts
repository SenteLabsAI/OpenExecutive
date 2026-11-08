// What a workflow's tools let it do, in plain words: each tool's app, the
// action it takes, and every tool grouped by app for the review card. Pure,
// so the node test runner can load it (scripts/workflowTools.test.mjs).

/** The icon a tool's app is drawn with. */
export type ToolKind = "browser" | "email" | "calendar" | "files" | "messages" | "other";

export interface ToolApp {
  label: string;
  kind: ToolKind;
}

function split(name: string): { server: string; tool: string } {
  const n = name.toLowerCase();
  const cut = n.indexOf("__");
  return cut > 0
    ? { server: n.slice(0, cut), tool: n.slice(cut + 2) }
    : { server: "", tool: n };
}

function titleWords(s: string): string {
  const words = s.replace(/[_-]+/g, " ").trim();
  return words.replace(/\b\w/g, (c) => c.toUpperCase());
}

/**
 * The app a tool works in. Tool names are `server__tool`
 * (`google_workspace__send_gmail_message`, `oe__browser_click`).
 */
export function toolApp(name: string): ToolApp {
  const { server, tool } = split(name);
  const google = server === "google_workspace";
  const microsoft = /ms365|microsoft|outlook|office/.test(server);
  if (server === "oe" && tool.startsWith("browser_")) return { label: "Browser", kind: "browser" };
  // Named by the server only: `hubspot__post_to_slack_channel` is HubSpot.
  if (/slack/.test(server)) return { label: "Slack", kind: "messages" };
  if (/teams/.test(server)) return { label: "Teams", kind: "messages" };
  // Mail and calendar words only name the app on servers that have them:
  // `hubspot__create_email_campaign` is HubSpot, not Email.
  const ownsMail = google || microsoft || server === "oe";
  if (ownsMail && /gmail|mail/.test(tool)) {
    return {
      label: google ? "Gmail" : microsoft ? "Outlook" : "Email",
      kind: "email",
    };
  }
  if (ownsMail && /calendar|event|freebusy/.test(tool)) {
    return {
      label: google ? "Google Calendar" : microsoft ? "Outlook calendar" : "Calendar",
      kind: "calendar",
    };
  }
  if (microsoft && /onedrive|sharepoint|drive|file|folder/.test(tool)) {
    return { label: "OneDrive", kind: "files" };
  }
  if (google && /drive|doc|sheet|spreadsheet|folder|presentation|slides/.test(tool)) {
    return { label: "Google Drive", kind: "files" };
  }
  if (server === "oe") {
    if (/message|notify/.test(tool)) return { label: "Messages", kind: "messages" };
    if (/file/.test(tool)) return { label: "Files", kind: "files" };
    return { label: "Open Executive", kind: "other" };
  }
  if (!server) return { label: "Other", kind: "other" };
  return { label: titleWords(server), kind: "other" };
}

// The browser tools are verbs on their own; say what each does to a page.
const BROWSER_ACTIONS: Record<string, string> = {
  navigate: "open pages",
  look: "read pages",
  read: "read pages",
  click: "click",
  type: "type",
  scroll: "scroll",
};

/** What a tool does, lowercase: "open pages", "send gmail message". */
export function toolAction(name: string): string {
  const { server, tool } = split(name);
  if (server === "oe" && tool.startsWith("browser_")) {
    const verb = tool.slice("browser_".length);
    return BROWSER_ACTIONS[verb] ?? verb.replace(/[_-]+/g, " ").trim();
  }
  return tool.replace(/[_-]+/g, " ").trim();
}

/** One app's row in "what it can do": the tools that may change things, and the ones that only look. */
export interface ToolGroup {
  app: ToolApp;
  changes: { action: string; tools: string[] }[];
  looks: { action: string; tools: string[] }[];
}

/**
 * `names` grouped by app, apps in the order first used. Tools that do the
 * same thing (two browser reads) share one action. `readOnly` says which
 * tools only look; any other tool is listed as one that may change things.
 */
export function groupTools(names: string[], readOnly: (name: string) => boolean): ToolGroup[] {
  const groups: ToolGroup[] = [];
  for (const name of Array.from(new Set(names))) {
    const app = toolApp(name);
    let group = groups.find((g) => g.app.label === app.label);
    if (!group) {
      group = { app, changes: [], looks: [] };
      groups.push(group);
    }
    const list = readOnly(name) ? group.looks : group.changes;
    const action = toolAction(name);
    const same = list.find((a) => a.action === action);
    if (same) same.tools.push(name);
    else list.push({ action, tools: [name] });
  }
  return groups;
}

/** The apps a step's tools work in, in order, once each. */
export function stepApps(tools: string[]): ToolApp[] {
  const out: ToolApp[] = [];
  for (const t of tools) {
    const app = toolApp(t);
    if (!out.some((a) => a.label === app.label)) out.push(app);
  }
  return out;
}
