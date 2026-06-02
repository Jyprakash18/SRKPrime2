# Telegram Premium Subscription Bot — aiogram + Render

Manual payment screenshot approval bot for a private Telegram channel or supergroup.

## Included flow

- `/start` → plan buttons: 1 Month, 3 Months, 6 Months, 1 Year.
- User selects plan → bot shows amount and asks for payment screenshot only; no UPI ID or QR is displayed.
- Screenshot goes to admin panel with **✅ Approve** / **❌ Reject**.
- On approval, premium start/expiry is stored and the user receives a limited-time **join-request invite link**.
- The bot approves only the paid user's join request, then revokes that link.
- On rejection, user receives: `Payment rejected, please contact admin.`
- Expired users are removed/banned automatically when the cleanup task runs.

## User commands

- `/start` — show plans
- `/myplan` — current active plan and expiry
- `/renew` — choose a renewal plan
- `/help` — support message

## Admin commands

- `/users`
- `/premium_users`
- `/addpremium user_id days`
- `/removepremium user_id`
- `/broadcast message`
- `/stats`

## Database data saved

- Telegram user ID, username and full name
- Selected plan and active plan
- Payment request, screenshot file ID and approval status
- Premium start date and expiry date
- Channel/group access status
- Generated invite link usage/revocation state

## 1. Telegram setup

1. Create a bot using **@BotFather** and copy the bot token.
2. Create your private channel or private supergroup.
3. Add the bot as an administrator with permissions to:
   - Invite users / manage invite links
   - Approve join requests
   - Ban or remove members
4. Get your numeric admin user ID and the private chat ID. The premium chat ID usually looks like `-100...`.

The bot never forcibly adds a user. After approval it creates a time-limited link that creates a join request; the bot accepts only the approved user's request.

## 2. Local test

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Fill `.env`. For a public HTTPS webhook during local testing you need an HTTPS tunnel and must set `WEBHOOK_BASE_URL` to that public URL.

Run:

```bash
python -m app.main
```

Health check:

```bash
curl http://localhost:10000/health
```

## 3. Render trial deploy with Blueprint

1. Push this folder to a GitHub repository. Do **not** push `.env`.
2. In Render: **New → Blueprint** and connect the GitHub repository.
3. Render reads `render.yaml` and creates:
   - a Python web service using webhook mode;
   - a Postgres database connected through `DATABASE_URL`.
4. Enter secret environment values when Render asks:
   - `BOT_TOKEN`
   - `ADMIN_IDS` — comma-separated numeric IDs, for example `123456789,987654321`
   - `PREMIUM_CHAT_ID` — private channel/supergroup numeric ID
   - `WEBHOOK_BASE_URL` — your deployed Render URL, for example `https://telegram-premium-bot.onrender.com`
   - `PLAN_1M_AMOUNT`, `PLAN_3M_AMOUNT`, `PLAN_6M_AMOUNT`, `PLAN_12M_AMOUNT`
   - `SUPPORT_TEXT`
5. Redeploy once after setting the final `WEBHOOK_BASE_URL`. The app registers its Telegram webhook at startup.
6. Open `/health` in the browser. Then send `/start` to the bot.

### Important trial limits

Render free services are suitable for testing, not production. A free web service can sleep when idle; local SQLite/files should not be used for real subscription records because service filesystem data is not persistent across restarts/redeploys. Use the Postgres connection configured by the Blueprint.

Render's free Postgres is for trial use and expires after its free period. Export or move data before using the bot for real customers.

## 4. Expiry removal while the free web service sleeps

The app runs an expiry checker while it is awake. A sleeping free web service cannot run its internal loop, so the project also includes:

```text
.github/workflows/expire.yml
```

Set two GitHub repository secrets:

- `BOT_SERVICE_URL` = your Render service URL, for example `https://telegram-premium-bot.onrender.com`
- `CRON_SECRET` = exactly the `CRON_SECRET` value from your Render environment

The workflow calls the protected `/tasks/expire` endpoint four times per hour, which wakes the web service and removes expired users. GitHub scheduled workflows can occasionally be delayed, so use an always-on/paid scheduler later when exact removal timing is important.

Manual test for cleanup:

```bash
curl -X POST \
  -H "Authorization: Bearer YOUR_CRON_SECRET" \
  "https://your-service-name.onrender.com/tasks/expire"
```

## 5. Approval test checklist

1. User sends `/start` and picks a plan.
2. User uploads an image screenshot.
3. Admin receives the image with Approve/Reject buttons.
4. Press **Approve**.
5. User receives `Your premium is activated.` and a join button.
6. User taps the link and submits the join request.
7. Bot automatically approves that matching user and revokes the invite link.
8. Use `/myplan` to confirm expiry.
9. For fast expiry testing, use `/addpremium USER_ID 1` and temporarily set a shorter expiry directly in the database, or test `/removepremium USER_ID`.

## Security notes

- Never commit `BOT_TOKEN`, `WEBHOOK_SECRET`, `CRON_SECRET`, or database URLs.
- Keep the premium channel private.
- Use join-request links rather than shareable one-use links, because the bot can verify the Telegram user ID before admitting access.
- Screenshot verification is manual: approve only after independently checking that payment was actually received.
