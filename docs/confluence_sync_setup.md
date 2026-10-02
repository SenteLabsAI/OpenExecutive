# Confluence sync setup

The Confluence sync (`knowledge/confluence_sync.py`) copies the pages in
chosen Confluence spaces into the Executive's knowledge base. It checks for
changes on an interval, re-indexes the pages whose version changed, and drops
pages that were deleted, moved out of the spaces, or restricted. Retrieved
passages are labelled as synced Confluence content, which many people can
edit and nobody has reviewed, so they rank below your curated company
documents. Each passage shows its page id, space and sync time.

It works with **Confluence Cloud** and **Confluence Server / Data Center**,
and only reads: it never changes anything in Confluence.

The sync is separate from a Confluence MCP server such as mcp-atlassian. That
gives the Executive live tools in chat (open a page, edit one); the sync
makes the spaces searchable in the background without a tool call. You can
run either or both. The connection settings below use the same names as
mcp-atlassian, so one set of values serves both.

---

## Who the sync reads as

A Confluence token always belongs to a user, and the sync can see what that
user can see. Everyone who can talk to the Executive can then find what it
synced. Two controls keep that to content meant for everyone:

1. **Spaces.** Only the spaces in `CONFLUENCE_SYNC_SPACE_KEYS` are read,
   whatever else the token can see.
2. **Restricted pages are skipped.** A page with a view restriction, its own
   or one inherited from a parent page, is left out, and purged if it was
   synced before. If Confluence does not report a page's restrictions, the
   sync treats the page as restricted. Set
   `CONFLUENCE_SYNC_SKIP_RESTRICTED=false` only if everyone who uses the
   Executive may read every page in those spaces.

With both in place, the token can belong to any existing member who can read
the spaces, so no extra Confluence licence is needed. A dedicated read-only
account is still the tightest option, and it keeps the sync working if the
token owner leaves.

## Step 1 — Create a token

**Server / Data Center (7.9 or later):** signed in as the user the sync will
read as, open **Profile → Settings → Personal Access Tokens → Create token**.
Give it an expiry you will remember to renew.

**Cloud:** signed in as that user, create an API token at
[id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens).
The sync signs in with that user's email and the token.

## Step 2 — Find the space keys

The space key is in a space's URL: `/display/<KEY>/...` or
`/spaces/<KEY>/...`. Personal spaces start with `~`.

## Step 3 — Configure and run

In `.env`:

```
CONFLUENCE_SYNC_ENABLED=true
# Server/Data Center: the site's base URL, with any context path
CONFLUENCE_URL=https://confluence.example.com
CONFLUENCE_PERSONAL_TOKEN=<token>
# Cloud instead: the URL ends in /wiki, and the email + API token sign in
# CONFLUENCE_URL=https://<site>.atlassian.net/wiki
# CONFLUENCE_USERNAME=<email>
# CONFLUENCE_API_TOKEN=<token>
CONFLUENCE_SYNC_SPACE_KEYS=ENG,OPS
# Optional:
# CONFLUENCE_SSL_VERIFY=true
# CONFLUENCE_SYNC_SKIP_RESTRICTED=true
# CONFLUENCE_SYNC_ALLOW_HTTP=false
# CONFLUENCE_SYNC_INTERVAL_MINUTES=60
# CONFLUENCE_MAX_PAGES_PER_SCAN=40
```

If both a personal access token and a username + API token are set, the
personal access token is used.

**Private certificates.** For a server whose certificate comes from your own
certificate authority, set `CONFLUENCE_SSL_VERIFY` to the path of that CA's
bundle (PEM). `false` turns verification off entirely; avoid it outside a
test setup.

**Plain HTTP.** The URL must be `https://` unless you set
`CONFLUENCE_SYNC_ALLOW_HTTP=true`. Over plain HTTP the token crosses the
network unencrypted, so use it only on a network you trust.

Restart the backend. The first sync runs about a minute after startup and
then every `CONFLUENCE_SYNC_INTERVAL_MINUTES`. To run one immediately:

```bash
cd packages/core && uv run openexecutive sync-confluence
```

The result lists pages seen, updated, skipped (unchanged), restricted,
failed, purged and capped. Each sync fetches at most
`CONFLUENCE_MAX_PAGES_PER_SCAN` changed pages, newest first; the rest follow
on the next runs.

## What is synced

- Pages (not blog posts, comments or attachments) in the listed spaces, up
  to 5,000 pages across all of them.
- Page text, headings, lists, tables, links, task lists and code blocks.
- The body of info, note, warning, tip, panel, expand and similar macros.
- Not: dynamic macros (table of contents, child pages, Jira issues, included
  pages), images, or attachments. Their content is not in the page itself.

## Removing content

- A page deleted, moved out of the listed spaces, or newly restricted is
  purged on the next sync.
- Removing a space from `CONFLUENCE_SYNC_SPACE_KEYS` purges its pages on the
  next sync.
- `openexecutive purge-confluence --page-id <id>` removes one page now.
- `purge-confluence --stale` runs a purge-only pass.
- `purge-confluence --all` removes everything synced.

If a space cannot be listed (a wrong key, an expired token, the server
down), nothing is purged that run, so a passing outage never empties the
knowledge base. The reason is logged.
