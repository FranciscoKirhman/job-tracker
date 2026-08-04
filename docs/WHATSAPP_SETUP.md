# WhatsApp notifications + two-way commands

This wires the tracker up to WhatsApp using Meta's official Cloud API:
GitHub Actions sends outbound notifications directly (it's just an HTTP
call), and a small always-on Cloudflare Worker receives your replies and
relays them back into GitHub Actions, since a scheduled Actions job can't
itself host a webhook.

```
you (WhatsApp) --reply--> Meta --webhook--> Cloudflare Worker --dispatch--> GitHub Actions --> tracker + WhatsApp reply
GitHub Actions --schedule--> WhatsApp Cloud API --message--> you
```

## 1. Meta app + WhatsApp number

1. Go to [developers.facebook.com](https://developers.facebook.com) and create a developer account if you don't have one.
2. Create a new App, add the **WhatsApp** product to it.
3. In the app's WhatsApp > API Setup page, note down:
   - the **temporary access token** (works for 24h, fine for testing)
   - the **Phone Number ID**
4. Add your own WhatsApp number as a test recipient on that same page (free tier only delivers to pre-approved numbers until Meta reviews the app).
5. Once testing works, get a **permanent token**: Meta Business Settings → Users → System Users → create one, assign it to the app with `whatsapp_business_messaging` permission, generate a token with no expiry.

## 2. GitHub repo secrets

In the repo's Settings → Secrets and variables → Actions, add:

| Secret | Value |
|---|---|
| `WHATSAPP_TOKEN` | the permanent access token from step 1 |
| `WHATSAPP_PHONE_ID` | the Phone Number ID from step 1 |
| `WHATSAPP_TO` | your WhatsApp number, international format, no `+` (e.g. `56912345678`) |

This alone enables the scheduled daily "pipeline" digest — `.github/workflows/whatsapp.yml` runs on a cron and sends it automatically. Two-way commands need step 3.

## 3. Cloudflare Worker (for receiving your replies)

1. Create a free account at [cloudflare.com](https://cloudflare.com) if needed.
2. From `cloudflare-worker/`, install and log in:
   ```bash
   npx wrangler login
   ```
3. Set the worker's secrets (values, not files — nothing here gets committed):
   ```bash
   npx wrangler secret put VERIFY_TOKEN
   npx wrangler secret put ALLOWED_FROM
   npx wrangler secret put GITHUB_REPO
   npx wrangler secret put GITHUB_TOKEN
   ```
   - `VERIFY_TOKEN`: any random string you make up, you'll reuse it in step 4.
   - `ALLOWED_FROM`: your own WhatsApp number (no `+`) — messages from anyone else are dropped.
   - `GITHUB_REPO`: `FranciscoKirhman/job-tracker`.
   - `GITHUB_TOKEN`: a GitHub fine-grained PAT (Settings → Developer settings → Fine-grained tokens), scoped to only this repo, with **Contents: read/write** and **Actions: read/write** permissions.
4. Deploy:
   ```bash
   npx wrangler deploy
   ```
   This prints a URL like `https://jobs3-whatsapp-relay.<you>.workers.dev`.

## 4. Point Meta's webhook at the Worker

In the Meta app's WhatsApp > Configuration page:
- **Callback URL**: the Worker URL from step 3.
- **Verify token**: the same `VERIFY_TOKEN` value you set in step 3.
- Subscribe to the `messages` webhook field.

Meta will call the Worker once to verify (a GET request); it should show as verified immediately.

## Command syntax

Send these as plain WhatsApp messages to your own configured number:

- `pipeline` — status digest: stage counts, a "Qué hacer hoy" action summary, upcoming deadlines, new postings from `MARKET_HISTORY` ranked by fit (verified vs. unverified shown separately), and a failed-sources summary.
- `update: <company> | <status> | <YYYY-MM-DD>` — update an existing application's status. The date is optional (defaults to today). Status must be one of: `todo`, `applied`, `interview`, `offer`, `rejected`, `withdrawn`.

  Examples:
  ```
  update: Roche | interview
  update: bioMérieux | rejected | 2026-08-10
  ```

  If the company name matches more than one tracked application, you'll get a reply listing the ambiguous ones so you can be more specific (e.g. include part of the role too).

- `sources` — list every `FAILED_SOURCES` entry in full, with its manual-fix note.
- `sources: <name> | ok` or `sources: <name> | updated` — resolve one (fuzzy match on the source name), removing it from `FAILED_SOURCES` and logging it to `REVIEWED_SOURCES`. Use `ok` when there's nothing to fix, `updated` when you've fixed it.

This only updates *existing* records. Adding a brand-new application still goes through `tools/update_job_tracker.py` with a full structured posting file, since a one-line WhatsApp message doesn't carry enough information (job description, requirements, etc.) to create a proper record.

## Notes

- The Worker never touches the tracker data directly — it only relays your message text to GitHub, which does the actual read/write and holds the tracker as the single source of truth.
- If you ever want to change the daily digest time, edit the `cron` line in `.github/workflows/whatsapp.yml` (it's in UTC).
- You can also trigger a one-off command manually from the GitHub Actions tab: Actions → WhatsApp tracker sync → Run workflow → enter a command.
