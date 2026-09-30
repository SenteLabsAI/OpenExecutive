# Google Drive folder sync setup

The Drive sync (`knowledge/drive_sync.py`) copies the files in chosen Google
Drive folders into the Executive's knowledge base. It checks for changes on
an interval, re-indexes the files that changed, and drops files that left
the folders. Retrieved passages are labelled as synced Drive content, which
many people can edit and nobody has reviewed, so they rank below your
curated company documents. Each passage shows its file id and sync time, so
the Executive can open the live file when the latest version matters.

The sync reads Drive as a **service account** with the read-only
`drive.readonly` scope. It can see only what is shared with that account,
and it cannot change anything. It is separate from the Google Workspace
tools the Executive uses in chat, which keep working as before.

---

## Step 1 — Enable the Drive API

In [console.cloud.google.com](https://console.cloud.google.com), pick (or
create) a project, then **APIs & Services → Enable APIs & Services → Google
Drive API → Enable**.

## Step 2 — Create the service account and its key

1. **IAM & Admin → Service Accounts → Create Service Account**. Name it,
   e.g. `open-executive-drive-sync`. It needs no project IAM roles.
2. Open it → **Keys → Add Key → Create new key → JSON**, and download it.
3. Save the key somewhere the backend can read and git cannot, e.g.
   `packages/core/company/drive_sync_service_account.json` (`company/` is
   gitignored) or a mounted secret.
4. Note the service account's email
   (`…@<project>.iam.gserviceaccount.com`).

## Step 3 — Share the folders

For each Drive folder to sync, **Share** it with:

- the service account's email, as **Viewer**, which lets the sync read it;
- the Executive's own Google account (`EXEC_EMAIL_ADDRESS`), as **Viewer**
  or more, so the Executive can open a synced file live in chat.

Subfolders are included, up to four levels deep. For a folder in a shared
drive, add the service account as a member of that shared drive (or share
the folder itself).

The folder id is the last part of the folder's URL:
`https://drive.google.com/drive/folders/<folder id>`.

## Step 4 — Configure and run

In `.env`:

```
DRIVE_SYNC_ENABLED=true
DRIVE_SYNC_SERVICE_ACCOUNT_FILE=/absolute/path/to/drive_sync_service_account.json
DRIVE_SYNC_FOLDER_IDS=<folder id>,<another folder id>
# Optional:
# DRIVE_SYNC_INTERVAL_MINUTES=60
# DRIVE_MAX_FILES_PER_SCAN=40
```

Restart the backend. The first sync runs about a minute after startup and
then every `DRIVE_SYNC_INTERVAL_MINUTES`. To run one immediately:

```bash
cd packages/core && uv run openexecutive sync-drive
```

## What is synced

- **Google Docs and Slides** are exported as text.
- **Google Sheets** are exported as `.xlsx` and read sheet by sheet.
- **PDF, Word (`.docx`) and Excel (`.xlsx`)** files have their text
  extracted. A scanned PDF with no text layer yields nothing.
- **Plain text, Markdown and CSV** are read as they are.
- Anything else (images, video, other binaries) is skipped, and so is any
  file over 20 MB.

## Removing content

- A file removed from a synced folder, or a folder unshared from the
  service account, is purged on the next sync.
- `openexecutive purge-drive --file-id <id>` removes one file now.
- `purge-drive --stale` runs a purge-only pass.
- `purge-drive --all` removes everything synced.
