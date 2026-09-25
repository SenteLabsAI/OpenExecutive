# Telegram Integration Setup

The Open Executive Telegram bot receives messages via a webhook (`POST /webhook/telegram`) and replies through the Telegram Bot API. The integration is already implemented — you just need a bot token, a webhook secret, and a publicly reachable API.

---

## Step 1 — Create the Bot

1. In Telegram, open a chat with [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts (display name, then a username ending in `bot`).
3. BotFather replies with a token like `123456789:AA...`. This is your `TELEGRAM_BOT_TOKEN` — treat it as a secret.

---

## Step 2 — Generate a Webhook Secret

Telegram echoes a secret of your choosing back in the `X-Telegram-Bot-Api-Secret-Token` header of every webhook request. The API compares that header against `TELEGRAM_WEBHOOK_SECRET` and rejects mismatches with `401`.

Telegram only accepts 1–256 characters from `A-Z`, `a-z`, `0-9`, `_` and `-`. A hex string fits:

```bash
openssl rand -hex 32
```

---

## Step 3 — Set Environment Variables

```bash
TELEGRAM_BOT_TOKEN=123456789:AA...          # from Step 1
TELEGRAM_WEBHOOK_SECRET=<value from Step 2>
```

Restart the API so it picks them up (`make dev` locally). Without `TELEGRAM_BOT_TOKEN` the webhook answers `503 Telegram integration not configured`.

If `TELEGRAM_WEBHOOK_SECRET` is left unset, the header check is skipped and anyone who finds the URL can post fake updates to it. Always set it for a public deployment.

---

## Step 4 — Expose the Webhook

Telegram must reach `https://<your-api-host>/webhook/telegram` over HTTPS. The route is exempt from the `BACKEND_SHARED_SECRET` gate (see [auth.md](auth.md)) — the webhook secret is its authentication.

For **local development**, expose your server with [ngrok](https://ngrok.com):

```bash
ngrok http 8000
# Use the HTTPS forwarding URL, e.g. https://abc123.ngrok-free.app/webhook/telegram
```

---

## Step 5 — Register the Webhook

Pass the **same** secret as `secret_token`. Omitting it registers a webhook that sends no header, and every update is rejected with `401`.

```bash
curl -F "url=https://<your-api-host>/webhook/telegram" \
     -F "secret_token=$TELEGRAM_WEBHOOK_SECRET" \
     "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook"
```

Expected response: `{"ok":true,"result":true,"description":"Webhook was set"}`.

Re-run this command whenever the host URL or the secret changes. Verify the registration with:

```bash
curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getWebhookInfo"
```

`last_error_message` in that response shows the most recent delivery failure (e.g. `Wrong response from the webhook: 401 Unauthorized`).

---

## Step 6 — Grant Access

Access is roster-driven. The bot only answers a chat whose ID matches the `telegram_chat_id` of a non-archived Person — add or edit the Person in the **/people** UI. Messages from any other chat are dropped without a reply.

To find a chat ID, message the bot once and check the API logs for:

```
Telegram: rejected message from chat_id=<id> (not in People roster)
```

The same rejection is recorded in the audit log as an `integration_inbound` event with `outcome=rejected_unknown_sender`.

---

## Step 7 — Test

Send the bot a direct message. It should reply within a few seconds. `/start`, `/help` and `/ask` prefixes are stripped before the text reaches the Executive; photos and documents are passed along as attachments.

If it doesn't reply, open **Settings → Setup status** in the web app. The Telegram light says whether the token works, whether the webhook is registered at this app's address, what went wrong with Telegram's last delivery, and whether anyone on the team list has a Telegram chat ID.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `getWebhookInfo` shows `401 Unauthorized` | `secret_token` missing from `setWebhook`, or it differs from `TELEGRAM_WEBHOOK_SECRET` | Re-run Step 5 with the current secret |
| `503 Telegram integration not configured` | `TELEGRAM_BOT_TOKEN` unset or API not restarted | Set the var and restart |
| No reply, `rejected message from chat_id=…` in logs | Chat not on the People roster | Add the chat ID to a Person in /people (Step 6) |
| No reply, nothing in logs | Webhook URL unreachable | Check `getWebhookInfo`; for local dev, confirm ngrok is still running and re-register if its URL changed |
