// Two jobs, one always-on Worker (a scheduled GitHub Actions job can't
// receive webhooks or serve phone requests, so this fills that gap):
//
// 1. Relays incoming WhatsApp messages to the job-tracker repo via
//    repository_dispatch (POST /, Meta's webhook shape).
// 2. Bridges phone-swipe actions (discard/save) from the static tracker
//    app -- which has no backend of its own -- to the same repo, also via
//    repository_dispatch (POST /mobile-sync, app's own shape + shared secret).
//
// Required secrets (set with `wrangler secret put <NAME>`, never committed):
//   VERIFY_TOKEN  - a random string you choose; must match Meta's webhook config
//   ALLOWED_FROM  - your WhatsApp number, international format, no '+'
//                   (e.g. 56912345678); messages from any other number are dropped
//   GITHUB_REPO   - "owner/repo", e.g. "FranciscoKirhman/job-tracker"
//   GITHUB_TOKEN  - fine-grained PAT scoped to this repo only, with
//                   Contents: write permission
//   APP_SECRET    - any random string; the tracker app sends it back on every
//                   /mobile-sync request as a shared secret (not real auth,
//                   just enough to stop randos who find this URL from
//                   spamming the repo)

const MOBILE_SYNC_ACTIONS = new Set(["discard", "save"]);

function cors(body, status, extraHeaders = {}) {
  return new Response(body, {
    status,
    headers: {
      "content-type": "application/json",
      "access-control-allow-origin": "*",
      "access-control-allow-headers": "content-type, x-app-secret",
      "access-control-allow-methods": "POST, OPTIONS",
      ...extraHeaders,
    },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/mobile-sync") {
      if (request.method === "OPTIONS") return cors(null, 204);
      if (request.method !== "POST") return cors(JSON.stringify({ error: "POST only" }), 405);
      return handleMobileSync(request, env);
    }

    if (request.method === "GET") {
      return handleVerification(request, env);
    }
    if (request.method === "POST") {
      return handleIncoming(request, env);
    }
    return new Response("Method not allowed", { status: 405 });
  },
};

async function handleMobileSync(request, env) {
  const providedSecret = request.headers.get("x-app-secret") || "";
  if (!env.APP_SECRET || providedSecret !== env.APP_SECRET) {
    return cors(JSON.stringify({ error: "unauthorized" }), 401);
  }

  let payload;
  try {
    payload = await request.json();
  } catch {
    return cors(JSON.stringify({ error: "invalid JSON body" }), 400);
  }

  const { action, company, role } = payload || {};
  if (!MOBILE_SYNC_ACTIONS.has(action)) {
    return cors(JSON.stringify({ error: `action must be one of: ${[...MOBILE_SYNC_ACTIONS].join(", ")}` }), 400);
  }
  if (!company || !role) {
    return cors(JSON.stringify({ error: "company and role are required" }), 400);
  }

  const clientPayload = {
    action,
    company: String(company).slice(0, 300),
    role: String(role).slice(0, 300),
    jobId: String(payload.jobId || "").slice(0, 200),
    location: String(payload.location || "").slice(0, 300),
    fitScore: String(payload.fitScore ?? 0),
    url: String(payload.url || "").slice(0, 500),
    reason: String(payload.reason || "").slice(0, 1000),
    risk: String(payload.risk || "").slice(0, 1000),
  };

  const ghResponse = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "user-agent": "jobs3-mobile-sync-worker",
    },
    body: JSON.stringify({ event_type: "mobile_sync", client_payload: clientPayload }),
  });

  if (ghResponse.status !== 204) {
    const detail = await ghResponse.text().catch(() => "");
    return cors(JSON.stringify({ error: "GitHub dispatch failed", status: ghResponse.status, detail }), 502);
  }

  return cors(JSON.stringify({ ok: true }), 202);
}

function handleVerification(request, env) {
  const url = new URL(request.url);
  const mode = url.searchParams.get("hub.mode");
  const token = url.searchParams.get("hub.verify_token");
  const challenge = url.searchParams.get("hub.challenge");

  if (mode === "subscribe" && token === env.VERIFY_TOKEN) {
    return new Response(challenge ?? "", { status: 200 });
  }
  return new Response("Forbidden", { status: 403 });
}

async function handleIncoming(request, env) {
  let payload;
  try {
    payload = await request.json();
  } catch {
    return new Response("Bad request", { status: 400 });
  }

  try {
    const message = payload?.entry?.[0]?.changes?.[0]?.value?.messages?.[0];
    if (message && message.from === env.ALLOWED_FROM) {
      const text = message.text?.body ?? "";
      if (text) {
        await dispatchToGitHub(env, text, message.from);
      }
    }
    // Anything else (delivery/read status callbacks, messages from other
    // numbers) is intentionally ignored, not an error.
  } catch (err) {
    console.error("whatsapp relay error", err);
  }

  // Always ack quickly with 200 so Meta doesn't retry or disable the webhook.
  return new Response("ok", { status: 200 });
}

async function dispatchToGitHub(env, command, from) {
  const response = await fetch(
    `https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "User-Agent": "jobs3-whatsapp-worker",
      },
      body: JSON.stringify({
        event_type: "whatsapp_command",
        client_payload: { command, from },
      }),
    }
  );
  if (!response.ok) {
    console.error("GitHub dispatch failed", response.status, await response.text());
  }
}
