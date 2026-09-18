"""Firebase Auth (phone / Google / Apple) → Django user helpers."""

from __future__ import annotations

import logging
import re

from django.contrib.auth import get_user_model
from rest_framework.exceptions import AuthenticationFailed, ValidationError

from .firebase_app import get_firebase_app

logger = logging.getLogger(__name__)
User = get_user_model()

_PHONE_LOCAL_DOMAIN = "phone.aajhee.local"
_FIREBASE_LOCAL_DOMAIN = "firebase.aajhee.local"


def _synthetic_email_for_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if not digits:
        raise ValidationError({"id_token": ["Phone number claim is invalid."]})
    return f"{digits}@{_PHONE_LOCAL_DOMAIN}"


def _synthetic_email_for_uid(firebase_uid: str) -> str:
    return f"{firebase_uid}@{_FIREBASE_LOCAL_DOMAIN}"


def _split_display_name(name: str) -> tuple[str, str]:
    cleaned = name.strip()
    if not cleaned:
        return "", ""
    first, _, last = cleaned.partition(" ")
    return first.strip(), last.strip()


def verify_firebase_id_token(id_token: str) -> dict:
    """Verify a Firebase ID token and return decoded claims.

    Raises AuthenticationFailed on invalid/expired tokens or missing Firebase config.
    """
    app = get_firebase_app()
    if app is None:
        raise AuthenticationFailed(
            "Firebase authentication is unavailable. Credentials are not configured."
        )

    try:
        from firebase_admin import auth as firebase_auth
    except ImportError as exc:
        raise AuthenticationFailed(
            "Firebase authentication is unavailable. firebase-admin is not installed."
        ) from exc

    try:
        return firebase_auth.verify_id_token(id_token, app=app)
    except Exception as exc:
        logger.info("Firebase ID token verification failed: %s", exc)
        raise AuthenticationFailed("Invalid or expired Firebase ID token.") from exc


# Backwards-compatible alias used by older imports/tests.
verify_firebase_phone_id_token = verify_firebase_id_token


def get_or_create_consumer_from_firebase_claims(claims: dict) -> User:
    """Resolve or create a consumer from verified Firebase Auth claims.

    Supports phone, Google, Apple, and other Firebase providers that mint an ID token.
    """
    firebase_uid = (claims.get("uid") or "").strip()
    phone = (claims.get("phone_number") or "").strip() or None
    email_claim = (claims.get("email") or "").strip().lower() or None
    display_name = (claims.get("name") or "").strip()

    if not firebase_uid:
        raise ValidationError({"id_token": ["Firebase UID is missing from the token."]})
    if not phone and not email_claim:
        # Apple may omit email on later sign-ins; uid alone is enough to create/login.
        pass

    user = User.objects.filter(firebase_uid=firebase_uid).first()
    if user is None and phone:
        user = User.objects.filter(phone=phone).first()
    if user is None and email_claim:
        user = User.objects.filter(email__iexact=email_claim).first()

    if user is not None:
        if user.account_type != User.AccountType.CONSUMER:
            raise ValidationError(
                {
                    "id_token": [
                        "This identity is linked to a non-consumer account."
                    ]
                }
            )
        update_fields: list[str] = []
        if user.firebase_uid != firebase_uid:
            user.firebase_uid = firebase_uid
            update_fields.append("firebase_uid")
        if phone and user.phone != phone:
            user.phone = phone
            update_fields.append("phone")
        if display_name and not user.get_full_name().strip():
            first_name, last_name = _split_display_name(display_name)
            user.first_name = first_name
            user.last_name = last_name
            update_fields.extend(["first_name", "last_name"])
        if update_fields:
            user.save(update_fields=list(dict.fromkeys(update_fields)))
        return user

    if email_claim and not User.objects.filter(email__iexact=email_claim).exists():
        email = email_claim
    elif phone:
        email = _synthetic_email_for_phone(phone)
        if User.objects.filter(email__iexact=email).exists():
            email = _synthetic_email_for_uid(firebase_uid)
    else:
        email = _synthetic_email_for_uid(firebase_uid)

    first_name, last_name = _split_display_name(display_name)
    user = User(
        email=email,
        phone=phone,
        firebase_uid=firebase_uid,
        account_type=User.AccountType.CONSUMER,
        first_name=first_name,
        last_name=last_name,
    )
    user.set_unusable_password()
    user.save()
    return user
