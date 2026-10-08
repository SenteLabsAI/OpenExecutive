// The chips under a chat reply (`action_taken` events): repeats of the same
// action collapse into one chip with a count, and a chip can list what each
// run looked at. Kept apart from the component so `npm test` can check it
// (see scripts/actionChips.test.mjs). The wording comes from the backend
// (orchestrator/action_chips.py and tool_labels.py); this file only groups.

export interface ChipAction {
  tool: string;
  summary: string;
  target?: string | null;
  link?: string | null;
}

export interface ChipRun {
  target: string | null;
  link: string | null;
}

export interface ChipGroup {
  key: string;
  tool: string;
  summary: string;
  runs: ChipRun[];
}

// Replies saved before connected tools had plain names carry
// "Called google_workspace__search_drive_files". Show those as words too.
const LEGACY_CALLED = /^Called ([A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+)$/;

/** The chip's wording, with an old raw tool name turned into words. */
export function chipSummary(summary: string): string {
  const m = LEGACY_CALLED.exec(summary);
  if (!m) return summary;
  const tool = m[1].slice(m[1].indexOf("__") + 2);
  const words = tool.replace(/[_-]+/g, " ").trim();
  return words ? `Used ${words}` : "Used a connected tool";
}

/** One chip per distinct wording, in the order each first appeared. */
export function groupActions(actions: ChipAction[]): ChipGroup[] {
  const groups = new Map<string, ChipGroup>();
  for (const action of actions) {
    const summary = chipSummary(action.summary);
    let group = groups.get(summary);
    if (!group) {
      group = { key: summary, tool: action.tool, summary, runs: [] };
      groups.set(summary, group);
    }
    group.runs.push({ target: action.target || null, link: action.link || null });
  }
  return Array.from(groups.values());
}

/** A connected tool's raw name has a `server__` prefix; built-in tools don't. */
function isConnectedTool(tool: string): boolean {
  return tool.includes("__");
}

/**
 * Whether tapping the chip opens the list of runs. A repeat always does. A
 * single built-in action already says what it did in its own words (and may
 * link somewhere), so only a single connected-tool run with a target opens.
 */
export function opensDetails(group: ChipGroup): boolean {
  if (group.runs.length > 1) return true;
  const run = group.runs[0];
  return !!run && !run.link && !!run.target && isConnectedTool(group.tool);
}
