// Open Executive browser extension: the script on every page.
//
// On a connected Open Executive site it is the relay: it asks the site for the
// next step the Executive wants done (signed in as the person, same origin),
// hands it to the background, and posts back what happened. On any page it
// does the steps the background sends it (look, click, type, pick, key,
// scroll) and shows the Stop bar while it works. A step handed over to the
// person shows the "Waiting on you" bar until they press Done or Stop.

(function () {
  if (window.top !== window) return;
  if (globalThis.__oeLoaded) return;
  globalThis.__oeLoaded = true;
  const OE = globalThis.OE;

  // ---- The relay (connected Open Executive sites only) ----

  const API = "/api/backend/browser";
  let relaying = false;

  async function connectedOrigins() {
    try {
      const { origins } = await chrome.storage.local.get("origins");
      return Array.isArray(origins) ? origins : [];
    } catch {
      return [];
    }
  }

  const pause = (ms) => new Promise((ok) => setTimeout(ok, ms));

  // Whether "Stay connected" is ticked, so Settings → Browser can say
  // whether their workflows can use this browser.
  async function stayParam() {
    try {
      const { stayConnected } = await chrome.storage.local.get("stayConnected");
      return stayConnected === false ? "0" : "1";
    } catch {
      return "1";
    }
  }

  async function relay() {
    if (relaying) return;
    relaying = true;
    let backoff = 1000;
    try {
      while (OE.onConnected(location.href, await connectedOrigins())) {
        let res;
        try {
          res = await fetch(`${API}/steps?wait=20&stay=${await stayParam()}`, { cache: "no-store", credentials: "same-origin" });
        } catch {
          await pause(backoff);
          backoff = Math.min(backoff * 2, 30000);
          continue;
        }
        if (!res.ok) {
          // Signed out, or not on the team: try again now and then.
          await pause(res.status === 401 || res.status === 403 ? 30000 : backoff);
          backoff = Math.min(backoff * 2, 30000);
          continue;
        }
        backoff = 1000;
        const { step } = await res.json().catch(() => ({ step: null }));
        if (!step || typeof step.id !== "string") continue;
        let result;
        try {
          result = await chrome.runtime.sendMessage({ type: "step", step });
        } catch {
          result = { ok: false, error: "The browser extension didn't answer. Ask them to reload this tab." };
        }
        await fetch(`${API}/steps/${encodeURIComponent(step.id)}`, {
          method: "POST",
          credentials: "same-origin",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(result || { ok: false, error: "No answer from the browser." }),
        }).catch(() => undefined);
      }
    } finally {
      relaying = false;
    }
  }

  chrome.storage.onChanged.addListener((changes) => {
    if (changes.origins) void relay();
  });
  void relay();

  async function hello() {
    try {
      const res = await fetch(`${API}/hello`, { cache: "no-store", credentials: "same-origin" });
      if (res.status === 401) return { ok: false, signedOut: true };
      if (!res.ok) return { ok: false };
      const body = await res.json();
      if (typeof body.exec_name !== "string") return { ok: false };
      const protocol = Number.isInteger(body.protocol) ? body.protocol : null;
      return {
        ok: true,
        origin: OE.originOf(location.href),
        exec_name: OE.squeeze(body.exec_name, 40),
        first_name: OE.squeeze(body.first_name || "", 40),
        protocol,
      };
    } catch {
      return { ok: false };
    }
  }

  // ---- Doing steps on this page ----

  let refs = new Map();

  function visible(el) {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    const s = getComputedStyle(el);
    return s.visibility !== "hidden" && s.display !== "none" && Number(s.opacity) > 0.05;
  }

  function attrs(el) {
    return {
      type: el.getAttribute("type"),
      autocomplete: el.getAttribute("autocomplete"),
      name: el.getAttribute("name"),
      id: el.id,
      ariaLabel: el.getAttribute("aria-label"),
      placeholder: el.getAttribute("placeholder"),
      label: el.labels ? Array.from(el.labels).map((l) => l.innerText).join(" ") : "",
    };
  }

  function labelOf(el) {
    const aria = el.getAttribute("aria-label");
    if (aria) return aria;
    const by = el.getAttribute("aria-labelledby");
    if (by) {
      const t = by.split(/\s+/).map((id) => document.getElementById(id)?.innerText || "").join(" ").trim();
      if (t) return t;
    }
    if (el.labels && el.labels.length) return Array.from(el.labels).map((l) => l.innerText).join(" ");
    return (
      el.getAttribute("placeholder") ||
      el.innerText ||
      el.getAttribute("title") ||
      el.getAttribute("alt") ||
      el.getAttribute("name") ||
      // A button's value is its label; a field's value is never one.
      (el.tagName === "INPUT" && ["submit", "button", "reset"].includes((el.getAttribute("type") || "").toLowerCase())
        ? el.value
        : "") ||
      ""
    );
  }

  /** What an element is and its label: the name a held step's card shows,
   * and what an approved step checks is still the element it acts on. */
  function nameOf(el) {
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute("role");
    const type = (el.getAttribute("type") || "").toLowerCase();
    let kind = role || tag;
    if (tag === "input") kind = type ? `${type} field` : "field";
    if (tag === "textarea") kind = "text box";
    if (tag === "a") kind = "link";
    if (el.isContentEditable && tag !== "input" && tag !== "textarea") kind = "editable text";
    return `${kind} ${JSON.stringify(OE.squeeze(labelOf(el), 80).replace(/\n/g, " "))}`;
  }

  function describe(el) {
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute("role");
    const type = (el.getAttribute("type") || "").toLowerCase();
    const parts = [nameOf(el)];
    if (OE.sensitiveField(attrs(el))) {
      parts.push("(sensitive: they fill this one)");
    } else if ((tag === "input" && !["checkbox", "radio", "submit", "button"].includes(type)) || tag === "textarea") {
      if (el.value && !el.readOnly) parts.push(`value=${JSON.stringify(OE.squeeze(el.value, 60))}`);
    }
    if (tag === "select") {
      const opt = el.options[el.selectedIndex];
      parts.push(`selected=${JSON.stringify(opt ? opt.text.trim() : "")}`);
      const list = Array.from(el.options).slice(0, 12).map((o) => o.text.trim());
      parts.push(`options=${JSON.stringify(list)}${el.options.length > 12 ? "…" : ""}`);
    }
    if (type === "checkbox" || type === "radio" || role === "checkbox" || role === "switch") {
      parts.push(el.checked || el.getAttribute("aria-checked") === "true" ? "checked" : "not checked");
    }
    if (el.disabled || el.getAttribute("aria-disabled") === "true") parts.push("disabled");
    if (tag === "a" && el.href) parts.push(`-> ${OE.squeeze(el.href, 100)}`);
    return parts.join(" ");
  }

  const INTERACTIVE = [
    "a[href]", "button", "input:not([type=hidden])", "select", "textarea", "summary",
    "[role=button]", "[role=link]", "[role=checkbox]", "[role=radio]", "[role=tab]", "[role=menuitem]",
    "[role=option]", "[role=combobox]", "[role=switch]", "[role=textbox]", "[contenteditable='']",
    "[contenteditable='true']",
  ].join(",");
  const MAX_ELEMENTS = 300;

  function look() {
    refs = new Map();
    const lines = [];
    let n = 0;
    let more = false;
    for (const el of document.querySelectorAll(INTERACTIVE)) {
      if (!visible(el)) continue;
      if (n >= MAX_ELEMENTS) {
        more = true;
        break;
      }
      n += 1;
      refs.set(n, el);
      lines.push(`[${n}] ${describe(el)}`);
    }
    return {
      ok: true,
      url: location.href,
      title: document.title,
      text: OE.squeeze(document.body ? document.body.innerText : "", 12000),
      elements: lines.join("\n") + (more ? `\n(more below: scroll down and look again)` : ""),
      scrolled: Math.round((window.scrollY / Math.max(1, document.documentElement.scrollHeight - innerHeight)) * 100),
      // The visible area, so the background can map its screenshot to the page.
      view: viewNow(),
    };
  }

  // What a step acts on: the focused element for a key, the element at a
  // point of the screenshot for click_at, else the one with that number.
  function targetOf(action, args) {
    if (action === "key") return document.activeElement;
    if (action === "click_at") return elementAt(args.cx, args.cy);
    return element(args.ref);
  }

  // The viewport's size and scroll, in CSS pixels.
  function viewNow() {
    return { width: innerWidth, height: innerHeight, x: Math.round(scrollX), y: Math.round(scrollY) };
  }

  // The page's element at a point, looking past the Stop bar.
  function elementAt(x, y) {
    if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
    const shown = bar && bar.isConnected;
    if (shown) bar.remove();
    try {
      return document.elementFromPoint(x, y);
    } finally {
      if (shown) document.documentElement.appendChild(bar);
    }
  }

  function element(ref) {
    const el = refs.get(Number(ref));
    if (!el || !el.isConnected) return null;
    return el;
  }

  const GONE = { ok: false, error: "That element isn't on the page any more. Look again." };

  function setValue(el, text) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) setter.call(el, text);
    else el.value = text;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function pressEnter(el) {
    const opts = { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true };
    const go = el.dispatchEvent(new KeyboardEvent("keydown", opts));
    el.dispatchEvent(new KeyboardEvent("keypress", opts));
    el.dispatchEvent(new KeyboardEvent("keyup", opts));
    if (go && el.form && el.tagName === "INPUT") el.form.requestSubmit();
  }

  function click(el) {
    el.scrollIntoView({ block: "center", inline: "center" });
    const r = el.getBoundingClientRect();
    const at = { bubbles: true, cancelable: true, clientX: r.left + r.width / 2, clientY: r.top + r.height / 2, view: window };
    el.dispatchEvent(new PointerEvent("pointerdown", at));
    el.dispatchEvent(new MouseEvent("mousedown", at));
    if (typeof el.focus === "function") el.focus({ preventScroll: true });
    el.dispatchEvent(new PointerEvent("pointerup", at));
    el.dispatchEvent(new MouseEvent("mouseup", at));
    el.click();
  }

  // A click at a point of the page, on the element there.
  function clickAt(el, x, y) {
    const at = { bubbles: true, cancelable: true, clientX: x, clientY: y, view: window };
    el.dispatchEvent(new PointerEvent("pointerdown", at));
    el.dispatchEvent(new MouseEvent("mousedown", at));
    if (typeof el.focus === "function") el.focus({ preventScroll: true });
    el.dispatchEvent(new PointerEvent("pointerup", at));
    el.dispatchEvent(new MouseEvent("mouseup", at));
    el.dispatchEvent(new MouseEvent("click", at));
  }

  // Steps that may leave the page answer first, then act: the page may be
  // gone before an answer could be sent.
  function act(msg) {
    const { action, args = {} } = msg;
    if (action === "look") return { now: look() };
    if (action === "scroll") {
      if (args.ref != null) {
        const el = element(args.ref);
        if (!el) return { now: GONE };
        el.scrollIntoView({ block: "center" });
      } else {
        window.scrollBy({ top: (args.direction === "up" ? -1 : 1) * innerHeight * 0.8 });
      }
      return { now: { ok: true, done: true } };
    }
    if (action === "key") {
      const el = document.activeElement || document.body;
      if (args.key === "Enter") return { now: { ok: true, done: true }, after: () => pressEnter(el) };
      if (args.key === "Backspace" && (el.tagName === "INPUT" || el.tagName === "TEXTAREA")) {
        if (OE.sensitiveField(attrs(el))) return { now: { ok: false, error: "That's a sensitive field. Ask them to do it." } };
        setValue(el, String(el.value || "").slice(0, -1));
        return { now: { ok: true, done: true } };
      }
      if (args.key === "Tab") {
        const all = Array.from(document.querySelectorAll(INTERACTIVE)).filter(visible);
        const i = all.indexOf(el);
        all[(i + 1) % Math.max(1, all.length)]?.focus();
        return { now: { ok: true, done: true } };
      }
      const opts = { key: args.key, code: args.key, bubbles: true, cancelable: true };
      el.dispatchEvent(new KeyboardEvent("keydown", opts));
      el.dispatchEvent(new KeyboardEvent("keyup", opts));
      return { now: { ok: true, done: true } };
    }
    if (action === "click_at") {
      const at = elementAt(args.cx, args.cy);
      if (!at || at === document.documentElement || at === document.body) {
        return { now: { ok: false, error: "Nothing to click there. Look again." } };
      }
      if (at.tagName === "IFRAME" || at.tagName === "FRAME") {
        return { now: { ok: false, error: "That's inside a frame on the page, which can't be clicked from here. Ask them to." } };
      }
      return {
        now: { ok: true, done: true, clicked: OE.squeeze(labelOf(at), 80) },
        after: () => clickAt(at, args.cx, args.cy),
      };
    }
    const el = element(args.ref);
    if (!el) return { now: GONE };
    if (action === "point") {
      point(el);
      return { now: { ok: true, done: true, pointed: OE.squeeze(labelOf(el), 80) } };
    }
    if (action === "click") {
      return { now: { ok: true, done: true, clicked: OE.squeeze(labelOf(el), 80) }, after: () => click(el) };
    }
    if (action === "type") {
      if (OE.sensitiveField(attrs(el))) {
        return { now: { ok: false, error: "That's a password, card or similar field. Ask them to fill it in themselves." } };
      }
      const text = String(args.text || "");
      el.scrollIntoView({ block: "center" });
      el.focus();
      if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") {
        setValue(el, text);
      } else if (el.isContentEditable) {
        document.execCommand("selectAll", false);
        document.execCommand("insertText", false, text);
      } else {
        return { now: { ok: false, error: "That isn't a field you can type in. Look again for the field." } };
      }
      if (args.submit) return { now: { ok: true, done: true }, after: () => pressEnter(el) };
      return { now: { ok: true, done: true } };
    }
    if (action === "select") {
      if (el.tagName !== "SELECT") {
        return { now: { ok: false, error: "That isn't a plain dropdown. Click it, look, then click the option." } };
      }
      const i = OE.pickOption(Array.from(el.options).map((o) => o.text), args.option);
      if (i < 0) return { now: { ok: false, error: "No option with that text. Look at its options again." } };
      el.selectedIndex = i;
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return { now: { ok: true, done: true, picked: el.options[i].text.trim() } };
    }
    return { now: { ok: false, error: "Unknown step." } };
  }

  // ---- Pointing something out ----

  let ring = null;
  let ringTimer = 0;

  // A ring around `el` for a few seconds, so the person sees what's meant.
  function point(el) {
    el.scrollIntoView({ block: "center", behavior: "smooth" });
    ring?.remove();
    const host = document.createElement("div");
    host.style.cssText = "all:initial;position:fixed;z-index:2147483646;pointer-events:none;";
    const root = host.attachShadow({ mode: "closed" });
    root.innerHTML = `
      <style>
        .r{position:fixed;border:3px solid #14b8a6;border-radius:10px;box-shadow:0 0 0 6px rgba(20,184,166,.25);
           transition:all .2s ease;animation:p 1s ease-in-out infinite;}
        @keyframes p{50%{box-shadow:0 0 0 12px rgba(20,184,166,.12);}}
      </style><div class="r"></div>`;
    const box = root.querySelector(".r");
    const place = () => {
      const r = el.getBoundingClientRect();
      box.style.left = `${r.left - 6}px`;
      box.style.top = `${r.top - 6}px`;
      box.style.width = `${r.width + 12}px`;
      box.style.height = `${r.height + 12}px`;
    };
    place();
    document.documentElement.appendChild(host);
    ring = host;
    const follow = setInterval(place, 100);
    clearTimeout(ringTimer);
    ringTimer = setTimeout(() => {
      clearInterval(follow);
      host.remove();
      if (ring === host) ring = null;
    }, POINT_MS);
  }

  const POINT_MS = 6000;

  // ---- Opening an Open Executive page (its own tab only) ----

  // The page opens it in place (so whatever runs in this tab never
  // reloads) and says so; no answer means this page can't.
  function goTo(path) {
    return new Promise((done) => {
      const timer = setTimeout(() => finish({ ok: false, error: "Open Executive couldn't open that page here. Give them the link instead." }), 2500);
      function finish(out) {
        clearTimeout(timer);
        window.removeEventListener("message", heard);
        done(out);
      }
      function heard(e) {
        if (e.source !== window || e.origin !== location.origin) return;
        const d = e.data;
        if (!d || d.__oe !== "went" || d.path !== path) return;
        finish(d.ok ? { ok: true, done: true, path, title: OE.squeeze(document.title, 100) } : { ok: false, error: "There's no page there. Look up the right one." });
      }
      window.addEventListener("message", heard);
      window.postMessage({ __oe: "go", path }, location.origin);
    });
  }

  // ---- The Stop bar ----

  let bar = null;
  let barTimer = 0;
  // The bar was showing when a screenshot hid it.
  let snapped = false;

  function showBar() {
    if (!bar) {
      const host = document.createElement("div");
      host.style.cssText = "all:initial;position:fixed;top:0;left:0;right:0;z-index:2147483647;";
      const root = host.attachShadow({ mode: "closed" });
      root.innerHTML = `
        <style>
          .b{display:flex;align-items:center;justify-content:center;gap:12px;padding:6px 12px;
             background:#4f46e5;color:#fff;font:600 13px/1.4 system-ui,sans-serif;}
          button{all:unset;cursor:pointer;background:#dc2626;color:#fff;border-radius:999px;padding:4px 14px;font:600 13px system-ui,sans-serif;}
        </style>
        <div class="b" role="status"><span>Open Executive is working in this tab</span><button type="button">Stop</button></div>`;
      root.querySelector("button").addEventListener("click", () => {
        void chrome.runtime.sendMessage({ type: "stop" }).catch(() => undefined);
        hideBar();
      });
      bar = host;
    }
    if (!bar.isConnected) document.documentElement.appendChild(bar);
    clearTimeout(barTimer);
    barTimer = setTimeout(hideBar, 10000);
  }

  function hideBar() {
    clearTimeout(barTimer);
    bar?.remove();
  }

  /** Where the password, card and code fields (and payment providers'
   * frames) are on screen, in CSS pixels: never shown in a screenshot. */
  function masks() {
    const out = [];
    const add = (el) => {
      const r = el.getBoundingClientRect();
      if (r.width < 1 || r.height < 1 || r.bottom < 0 || r.right < 0) return;
      if (r.top > innerHeight || r.left > innerWidth) return;
      out.push({ x: r.left - 2, y: r.top - 2, w: r.width + 4, h: r.height + 4 });
    };
    for (const el of document.querySelectorAll("input, textarea")) {
      if (OE.sensitiveField(attrs(el))) add(el);
    }
    for (const el of document.querySelectorAll("iframe")) {
      if (OE.paymentFrame(el.src, el.title || el.name)) add(el);
    }
    return out.slice(0, 200);
  }

  // ---- Waiting on you: a step handed over to the person ----

  /** Whether a sign-in form is on the page now (a password, a code, an
   * account name a sign-in asks for first). */
  function hasSignIn() {
    for (const el of document.querySelectorAll("input:not([type=hidden])")) {
      if (visible(el) && OE.signInField(attrs(el))) return true;
    }
    return false;
  }

  let handBar = null;

  function hideHandBar() {
    handBar?.remove();
    handBar = null;
  }

  /** The bar that stays until they press Done or Stop: what the Executive
   * is waiting on them for. `need` is shown as text, never as markup. */
  function showHandBar(id, need) {
    hideHandBar();
    const host = document.createElement("div");
    host.style.cssText = "all:initial;position:fixed;top:0;left:0;right:0;z-index:2147483647;";
    const root = host.attachShadow({ mode: "closed" });
    const style = document.createElement("style");
    style.textContent = `
      .b{display:flex;flex-wrap:wrap;align-items:center;justify-content:center;gap:10px;padding:8px 12px;
         background:#b45309;color:#fff;font:600 14px/1.4 system-ui,sans-serif;}
      .t{max-width:70vw;}
      .n{font-weight:400;opacity:.9;}
      button{all:unset;cursor:pointer;border-radius:999px;padding:5px 16px;font:600 13px system-ui,sans-serif;}
      .done{background:#fff;color:#7c2d12;}
      .stop{background:transparent;color:#fff;border:1px solid rgba(255,255,255,.7);}`;
    const box = document.createElement("div");
    box.className = "b";
    box.setAttribute("role", "status");
    const text = document.createElement("span");
    text.className = "t";
    text.textContent = `Waiting on you: ${OE.squeeze(need, 140)}`;
    const note = document.createElement("span");
    note.className = "n";
    note.textContent = "Press Done when you've finished.";
    const done = document.createElement("button");
    done.type = "button";
    done.className = "done";
    done.textContent = "Done";
    const stop = document.createElement("button");
    stop.type = "button";
    stop.className = "stop";
    stop.textContent = "Stop";
    const send = (outcome) => {
      done.disabled = stop.disabled = true;
      note.textContent = outcome === "done" ? "Carrying on…" : "Stopping…";
      void chrome.runtime
        .sendMessage({ type: "handover", id, outcome })
        .catch(() => ({ ok: false }))
        .then((out) => {
          if (out && out.ok && out.on_card) {
            // The Executive stopped waiting: it carries on from the card,
            // where they see what it will do.
            done.remove();
            stop.remove();
            note.textContent = "Thanks. Press Done on the card in Open Executive to carry on.";
            setTimeout(hideHandBar, 8000);
            return;
          }
          if (out && out.ok) {
            hideHandBar();
            return;
          }
          done.disabled = stop.disabled = false;
          note.textContent =
            (out && out.error) || "Open Executive didn't answer. Keep an Open Executive tab open and try again.";
        });
    };
    done.addEventListener("click", () => send("done"));
    stop.addEventListener("click", () => send("stop"));
    box.append(text, note, done, stop);
    root.append(style, box);
    handBar = host;
    document.documentElement.appendChild(host);
  }

  /** On the Open Executive site: tell it they pressed Done or Stop. */
  async function postHandover(id, outcome) {
    try {
      const res = await fetch(`${API}/hand-over/${encodeURIComponent(id)}`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ outcome }),
      });
      if (res.status === 404) return { ok: true, gone: true };
      if (!res.ok) return { ok: false, error: "Open Executive couldn't take that. Try again." };
      const body = await res.json().catch(() => ({}));
      return { ok: true, carried_on: body.carried_on === true, kept: body.kept === true, on_card: body.on_card === true };
    } catch {
      return { ok: false, error: "Open Executive didn't answer. Try again." };
    }
  }

  chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
    if (!msg || typeof msg !== "object") return false;
    if (msg.type === "hello") {
      void hello().then(reply);
      return true;
    }
    if (msg.type === "stopped") {
      hideBar();
      return false;
    }
    if (msg.type === "signin") {
      // Only the background asks: whether a sign-in form is on the page.
      if (_sender.id !== chrome.runtime.id || _sender.tab) return false;
      reply({ signIn: hasSignIn() });
      return false;
    }
    if (msg.type === "handover_show" || msg.type === "handover_hide") {
      if (_sender.id !== chrome.runtime.id || _sender.tab) return false;
      if (msg.type === "handover_show") showHandBar(String(msg.id || ""), String(msg.need || ""));
      else hideHandBar();
      reply({ ok: true });
      return false;
    }
    if (msg.type === "handover_post") {
      // Only the background asks, and only on a connected Open Executive site.
      if (_sender.id !== chrome.runtime.id || _sender.tab) return false;
      void (async () => {
        if (!OE.onConnected(location.href, await connectedOrigins())) return { ok: false };
        const outcome = msg.outcome === "stop" ? "stop" : msg.outcome === "auto" ? "auto" : "done";
        return postHandover(String(msg.id || ""), outcome);
      })().then(reply);
      return true;
    }
    if (msg.type === "go") {
      if (_sender.id !== chrome.runtime.id || _sender.tab) return false;
      void (async () => {
        if (!OE.onConnected(location.href, await connectedOrigins())) {
          return { ok: false, error: "That isn't the Open Executive tab." };
        }
        return goTo(String(msg.path || ""));
      })().then(reply);
      return true;
    }
    if (msg.type === "snap") {
      // Only the background asks: the Stop bar out of its screenshot.
      if (_sender.id !== chrome.runtime.id || _sender.tab) return false;
      if (msg.hide) {
        snapped = Boolean(bar && bar.isConnected);
        bar?.remove();
      } else if (snapped && bar) {
        snapped = false;
        document.documentElement.appendChild(bar);
      }
      // A frame for the page to draw without it before the screenshot, and
      // where its password, card and code fields are, for the background to
      // black out in the picture.
      requestAnimationFrame(() => setTimeout(() => reply({ ok: true, view: viewNow(), masks: msg.hide ? masks() : [] }), 30));
      return true;
    }
    if (msg.type === "view") {
      // Only the background asks: where the page is now, to tell whether
      // the last screenshot still matches it.
      if (_sender.id !== chrome.runtime.id || _sender.tab) return false;
      reply({ ok: true, view: viewNow() });
      return false;
    }
    if (msg.type === "name") {
      // Only the background asks: what a held step would act on, by name.
      if (_sender.id !== chrome.runtime.id || _sender.tab) return false;
      const target = targetOf(msg.action, msg.args || {});
      reply({ element: target && target !== document.body ? nameOf(target) : null });
      return false;
    }
    if (msg.type !== "act") return false;
    // Only the background hands out steps.
    if (_sender.id !== chrome.runtime.id || _sender.tab) return false;
    void (async () => {
      // Checked again here: the tab may have moved on since the background looked.
      const here = location.href;
      const shows = msg.action === "look" || msg.action === "scroll" || msg.action === "point";
      if (!shows && OE.onConnected(here, await connectedOrigins())) {
        return { now: { ok: false, error: "That's Open Executive itself: point to what to press, and they press it." } };
      }
      if (OE.blockedUrl(here)) return { now: { ok: false, error: "That page can't be used by the extension." } };
      if (!shows) {
        const stop = OE.sendingStop(here, msg.args);
        if (stop) return { now: stop };
        // A step approved on a card acts only on the element it was approved for.
        const expected = (msg.args || {}).element;
        if (typeof expected === "string" && expected) {
          const target = targetOf(msg.action, msg.args || {});
          if (!target || nameOf(target) !== expected) {
            return { now: { ok: false, error: "The page changed since this was approved. Look again." } };
          }
        }
      }
      try {
        showBar();
        return act(msg);
      } catch (e) {
        return { now: { ok: false, error: `That didn't work on this page (${String(e && e.message ? e.message : e).slice(0, 120)}).` } };
      }
    })().then((out) => {
      reply(out.now);
      if (out.after) {
        setTimeout(() => {
          try {
            out.after();
          } catch {
            // Already answered; the next look shows what happened.
          }
        }, 0);
      }
    });
    return true;
  });
})();
