"""Discord slash-command interaction handler.

Discord posts every slash-command invocation to a single endpoint with an
Ed25519 signature. We verify the signature, route on `data.name`, and
either respond inline (fast commands) or defer + followup (slow commands).

Why split from src/discord.py:
  - src/discord.py wraps the *outgoing* incoming-webhook (we post to Discord).
  - this module handles *incoming* interactions (Discord posts to us).
  Different auth, different shape, different lifecycle.

Public surface:
  - verify_signature(public_key, signature, timestamp, body) -> bool
  - send_followup(app_id, interaction_token, content) -> None
"""

from __future__ import annotations

import os

import httpx
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey


def verify_signature(
    public_key_hex: str,
    signature_hex: str,
    timestamp: str,
    body: bytes,
) -> bool:
    """Ed25519 signature check on the raw request body.

    Discord signs `timestamp + body` with their private key; we verify with
    the public key from the application's General Information page.
    Returns True on success, False on any failure (bad sig, malformed key,
    wrong length, etc.).
    """
    try:
        verify_key = VerifyKey(bytes.fromhex(public_key_hex))
        verify_key.verify(timestamp.encode() + body, bytes.fromhex(signature_hex))
        return True
    except (BadSignatureError, ValueError):
        return False


def send_followup(app_id: str, interaction_token: str, content: str) -> None:
    """POST a followup message to a deferred interaction.

    After we return {type: 5} (deferred), Discord shows "Bot is thinking…"
    until we POST /webhooks/{app_id}/{token}. POST is more forgiving than
    PATCH /messages/@original, which 404s if the deferred reply hasn't
    fully landed yet. The token is valid for 15 minutes.
    """
    url = f"https://discord.com/api/v10/webhooks/{app_id}/{interaction_token}"
    r = httpx.post(url, json={"content": content}, timeout=15.0)
    r.raise_for_status()


def followup_url(interaction_token: str, app_id: str | None = None) -> str:
    """Build the followup URL — useful when callers want to PATCH directly."""
    app_id = app_id or os.environ["DISCORD_APP_ID"]
    return f"https://discord.com/api/v10/webhooks/{app_id}/{interaction_token}/messages/@original"
