// Relays incoming WhatsApp messages to the job-tracker repo's GitHub Actions
// workflow via repository_dispatch. Deployed separately from the repo's own
// automation because it must be always-on to receive Meta's webhook calls,
// which a scheduled GitHub Actions job cannot be.
//
// Required secrets (set with `wrangler secret put <NAME>`, never committed):
//   VERIFY_TOKEN  - a random string you choose; must match Meta's webhook config
//   ALLOWED_FROM  - your WhatsApp number, international format, no '+'
//                   (e.g. 56912345678); messages from any other number are dropped
//   GITHUB_REPO   - "owner/repo", e.g. "FranciscoKirhman/job-tracker"
//   GITHUB_TOKEN  - fine-grained PAT scoped to this repo only, with
//                   Contents: write and Actions: write permissions

export default {
  async fetch(request, env) {
    if (request.method === "GET") {
      return handleVerification(request, env);
    }
    if (request.method === "POST") {
      return handleIncoming(request, env);
    }
    return new Response("Method not allowed", { status: 405 });
  },
};

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
