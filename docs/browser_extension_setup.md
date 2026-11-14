# Browser extension setup

With the Open Executive browser extension in a person's Chrome, the Executive
can use their own browser: look at the page they're on, click, type, pick from
lists, scroll and open pages, until a task is done. For example: "sort this
Amazon search by best sellers and list the top five", or "fill in this vendor
form from the Harbor Lane contract".

It's on by default. Each person connects their own browser, and the Executive
only ever uses the browser of the person it's working for.

## What it does and doesn't do

- It works in the person's own Chrome, signed in as them, while one of their
  Open Executive tabs is open (or with **Stay connected**, while Chrome is
  open, in its own window).
- It never types into password, card number or sign-in code fields, and
  screenshots black those fields out.
- When a page needs the person (a sign-in, a code, a "prove you're human"
  check, a choice that's theirs), the Executive hands it over. Their browser
  shows **Waiting on you** with Done and Stop, and they get a card under
  Needs you. The Executive carries on when they press Done.
- Clicking and typing follow the person's Approvals. On an email or chat site,
  where a click can send a message, it goes only on Hands-off, or once they
  approve its card.
- A **Stop** bar sits at the top of the page while it works.
- Saved workflows can use it too, in the browser of the workflow's creator,
  in the extension's own window. Only the creator can save a workflow with a
  browser step, and it uses their browser only on runs they start or that run
  on its schedule.

## Install the extension

Pick one.

**From the Chrome Web Store.** Settings → Browser in your Open Executive
links to the published listing. The published extension connects to any Open
Executive site you choose, including yours.

**Load it unpacked** (no store needed):

1. Open `chrome://extensions` and turn on **Developer mode**.
2. Click **Load unpacked** and pick the `packages/extension` folder from this
   repo.
3. Pin the extension so its button stays in the toolbar.

## Connect it

1. Open your Open Executive in a tab and sign in.
2. Click the extension's button, then **Connect**.

Your site must be on `https://`, or on `localhost` while you develop. A plain
`http://` address on your network won't connect.

Settings → Browser shows whether it's connected and has a few things to try.

## Settings

| Variable | Default | What it does |
|---|---|---|
| `BROWSER_EXTENSION_URL` | The published Chrome Web Store listing | Where the **Add to Chrome** button goes. Set your own listing's `https://` address, or leave it empty to show only the load unpacked steps |
| `OE_BROWSER_OFF` | unset | Set to `1` to turn browser use off for everyone. The Executive gets no browser tools, and Settings → Browser says it's off |

## Make it your own

The extension builds with your name, icons and version, for your own store
listing or for your team to load unpacked:

```bash
cd packages/extension
node build.mjs --name "Acme Executive" --version 1.0.0 --icons path/to/icons --zip
```

`--icons` is a folder with `icon16.png`, `icon32.png`, `icon48.png` and
`icon128.png`. The build goes to `dist/` (and `dist.zip` with `--zip`, ready to
upload). Then set `BROWSER_EXTENSION_URL` to your listing.

**Site lists.** `packages/extension/site-rules.js` holds the email and chat
sites where a click waits for an OK, and the payment providers whose card
frames screenshots black out. Change them there and rebuild.

**Versions.** The site tells the extension which version of the steps it
speaks (`protocol` from `/browser/hello`). When they differ, the extension
asks the person to update instead of connecting.

## For developers

- Server side: `packages/core/openexecutive/browser/` (the relay, tools,
  hand-over and starters) and `api/routes/browser.py`.
- The extension polls `/api/backend/browser/steps` on the connected site, as
  the signed-in person, and posts each step's result back.
- An install can offer the tools on more kinds of turn, or mark turns where
  the person is away from their computer, with `SURFACES` and `AWAY` in
  `browser/tools.py`.
- Tests: `packages/core/tests/unit/test_browser_*.py` and
  `packages/ui/scripts/browserExtension.test.mjs`.
