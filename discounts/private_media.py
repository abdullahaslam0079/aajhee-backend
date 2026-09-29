from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import urlencode

from django.conf import settings
from django.core import signing
from django.http import Http404
from django.shortcuts import get_object_or_404

from .models import Business

PRIVATE_MEDIA_KINDS = frozenset({"cnic_image", "shop_photo"})
SIGNED_URL_MAX_AGE = 300  # 5 minutes


def _signing_key() -> bytes:
    return settings.SECRET_KEY.encode("utf-8")


def business_private_file(business: Business, kind: str):
    if kind not in PRIVATE_MEDIA_KINDS:
        raise Http404("Unknown media kind.")
    field = getattr(business, kind, None)
    if not field:
        raise Http404("File not found.")
    return field


def build_signed_private_media_url(request, business: Business, kind: str) -> str:
    """Return a short-lived URL for admin to view CNIC/shop photos."""
    field = business_private_file(business, kind)
    # Prefer native S3 querystring auth when available and enabled.
    try:
        storage = field.storage
        if getattr(settings, "USE_S3_MEDIA", False) and hasattr(storage, "url"):
            # django-storages S3Boto3Storage supports expire= for signed URLs
            try:
                return storage.url(field.name, expire=SIGNED_URL_MAX_AGE)
            except TypeError:
                pass
    except Exception:
        pass

    token = signing.dumps(
        {"b": business.id, "k": kind, "exp": int(time.time()) + SIGNED_URL_MAX_AGE},
        salt="aajhee-private-media",
    )
    path = f"/api/admin/businesses/{business.id}/private-media/{kind}/file"
    query = urlencode({"token": token})
    if request:
        return request.build_absolute_uri(f"{path}?{query}")
    return f"{path}?{query}"


def verify_private_media_token(token: str, business_id: int, kind: str) -> bool:
    try:
        payload = signing.loads(token, salt="aajhee-private-media", max_age=SIGNED_URL_MAX_AGE)
    except signing.BadSignature:
        return False
    if int(payload.get("b", -1)) != int(business_id):
        return False
    if payload.get("k") != kind:
        return False
    if int(payload.get("exp", 0)) < int(time.time()):
        return False
    return True


def get_business_for_private_media(business_id: int) -> Business:
    return get_object_or_404(Business, pk=business_id, deleted_at__isnull=True)
