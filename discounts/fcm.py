"""Firebase Cloud Messaging helpers.

Push is best-effort: if credentials are missing or firebase-admin is not
installed, calls no-op so the in-app inbox still works.

Canonical data keys (all string values for FCM):
  type, notification_id, order_public_id, business_id, branch_id, route
plus optional context keys (status, payment_status, offer_id, etc.).
"""

from __future__ import annotations

import logging
from typing import Any

from .firebase_app import get_firebase_app

logger = logging.getLogger(__name__)

# Keys clients rely on for routing / refresh. Always stringified when present.
CANONICAL_FCM_DATA_KEYS = (
    "type",
    "notification_id",
    "order_public_id",
    "business_id",
    "branch_id",
    "route",
    "status",
    "payment_status",
    "offer_id",
    "review_id",
    "product_id",
    "order_id",
)


def normalize_fcm_data(data: dict[str, Any] | None) -> dict[str, str]:
    """Return FCM-safe string data with stable key names."""
    raw = dict(data or {})
    out: dict[str, str] = {}

    for key in CANONICAL_FCM_DATA_KEYS:
        if key not in raw or raw[key] is None:
            continue
        value = str(raw[key]).strip()
        if value:
            out[key] = value

    # Prefer UUID public id; copy from order_id when it looks like a UUID.
    if "order_public_id" not in out:
        order_id = out.get("order_id", "")
        if order_id and not order_id.isdigit():
            out["order_public_id"] = order_id

    # Include any extra keys as strings so callers are not silently dropped.
    for key, value in raw.items():
        if key in out or value is None:
            continue
        text = str(value).strip()
        if text:
            out[str(key)] = text

    return out


def _is_invalid_token_error(exc: Exception) -> bool:
    name = type(exc).__name__
    if name in {"UnregisteredError", "SenderIdMismatchError"}:
        return True
    code = getattr(exc, "code", None) or getattr(exc, "cause", None)
    code_text = str(code or exc).upper()
    return any(
        token in code_text
        for token in (
            "UNREGISTERED",
            "NOT_FOUND",
            "REGISTRATION-TOKEN-NOT-REGISTERED",
            "INVALID_REGISTRATION",
        )
    )


def send_fcm_to_tokens(
    *,
    tokens: list[str],
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
) -> int:
    """Send FCM to tokens. Returns number of successful deliveries."""
    if not tokens:
        return 0

    app = get_firebase_app()
    if app is None:
        logger.warning(
            "FCM skipped: Firebase Admin not configured "
            "(set FIREBASE_CREDENTIALS_JSON or FIREBASE_CREDENTIALS_PATH)."
        )
        return 0

    try:
        from firebase_admin import messaging
    except ImportError:
        return 0

    string_data = normalize_fcm_data(data)
    messages = [
        messaging.Message(
            token=token,
            notification=messaging.Notification(title=title, body=body),
            data=string_data,
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    # Must match Flutter PushNotificationService / AndroidManifest.
                    channel_id="aajhee_default",
                    priority="high",
                    default_sound=True,
                    default_vibrate_timings=True,
                ),
            ),
            apns=messaging.APNSConfig(
                headers={"apns-priority": "10"},
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(sound="default", badge=1),
                ),
            ),
        )
        for token in tokens
    ]

    stale_tokens: list[str] = []
    success = 0

    # Prefer send_each (batch) when available; fall back to sequential send.
    logger.info(
        "FCM sending to %s device(s) title=%r type=%s",
        len(tokens),
        title,
        string_data.get("type"),
    )
    try:
        batch = messaging.send_each(messages, app=app)
        success = sum(1 for r in batch.responses if r.success)
        logger.info("FCM send_each done: %s/%s succeeded", success, len(tokens))
        for token, resp in zip(tokens, batch.responses):
            if resp.success:
                continue
            exc = resp.exception
            if exc is not None and _is_invalid_token_error(exc):
                stale_tokens.append(token)
                logger.info("FCM token stale, will remove …%s", token[-8:])
            elif exc is not None:
                logger.warning("FCM send failed for token …%s: %s", token[-8:], exc)
    except AttributeError:
        for token, message in zip(tokens, messages):
            try:
                messaging.send(message, app=app)
                success += 1
            except Exception as exc:
                if _is_invalid_token_error(exc):
                    stale_tokens.append(token)
                    logger.info("FCM token stale, will remove …%s", token[-8:])
                else:
                    logger.warning(
                        "FCM send failed for token …%s: %s", token[-8:], exc
                    )
    except Exception:
        logger.exception("FCM send_each failed; no messages delivered this attempt")

    if stale_tokens:
        try:
            from .models import DeviceToken

            deleted, _ = DeviceToken.objects.filter(token__in=stale_tokens).delete()
            if deleted:
                logger.info("Removed %s invalid FCM device token(s)", deleted)
        except Exception:
            logger.exception("Failed to delete stale FCM device tokens")

    return success
