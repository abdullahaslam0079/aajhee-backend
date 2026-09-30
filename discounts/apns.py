"""Direct APNs HTTP/2 sender (iOS fallback when FCM Admin IAM is broken).

Env:
  APNS_KEY_PATH or APNS_KEY_PEM   .p8 file path or PEM contents
  APNS_KEY_ID                     e.g. U4ZBFCM8MN
  APNS_TEAM_ID                    e.g. J5F5G7B832
  APNS_BUNDLE_ID                  default com.aajhee.app
  APNS_USE_SANDBOX                true for USB/dev installs, false for TestFlight/App Store
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_jwt_cache: tuple[float, str] | None = None


def apns_configured() -> bool:
    key_id = (os.environ.get("APNS_KEY_ID") or "").strip()
    team_id = (os.environ.get("APNS_TEAM_ID") or "").strip()
    pem = (os.environ.get("APNS_KEY_PEM") or "").strip()
    path = (os.environ.get("APNS_KEY_PATH") or "").strip()
    return bool(key_id and team_id and (pem or path))


def _load_private_key() -> str:
    pem = (os.environ.get("APNS_KEY_PEM") or "").strip()
    if pem:
        return pem.replace("\\n", "\n")
    path = (os.environ.get("APNS_KEY_PATH") or "").strip()
    if not path:
        raise RuntimeError("APNS_KEY_PEM or APNS_KEY_PATH required")
    return Path(path).expanduser().read_text()


def _bearer_token() -> str:
    global _jwt_cache
    now = time.time()
    if _jwt_cache and now - _jwt_cache[0] < 50 * 60:
        return _jwt_cache[1]

    try:
        import jwt
    except ImportError as exc:
        raise RuntimeError("PyJWT is required for APNs: pip install PyJWT cryptography") from exc

    key_id = (os.environ.get("APNS_KEY_ID") or "").strip()
    team_id = (os.environ.get("APNS_TEAM_ID") or "").strip()
    token = jwt.encode(
        {"iss": team_id, "iat": int(now)},
        _load_private_key(),
        algorithm="ES256",
        headers={"alg": "ES256", "kid": key_id},
    )
    _jwt_cache = (now, token)
    return token


def send_apns_to_tokens(
    *,
    tokens: list[str],
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
) -> int:
    """Send alert pushes via APNs. Returns number of HTTP 200 responses."""
    cleaned = [t.strip() for t in tokens if t and t.strip()]
    if not cleaned:
        return 0
    if not apns_configured():
        logger.info("APNs skipped: APNS_KEY_ID/TEAM_ID/KEY not configured")
        return 0

    try:
        import httpx
    except ImportError:
        logger.warning("APNs skipped: httpx not installed (pip install 'httpx[http2]')")
        return 0

    bundle_id = (os.environ.get("APNS_BUNDLE_ID") or "com.aajhee.app").strip()
    mode = (os.environ.get("APNS_USE_SANDBOX") or "auto").strip().lower()
    if mode in {"1", "true", "yes", "on", "sandbox"}:
        hosts = ["api.sandbox.push.apple.com"]
    elif mode in {"0", "false", "no", "off", "production"}:
        hosts = ["api.push.apple.com"]
    else:
        # auto: try sandbox first (USB/dev), then production (TestFlight/App Store)
        hosts = ["api.sandbox.push.apple.com", "api.push.apple.com"]

    string_data = {
        str(k): str(v)
        for k, v in (data or {}).items()
        if v is not None and str(v).strip()
    }
    payload = {
        "aps": {
            "alert": {"title": title, "body": body},
            "sound": "default",
            "badge": 1,
        },
        **string_data,
    }
    body_bytes = json.dumps(payload).encode("utf-8")

    try:
        auth = _bearer_token()
    except Exception:
        logger.exception("APNs JWT build failed")
        return 0

    success = 0
    headers_base = {
        "authorization": f"bearer {auth}",
        "apns-topic": bundle_id,
        "apns-push-type": "alert",
        "apns-priority": "10",
        "content-type": "application/json",
    }

    logger.info(
        "APNs sending to %s device(s) hosts=%s title=%r type=%s",
        len(cleaned),
        hosts,
        title,
        string_data.get("type"),
    )

    try:
        with httpx.Client(http2=True, timeout=30.0) as client:
            for token in cleaned:
                delivered = False
                last_status = None
                last_body = ""
                for host in hosts:
                    headers = {
                        **headers_base,
                        "apns-id": str(uuid.uuid4()),
                    }
                    url = f"https://{host}/3/device/{token}"
                    try:
                        resp = client.post(url, headers=headers, content=body_bytes)
                    except Exception:
                        logger.exception(
                            "APNs request failed host=%s token …%s",
                            host,
                            token[-8:],
                        )
                        continue
                    last_status = resp.status_code
                    last_body = resp.text[:300]
                    if resp.status_code == 200:
                        success += 1
                        delivered = True
                        break
                    # Wrong APNs environment for this device token — try next host.
                    if resp.status_code == 400 and "BadDeviceToken" in (resp.text or ""):
                        continue
                    break
                if not delivered:
                    logger.warning(
                        "APNs send failed for token …%s status=%s body=%s",
                        token[-8:],
                        last_status,
                        last_body,
                    )
    except Exception:
        logger.exception("APNs client failed")
        return success

    logger.info("APNs done: %s/%s succeeded", success, len(cleaned))
    return success
