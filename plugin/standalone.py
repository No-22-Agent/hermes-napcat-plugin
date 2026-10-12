"""Standalone out-of-process QQ sender (cron delivery via NapCat's OneBot HTTP API).

Kept from the original hermes-napcat plugin.  Used by
``tools/send_message_tool._send_via_adapter`` when the gateway runner is not in
this process (e.g. ``hermes cron run`` separate from ``hermes gateway``).

NapCat's link to Hermes is a *reverse* WebSocket — NapCat dials INTO the
gateway's listener, which a cron subprocess cannot reproduce (the gateway
already holds the port, and NapCat will not dial a second target on its own).
So the standalone path uses NapCat's **OneBot 11 HTTP API** instead: enable the
HTTP server in NapCat and point the plugin at its base URL via ``http_url``
(config.yaml ``platforms.napcat.extra``) or ``NAPCAT_HTTP_URL`` (env).  The
access token is the same ``NAPCAT_TOKEN`` used for the WS.

``thread_id`` and ``media_files`` are accepted for signature parity; the
standalone HTTP path delivers plain text only (the live adapter handles media).
``force_document`` is ignored.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from gateway.platforms._shared import get_scoped_secret as _get_scoped_secret, send_error

from .formatting import strip_markdown

logger = logging.getLogger(__name__)


def _chunk_text(text: str, limit: int = 4500) -> list[str]:
    """Split long text at the nearest newline/space boundary within ``limit``."""
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    while text:
        if len(text) <= limit:
            out.append(text)
            break
        split = text.rfind("\n", 0, limit)
        if split <= 0:
            split = text.rfind(" ", 0, limit)
        if split <= 0:
            split = limit
        out.append(text[:split])
        text = text[split:].lstrip()
    return out


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[list[str]] = None,
    force_document: bool = False,
) -> dict[str, Any]:
    import asyncio

    import aiohttp

    extra = getattr(pconfig, "extra", None) or {}
    base_url = str(
        _get_scoped_secret("NAPCAT_HTTP_URL") or extra.get("http_url") or ""
    ).rstrip("/")
    if not base_url:
        return {
            "error": (
                "NapCat standalone send: set NAPCAT_HTTP_URL (or http_url under "
                "platforms.napcat.extra) to your NapCat OneBot HTTP API base URL "
                "to enable out-of-process delivery"
            )
        }
    token = str(
        extra.get("token")
        or getattr(pconfig, "token", None)
        or _get_scoped_secret("NAPCAT_TOKEN")
        or ""
    ).strip()

    is_group = chat_id.startswith("group:")
    try:
        target_id = int(chat_id.split(":", 1)[1] if is_group else chat_id)
    except (TypeError, ValueError):
        return send_error(f"NapCat standalone send: invalid chat_id {chat_id!r}")

    action = "send_group_msg" if is_group else "send_private_msg"
    id_key = "group_id" if is_group else "user_id"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def _chunks(text: str) -> list[str]:
        return _chunk_text(text)

    last_id: Optional[str] = None
    try:
        async with aiohttp.ClientSession() as session:
            for chunk in _chunks(strip_markdown(message)):
                payload = {
                    id_key: target_id,
                    "message": [{"type": "text", "data": {"text": chunk}}],
                }
                try:
                    async with session.post(
                        f"{base_url}/{action}",
                        json=payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as resp:
                        try:
                            body = await resp.json(content_type=None)
                        except ValueError:
                            body = {}
                        if not resp.ok:
                            return send_error(
                                f"NapCat standalone send: HTTP {resp.status}: {body}"
                            )
                except aiohttp.ClientError as exc:
                    return send_error(f"NapCat standalone send: connection failed: {exc}")
                status = body.get("status")
                retcode = body.get("retcode")
                # Strict: OneBot HTTP always answers {status: "ok", retcode: 0}
                # on success; treat a non-ok status, a non-zero retcode, or a
                # body carrying neither field as a failure.
                failed = (
                    (status is not None and status != "ok")
                    or (retcode is not None and retcode != 0)
                    or (status is None and retcode is None)
                )
                if failed:
                    msg = body.get("message") or body.get("msg") or f"status={status!r} retcode={retcode!r}"
                    return send_error(f"NapCat standalone send ({action}): {msg}")
                data = body.get("data") or {}
                if data.get("message_id"):
                    last_id = str(data["message_id"])
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return send_error(f"NapCat standalone send failed: {exc}")

    return {"success": True, "message_id": last_id}
