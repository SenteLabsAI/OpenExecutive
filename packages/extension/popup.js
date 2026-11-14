// Open Executive browser extension: the popup. Connects the Open Executive
// site in the current tab (after asking it who it is), lists connected sites,
// lets the person go on after pressing Stop, and turns "Stay connected"
// (its own window for workflows) on or off.

const OE = globalThis.OE;
const $ = (id) => document.getElementById(id);

async function origins() {
  const { origins: list } = await chrome.storage.local.get("origins");
  return Array.isArray(list) ? list : [];
}

/** What to say when this extension and the site speak different versions of
 * the steps, or "" when they match (a site that doesn't say is taken as 1). */
function protocolMismatch(site) {
  const theirs = Number.isInteger(site) ? site : 1;
  if (theirs === OE.PROTOCOL) return "";
  return theirs > OE.PROTOCOL
    ? "This extension is older than your Open Executive. Update the extension, then open this again."
    : "Your Open Executive is older than this extension. Ask whoever runs it to update it, or use an older extension.";
}

async function render() {
  const list = await origins();
  const ul = $("site-list");
  ul.textContent = "";
  for (const origin of list) {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.textContent = origin.replace(/^https:\/\//, "");
    const off = document.createElement("button");
    off.className = "quiet";
    off.type = "button";
    off.textContent = "Disconnect";
    off.addEventListener("click", async () => {
      await chrome.storage.local.set({ origins: (await origins()).filter((o) => o !== origin) });
      void render();
    });
    li.append(name, off);
    ul.append(li);
  }
  $("sites").hidden = list.length === 0;
  const { stayConnected } = await chrome.storage.local.get("stayConnected");
  $("stay").hidden = list.length === 0;
  $("stay-box").checked = stayConnected !== false;

  const { stoppedUntil } = await chrome.storage.session.get("stoppedUntil");
  $("stopped").hidden = !(typeof stoppedUntil === "number" && stoppedUntil > Date.now());

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const origin = OE.originOf(tab?.url);
  $("connect").hidden = true;
  $("none").hidden = list.length > 0;
  if (!tab || !origin || list.includes(origin)) return;
  let who = null;
  try {
    who = await chrome.tabs.sendMessage(tab.id, { type: "hello" });
  } catch {
    // A tab opened before the extension was added has no page script yet.
    try {
      await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["site-rules.js", "shared.js", "content.js"] });
      who = await chrome.tabs.sendMessage(tab.id, { type: "hello" });
    } catch {
      who = null;
    }
  }
  if (!who || !who.ok || who.origin !== origin) {
    if (who && who.signedOut) {
      $("none").hidden = false;
      $("none").querySelector("p").textContent = "Sign in to Open Executive in this tab, then open this again.";
    }
    return;
  }
  $("none").hidden = true;
  const mismatch = protocolMismatch(who.protocol);
  $("update").hidden = !mismatch;
  if (mismatch) {
    $("update-text").textContent = mismatch;
    return;
  }
  $("connect").hidden = false;
  // The site's address first: it is the one thing the site can't choose.
  const name = OE.squeeze(who.exec_name, 40) || "your Executive";
  $("connect-text").textContent =
    `Connect ${origin.replace(/^https:\/\//, "")}? Do it only if that's your Open Executive. It lets ${name} use this browser.`;
  $("connect-btn").onclick = async () => {
    const now = await origins();
    if (!now.includes(origin)) await chrome.storage.local.set({ origins: [...now, origin] });
    void render();
  };
}

$("stay-box").addEventListener("change", async (e) => {
  const on = Boolean(e.target.checked);
  // Turning it back on also undoes closing its window.
  if (on) await chrome.storage.session.set({ ownClosed: false });
  await chrome.storage.local.set({ stayConnected: on });
});

$("resume").addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ type: "resume" });
  void render();
});

void render();
