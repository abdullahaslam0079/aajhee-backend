"""Twilio WhatsApp (and optional SMS) delivery for merchant alerts."""

from __future__ import annotations

import logging
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode

from django.conf import settings

from .phone_utils import normalize_pakistani_mobile

logger = logging.getLogger(__name__)


def _to_whatsapp_address(raw: str) -> str | None:
    cleaned = (raw or "").strip()
    if not cleaned:
        return None
    if cleaned.lower().startswith("whatsapp:"):
        return cleaned
    try:
        e164 = normalize_pakistani_mobile(cleaned)
    except Exception:
        # Allow already-international numbers that are not PK mobiles.
        digits = "".join(ch for ch in cleaned if ch.isdigit() or ch == "+")
        if digits.startswith("00"):
            digits = f"+{digits[2:]}"
        if not digits.startswith("+"):
            digits = f"+{digits}"
        e164 = digits
    return f"whatsapp:{e164}"


def send_twilio_whatsapp(*, to: str, body: str) -> bool:
    """
    Send a WhatsApp message via Twilio REST API.

    Returns True on HTTP 2xx. Logs and returns False on skip/failure.
    """
    sid = (getattr(settings, "TWILIO_ACCOUNT_SID", None) or "").strip()
    token = (getattr(settings, "TWILIO_AUTH_TOKEN", None) or "").strip()
    from_addr = (getattr(settings, "TWILIO_WHATSAPP_FROM", None) or "").strip()
    if not sid or not token or not from_addr:
        logger.info("Twilio WhatsApp skipped (missing TWILIO_* credentials)")
        return False

    to_addr = _to_whatsapp_address(to)
    if not to_addr:
        logger.warning("Twilio WhatsApp skipped (invalid destination): %s", to)
        return False

    if not from_addr.lower().startswith("whatsapp:"):
        from_addr = f"whatsapp:{from_addr}"

    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    payload = urllib.parse.urlencode(
        {
            "To": to_addr,
            "From": from_addr,
            "Body": body,
        }
    ).encode("utf-8")
    auth = b64encode(f"{sid}:{token}".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status = getattr(response, "status", 200)
            if 200 <= int(status) < 300:
                logger.info("Twilio WhatsApp sent to %s", to_addr)
                return True
            logger.warning(
                "Twilio WhatsApp unexpected status %s for %s", status, to_addr
            )
            return False
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        logger.warning(
            "Twilio WhatsApp HTTP %s for %s: %s", exc.code, to_addr, detail
        )
        return False
    except Exception:
        logger.exception("Twilio WhatsApp send failed for %s", to_addr)
        return False
