# Mobile sync (swipe discard/save from the phone, no backend of its own)

The tracker is a static HTML file — it has no server to write back to when
you swipe on your phone. This wires phone swipes into the same always-on
Cloudflare Worker that already relays WhatsApp (see
[`WHATSAPP_SETUP.md`](WHATSAPP_SETUP.md)), adding a second route it serves:

```
you (swipe in the app) --fetch--> Cloudflare Worker --dispatch--> GitHub Actions --> commits the tracker file
```

The Worker never touches the tracker data directly — it only relays the
swipe action to GitHub via `repository_dispatch`, which runs
`tools/mobile_sync.py` against a fresh checkout and pushes the commit. The
Worker holds the GitHub credential so the static app never has to.

## 1. Deploy the Worker (skip if you already did this for WhatsApp)

The Worker is shared — one deploy handles both WhatsApp and mobile sync.
Follow [`WHATSAPP_SETUP.md`](WHATSAPP_SETUP.md) step 3 if you haven't
deployed it yet. If it's already live, just add the one extra secret below
and redeploy.

## 2. Set `APP_SECRET`

This is a shared secret the tracker app sends back on every `/mobile-sync`
request. It is **not real authentication** — the app is a public static
site, so the secret ships in the page's own JS source and anyone who reads
it can forge requests. Its only job is to stop a rando who stumbles on the
Worker URL from spamming `repository_dispatch` events at your repo.

```bash
cd cloudflare-worker
npx wrangler secret put APP_SECRET
```

Generate a random value for it (e.g. `openssl rand -base64 32`), then embed
that same value in the tracker's own JS as `MOBILE_SYNC_SECRET` (search for
that constant near `mobileSyncPush()`) — the app and the Worker must agree
on it. Redeploy the Worker after setting the secret:

```bash
npx wrangler deploy
```

**If you ever lose the value you set**, there's no way to read it back —
Cloudflare secrets are write-only. Generate a new one and repeat this step,
updating `MOBILE_SYNC_SECRET` in the tracker to match.

## 3. Action types

`tools/mobile_sync.py --action <action>` supports four:

| Action | Effect | Fired by |
|---|---|---|
| `discard` | Adds an entry to `DISCARDED_POSTINGS` | Swiping left / "No postularé" |
| `save` | Adds a record to `SAVED_DATA` (status `todo`) | Swiping right / "Guardar" |
| `restore` | Removes the matching `DISCARDED_POSTINGS` entry | Undoing a discard |
| `remove` | Removes the matching `SAVED_DATA` entry | Undoing a save, or deleting a job from Procesos |

All four match on **company + role + jobId together**, not company+role
alone — two genuinely different postings (or two applications to the same
role, e.g. reapplying after a rejection) can share a company+role pair, and
matching on that alone risks a restore/remove silently hitting the wrong
one, or a discard silently colliding with an unrelated listing. This bit
us for real once already (two Fraunhofer Chile Research postings sharing a
placeholder `jobId`, discarding one instead of the other) — keep the third
field in the key if you ever touch this matching logic.

## 4. Concurrency

`.github/workflows/mobile-sync.yml` runs on `repository_dispatch` and can
fire multiple times in quick succession (no confirmation dialog gates a
swipe). It's queued on a `concurrency` group so runs never race on the git
push, and each retry attempt does a full `git fetch` + `reset --hard` +
re-run of `mobile_sync.py` from scratch rather than rebasing a stale
commit — `SAVED_DATA`/`DISCARDED_POSTINGS` get regex-replaced as a whole
array, so two commits touching the same array almost always conflict
textually on a rebase even when the underlying edits don't actually clash.
See the comments in that workflow file for the full reasoning.

## Notes

- `MOBILE_SYNC_URL` and `MOBILE_SYNC_SECRET` in the tracker's JS must point
  at your deployed Worker's `/mobile-sync` route
  (`https://<your-worker>.workers.dev/mobile-sync`).
- The tracker's `Content-Security-Policy` meta tag has an explicit
  `connect-src` allowlist — if you redeploy the Worker under a different
  subdomain, add the new origin there too, or the browser will silently
  block the fetch (no console error visible to the user, just a swipe that
  never syncs — this is exactly the kind of failure `mobileSyncPush()`'s
  toast-on-failure exists to catch).
- Wrangler's OAuth token (`~/Library/Preferences/.wrangler/config/default.toml`)
  expires after about an hour. If deploys start failing with a plain
  authentication error, refresh it via Cloudflare's OAuth token endpoint
  using the stored `refresh_token`, or just run `npx wrangler login` again.
