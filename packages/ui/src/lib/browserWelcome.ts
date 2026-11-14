// Whether a new chat shows "Your browser is connected" with its "Try one"
// pills (components/browser/BrowserWelcomeCard.tsx). It shows on a new chat
// of your own once your browser is connected, until you use a pill or close
// it (remembered in this browser, `BROWSER_WELCOME_KEY`).

export const BROWSER_WELCOME_KEY = "oe.browserWelcome.closed";

export interface BrowserWelcomeInput {
  /** A new chat of your own, before the first message. */
  landing: boolean;
  /** GET /browser/status: your extension is asking for steps now. */
  connected: boolean;
  starters: readonly string[];
  /** Closed or used before, here or on this page. */
  closed: boolean;
}

export function showBrowserWelcome(i: BrowserWelcomeInput): boolean {
  return i.landing && i.connected && !i.closed && i.starters.length > 0;
}

/** Whether it was closed or used before, in this browser. */
export function browserWelcomeClosed(): boolean {
  try {
    return window.localStorage.getItem(BROWSER_WELCOME_KEY) === "1";
  } catch {
    return false;
  }
}

export function closeBrowserWelcome(): void {
  try {
    window.localStorage.setItem(BROWSER_WELCOME_KEY, "1");
  } catch {
    // Private mode: it shows again next time.
  }
}
