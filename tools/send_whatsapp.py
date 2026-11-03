#!/usr/bin/env python3
"""Retired WhatsApp sender; retained as a harmless compatibility entry point.

Disabled at the user's request on 2026-11-03. Never reads credentials or
contacts Meta, including when called by an older local integration.
"""


def main() -> int:
    print("WhatsApp notifications are disabled; no send attempted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
