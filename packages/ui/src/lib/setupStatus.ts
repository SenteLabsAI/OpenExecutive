// Settings → Setup status (/settings/status): the shape of the API's
// GET /setup/status answer, and the one light this app works out itself —
// sign-in, whose settings live here rather than in the API.
//
// No imports, so `npm test` can exercise this under
// `node --experimental-strip-types` (see scripts/setupStatus.test.mjs).

export type SetupState = "ok" | "warn" | "error" | "off";

/** One light. Mirrors `SetupCheck` in packages/core/openexecutive/api/setup_checks.py. */
export interface SetupCheck {
  id: string;
  label: string;
  state: SetupState;
  summary: string;
  fix: string | null;
  /** An in-app page that helps with the fix, e.g. "/people". */
  link: string | null;
  /** When the channel last received a message (ISO time). */
  last_activity: string | null;
}

export interface SetupStatus {
  checked_at: string;
  checks: SetupCheck[];
}

export interface SignInEnv {
  localLogin: boolean;
  googleClientId: string | undefined;
  googleClientSecret: string | undefined;
  /** ALLOWED_EMAILS, parsed the way sign-in parses it. */
  allowedEmails: ReadonlySet<string>;
  authUrl: string | undefined;
  publicDeployment: boolean;
}

// RFC 2606 names reserved for examples — the same set as
// _EXAMPLE_EMAIL_DOMAINS in api/setup_checks.py, and
// scripts/setupStatus.test.mjs fails if the two ever disagree.
export const EXAMPLE_EMAIL_DOMAINS: ReadonlySet<string> = new Set([
  "example.com",
  "example.org",
  "example.net",
]);

export function isExampleEmail(email: string): boolean {
  const domain = email.slice(email.lastIndexOf("@") + 1).trim().toLowerCase();
  return EXAMPLE_EMAIL_DOMAINS.has(domain) || domain.endsWith(".example");
}

export function signInCheck(env: SignInEnv): SetupCheck {
  const check = (state: SetupState, summary: string, fix: string | null = null): SetupCheck => ({
    id: "sign_in",
    label: "Sign-in",
    state,
    summary,
    fix,
    link: null,
    last_activity: null,
  });

  if (env.localLogin) {
    return check("ok", "Local login: no sign-in needed, and only this computer can open the app.");
  }
  if (!env.googleClientId?.trim() || !env.googleClientSecret?.trim()) {
    return check(
      "error",
      "Google sign-in isn't fully set up: it needs both AUTH_GOOGLE_ID and AUTH_GOOGLE_SECRET.",
      "Set both from your Google Cloud OAuth client (docs/auth.md), then restart the app.",
    );
  }
  const samples = [...env.allowedEmails].filter(isExampleEmail);
  if (samples.length > 0) {
    return check(
      "warn",
      `ALLOWED_EMAILS still lists sample addresses: ${samples.join(", ")}.`,
      "Replace them in .env with the Google addresses of the people who should sign in, then restart the app.",
    );
  }
  if (env.publicDeployment && !env.authUrl?.trim()) {
    return check(
      "warn",
      "AUTH_URL isn't set, so Google can send people back to the wrong address after they sign in.",
      "Set AUTH_URL to this app's public address (for example https://exec.example.com), then restart the app.",
    );
  }
  const listed = env.allowedEmails.size;
  return check(
    "ok",
    listed > 0
      ? `Google sign-in, for the ${listed} address${listed === 1 ? "" : "es"} in ALLOWED_EMAILS and anyone with an email on the team list.`
      : "Google sign-in, for anyone with an email on the team list.",
  );
}

// Worst first, so what needs fixing is at the top of the page.
const STATE_RANK: Record<SetupState, number> = { error: 0, warn: 1, ok: 2, off: 3 };

/** The lights in the order the page shows them: worst first, and otherwise
 * in the order they arrived. */
export function sortChecks(checks: readonly SetupCheck[]): SetupCheck[] {
  return checks
    .map((check, index) => ({ check, index }))
    .sort((a, b) => STATE_RANK[a.check.state] - STATE_RANK[b.check.state] || a.index - b.index)
    .map(({ check }) => check);
}

/** "5 minutes ago" — how long before `now` an ISO time was. */
export function formatAgo(iso: string, now: Date = new Date()): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "at an unknown time";
  const minutes = Math.floor((now.getTime() - then) / 60_000);
  if (minutes < 1) return "less than a minute ago";
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  return `${Math.floor(hours / 24)} days ago`;
}

/** Only an in-app path is ever rendered as a link. */
export function safeInAppLink(link: string | null): string | null {
  return link && /^\/(?!\/)[\w\-/]*$/.test(link) ? link : null;
}
