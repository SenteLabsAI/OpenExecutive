OneDrive folder sync setup

The OneDrive sync (`knowledge/onedrive_sync.py`) copies the files in chosen
OneDrive or SharePoint folders into the Executive's knowledge base. It works
like the [Google Drive sync](drive_sync_setup.md): it checks for changes on
an interval, re-indexes the files that changed, and drops files that left
the folders. Retrieved passages are labelled as synced OneDrive content,
which many people can edit and nobody has reviewed, so they rank below your
curated company documents. Each passage shows the file's drive and item id
and its sync time, so the Executive can open the live file with its OneDrive
tools when the latest version matters.

The sync reads OneDrive as the **Executive's own Microsoft 365 account**, the
one the Microsoft 365 tools are signed in as (see "Microsoft 365 credentials"
in [deployment.md](deployment.md)). Microsoft has no equivalent of a Google
service account that sees only what is shared with it: app-only access
covers every file in the tenant. So the folders you list are the scope, and
the sync only ever reads. It is separate from the OneDrive tools the
Executive uses in chat.

---

## Step 1 — Allow file access on the sign-in

The Executive's Microsoft sign-in needs the delegated Graph permissions
**Files.Read.All** (what the sync reads with: files shared with the account
as well as its own) and **Files.ReadWrite** (what the OneDrive tools in chat
write to the account's own OneDrive with). The launcher asks for both at
sign-in once `MS365_MCP_ONEDRIVE=true` is set. It is off by default because
the Microsoft 365 server asks for every permission of its tools on each
token refresh: turning it on for a sign-in that never granted file access
fails every Microsoft 365 tool, mail and calendar included, until you sign
in again (step 2).

1. In the Entra admin center, open the app registration
   (`MS365_MCP_CLIENT_ID`) → **API permissions → Add a permission →
   Microsoft Graph → Delegated permissions → Files.Read.All** and
   **Files.ReadWrite**, then grant consent if your tenant requires an
   admin to. Don't add **Files.ReadWrite.All**; if an earlier setup granted
   it, remove it there and revoke its consent, since a sign-in keeps every
   permission it was ever granted.
2. Set `MS365_MCP_ONEDRIVE=true` in `.env`, restart, and sign in again so
   the saved grant includes the file permissions:

   ```bash
   docker compose exec api /usr/local/bin/ms365-mcp-launch.sh --login
   ```

The same sign-in also gives the Executive its OneDrive tools in chat, the
same surface it has on Google Drive: search, list a folder, read a file as
text, upload and edit, create folders, move, rename, copy, and share. Sharing
is checked like Drive sharing: every invitee must be on the People roster,
and a link must be limited to specific people (no anyone-with-the-link or
whole-organization links). Files shared with the Executive can be read, but
new and changed files are saved only to its own OneDrive (share them from
there). There is no delete tool.

## Step 2 — Share the folders and get their ids

For each folder to sync, share it with the Executive's Microsoft account
(view access is enough), or use a folder in that account's own OneDrive.
Subfolders are included, up to four levels deep. Shortcuts inside a folder
("Add shortcut to My files") are not followed: only the folders you list
and their own subfolders are read, even though the account can open more.

A folder is named by its drive id and item id. To get them, copy the
folder's sharing link and run:

```bash
cd packages/core && uv run openexecutive onedrive-folder 'https://1drv.ms/f/s!...'
```

It prints the folder's name and `<drive id>/<item id>`.

## Step 3 — Configure and run

In `.env`:

```
ONEDRIVE_SYNC_ENABLED=true
ONEDRIVE_SYNC_FOLDERS=<drive id>/<item id>,<drive id>/<item id>
# Optional:
# ONEDRIVE_SYNC_INTERVAL_MINUTES=60
# ONEDRIVE_MAX_FILES_PER_SCAN=40
# MS365_MCP_LAUNCHER=/usr/local/bin/ms365-mcp-launch.sh
```

Restart the backend. The first sync runs about a minute after startup and
then every `ONEDRIVE_SYNC_INTERVAL_MINUTES`. To run one immediately:

```bash
cd packages/core && uv run openexecutive sync-onedrive
```

The Knowledge page lists the synced files under OneDrive, with **Sync now**.
If the sign-in is missing or refused, that is shown there as the last error.

## What is synced

- **PDF, Word (`.docx`) and Excel (`.xlsx`, `.xlsm`)** files have their text
  extracted. A scanned PDF with no text layer yields nothing.
- **PowerPoint, older Word and Excel files (`.doc`, `.xls`), OpenDocument and
  RTF** are fetched as Microsoft's PDF rendering of the file, then read.
- **Plain text, Markdown and CSV** are read as they are.
- Anything else (images, video, OneNote notebooks, other binaries) is
  skipped, and so is any file over 20 MB.

## How the token is fetched

The sync never stores a credential of its own. Each run asks the Microsoft
365 launcher for a short-lived token (`ms365-mcp-launch.sh --access-token`),
which reads the same encrypted token cache the Microsoft 365 tools use and
prints only an access token. Although the sync asks for `Files.Read.All`,
Microsoft issues one Graph token per sign-in, so the token can do whatever the
sign-in was granted, mail and calendar included. It stays in memory for that
run and is sent only to Microsoft Graph, and the helper is given only the
launcher's environment, none of the API's other keys. File downloads follow
Graph's redirect to a pre-authenticated link without the token.

## Removing content

- A file removed from a synced folder, or a folder unshared from the
  Executive's account, is purged on the next sync.
- `openexecutive purge-onedrive --key <drive id>:<item id>` removes one file now.
- `purge-onedrive --stale` runs a purge-only pass.
- `purge-onedrive --all` removes everything synced.
