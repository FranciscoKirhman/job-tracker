#!/usr/bin/env python3
"""Send one WhatsApp text message via the Meta Cloud API.

Reads credentials from environment variables so no secret ever needs to
be passed on the command line or committed to the repo:

  WHATSAPP_TOKEN     permanent access token for the Meta app/system user
  WHATSAPP_PHONE_ID  the "Phone Number ID" from the Meta app dashboard
  WHATSAPP_TO        recipient number in international format, no '+'
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    if len(sys.argv) > 1:
        text = sys.argv[1]
    else:
        text = sys.stdin.read()
    text = text.strip()
    if not text:
        print("Nothing to send.", file=sys.stderr)
        return 1

    token = os.environ["WHATSAPP_TOKEN"]
    phone_id = os.environ["WHATSAPP_PHONE_ID"]
    to = os.environ["WHATSAPP_TO"]

    url = f"https://graph.facebook.com/v20.0/{phone_id}/messages"
    body = json.dumps(
        {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text[:4096]},
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request) as response:
            print(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"WhatsApp API error {exc.code}: {exc.read().decode('utf-8')}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
