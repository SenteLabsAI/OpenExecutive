# Google Chat Integration Setup

The Open Executive Google Chat bot receives messages via a webhook (`POST /webhook/google-chat`) and replies using a service account. The integration is already implemented — you just need to wire up a GCP project.

---

## Step 1 — Enable the Google Chat API

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and select or create a project.
2. Navigate to **APIs & Services → Enable APIs & Services**.
3. Search for **Google Chat API** and click **Enable**.
4. From the project dashboard (or **IAM & Admin → Settings**), note your **Project Number** — this is a 12-digit number like `123456789012`. Do not confuse it with the Project ID (the hyphenated string).

---

## Step 2 — Create a Service Account & Choose an Auth Method

The bot needs a service account to post replies. How you authenticate depends on your org's policies.

### 2a. Create the service account (always required)

1. Go to **IAM & Admin → Service Accounts → Create Service Account**.
2. Name it (e.g., `open-executive-chat`) and click through — no project-level IAM roles are needed at this step; the Chat API grants permissions via the app configuration.

---

### 2b. Choose your auth method

#### Option A — Service account key file *(simple, blocked by some orgs)*

If your org enforces `iam.disableServiceAccountKeyCreation`, skip to Option B.

1. Open the service account → **Keys** tab → **Add Key → Create new key → JSON**.
2. Download the JSON. Save to `company/google_chat_service_account.json` (gitignored).
3. Set in `.env`:
   ```
   GOOGLE_CHAT_SERVICE_ACCOUNT_FILE=/absolute/path/to/google_chat_service_account.json
   ```

---

#### Option B — ADC with impersonation *(for orgs that block key creation)*

No key file is downloaded. Your user credential impersonates the service account at runtime.

**One-time GCP setup:**

1. In IAM, grant your Google account the **Service Account Token Creator** role *on the service account* (not at project level):
   - Go to **IAM & Admin → Service Accounts** → click the `open-executive-chat` SA
   - **Permissions** tab → **Grant Access**
   - Add your email, role: `Service Account Token Creator`
   - Click **Save**

2. Authenticate locally:
   ```bash
   gcloud auth application-default login
   ```
   This opens a browser and caches a credential at `~/.config/gcloud/application_default_credentials.json`.

3. Set in `.env`:
   ```
   GOOGLE_CHAT_SERVICE_ACCOUNT_EMAIL=open-executive-chat@YOUR_PROJECT_ID.iam.gserviceaccount.com
   ```
   (Leave `GOOGLE_CHAT_SERVICE_ACCOUNT_FILE` unset.)

---

#### Option C — ADC direct *(for GCP-hosted deployments only)*

If the server runs on **Cloud Run, GCE, or GKE**, attach the `open-executive-chat` service account to the compute resource. No key file and no personal ADC login are involved.

1. Grant the service account the **Service Account Token Creator** role *on itself* (**IAM & Admin → Service Accounts** → `open-executive-chat` → **Permissions** → **Grant Access**, principal = the SA's own email). With `GOOGLE_CHAT_SERVICE_ACCOUNT_EMAIL` set, the code impersonates that SA using the metadata server's credential, and Google requires this grant even when an SA impersonates itself. On GCE, the VM's access scopes must also allow `cloud-platform`.

2. Set `GOOGLE_CHAT_SERVICE_ACCOUNT_EMAIL` to that SA's email address. This is **required**: the webhook returns `503 Google Chat integration not configured` unless `GOOGLE_CHAT_SERVICE_ACCOUNT_FILE` or `GOOGLE_CHAT_SERVICE_ACCOUNT_EMAIL` is set.

```
GOOGLE_CHAT_SERVICE_ACCOUNT_EMAIL=open-executive-chat@YOUR_PROJECT_ID.iam.gserviceaccount.com
```

---

## Step 3 — Configure the Google Chat App

1. In the Google Cloud Console, go to **APIs & Services → Google Chat API → Configuration**.
2. Fill in **App name**, **Avatar URL**, and **Description**.

   A ready-to-use avatar (brand-matched, 256×256 SVG) is included in this repo. Paste this URL into the **Avatar URL** field:
   ```
   https://raw.githubusercontent.com/SenteLabsAI/OpenExecutive/main/docs/assets/chat-avatar.svg
   ```
   Google Chat accepts SVG URLs served from GitHub raw content directly.
3. Under **Functionality**, enable:
   - Receive 1:1 messages
   - Join spaces and group conversations
4. Under **Connection settings**, select **App URL** and enter your webhook URL:
   ```
   https://your-domain.com/webhook/google-chat
   ```
   For **local development**, expose your server with [ngrok](https://ngrok.com):
   ```bash
   ngrok http 8000
   # Use the HTTPS forwarding URL, e.g. https://abc123.ngrok-free.app/webhook/google-chat
   ```
5. Under **Permissions**, choose who can install the app (your Google Workspace domain, specific users, etc.).
6. Click **Save**.

---

## Step 4 — Set Environment Variables

`GOOGLE_CHAT_PROJECT_NUMBER` is always required, plus one auth var — which one depends on the option you chose in Step 2b. With neither auth var set, the webhook returns `503`.

```bash
# Always required
GOOGLE_CHAT_PROJECT_NUMBER=123456789012   # numeric project number from Step 1

# Option A (key file)
GOOGLE_CHAT_SERVICE_ACCOUNT_FILE=/absolute/path/to/google_chat_service_account.json

# Option B (impersonation — org policy blocks key creation)
GOOGLE_CHAT_SERVICE_ACCOUNT_EMAIL=open-executive-chat@YOUR_PROJECT_ID.iam.gserviceaccount.com

# Option C (GCP-hosted) — same var as Option B, set to the SA attached to the compute resource
GOOGLE_CHAT_SERVICE_ACCOUNT_EMAIL=open-executive-chat@YOUR_PROJECT_ID.iam.gserviceaccount.com
```

---

## Step 5 — Start the Server

```bash
make dev
```

The webhook endpoint is now active at `POST /webhook/google-chat`. Google Chat will send a JWT-signed request for every message; the server verifies the JWT against your project number before processing.

---

## Step 6 — Test

1. In Google Chat, find your app (search by name in the **+ New chat** dialog).
2. Send it a direct message.
3. It should reply within a few seconds.
4. To watch logs:
   ```bash
   # Docker
   docker compose logs -f core

   # Local dev
   # Check the FastAPI terminal output
   ```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `503 Google Chat integration not configured` | `GOOGLE_CHAT_PROJECT_NUMBER` or both auth vars missing, or server not restarted | Set the project number plus `GOOGLE_CHAT_SERVICE_ACCOUNT_FILE` or `_EMAIL`, and restart |
| `401 Invalid JWT` | Wrong project number | Use the **numeric** Project Number, not the string Project ID |
| No reply, no error in Chat | Handler exception | Check server logs for `Google Chat: handler error` — usually an `ANTHROPIC_API_KEY` issue |
| Bot added to space but never responds | Webhook URL unreachable | Verify the URL is publicly accessible; for local dev, check ngrok is still running |
