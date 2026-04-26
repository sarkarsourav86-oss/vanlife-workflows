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
import time

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
    """Edit the deferred response with the actual content.

    PATCH /messages/@original is the canonical pattern: it replaces the
    "Bot is thinking..." placeholder in place. Retries on 404 because
    Discord's deferred-response registration is eventually consistent —
    .spawn.aio() can run faster than Discord registers our type-5 reply.
    """
    url = f"https://discord.com/api/v10/webhooks/{app_id}/{interaction_token}/messages/@original"
    delays = (0.5, 1.0, 2.0, 4.0, 8.0)  # 5 retries; total wait <= 15.5s
    last_response_text: str = ""
    for delay in delays:
        r = httpx.patch(url, json={"content": content}, timeout=15.0)
        if r.status_code == 404:
            last_response_text = r.text
            print(f"[discord followup] 404, retrying in {delay}s ({r.text[:200]})")
            time.sleep(delay)
            continue
        if not r.is_success:
            print(f"[discord followup] {r.status_code}: {r.text[:500]}")
        r.raise_for_status()
        return
    raise httpx.HTTPStatusError(
        f"Discord 404 after {len(delays)} retries on PATCH @original. "
        f"Last response body: {last_response_text[:500]}",
        request=r.request,
        response=r,
    )


def followup_url(interaction_token: str, app_id: str | None = None) -> str:
    """Build the followup URL — useful when callers want to PATCH directly."""
    app_id = app_id or os.environ["DISCORD_APP_ID"]
    return f"https://discord.com/api/v10/webhooks/{app_id}/{interaction_token}/messages/@original"
