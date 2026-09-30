from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction

from .async_notify import run_after_commit
from .fcm import normalize_fcm_data, send_fcm_to_tokens
from .models import BusinessLike, DeviceToken, Notification, Offer, Order, UserPreferences
from .whatsapp import send_twilio_whatsapp

logger = logging.getLogger(__name__)


def _notifications_enabled_for(user_id: int) -> bool:
    prefs = UserPreferences.objects.filter(user_id=user_id).only("notifications_enabled").first()
    if prefs is None:
        return True
    return prefs.notifications_enabled


def _send_fcm_after_commit(
    *,
    tokens: list[str],
    title: str,
    body: str,
    data: dict,
    apns_tokens: list[str] | None = None,
) -> None:
    """Send FCM on the request thread after commit; APNs fallback for iOS.

    Daemon background threads are unreliable on Render/Gunicorn (worker can
    recycle before the push finishes). FCM send_each is typically <500ms.
    """

    def _send() -> None:
        try:
            sent = send_fcm_to_tokens(
                tokens=tokens, title=title, body=body, data=data
            )
            if sent > 0:
                return
            # FCM failed or not configured — try native APNs for iOS devices.
            from .apns import send_apns_to_tokens

            apns = [t for t in (apns_tokens or []) if t]
            if not apns:
                logger.info(
                    "Push: FCM delivered 0 and no APNs tokens for fallback "
                    "(title=%r type=%s)",
                    title,
                    (data or {}).get("type"),
                )
                return
            apns_sent = send_apns_to_tokens(
                tokens=apns, title=title, body=body, data=data
            )
            if apns_sent < 1:
                logger.warning(
                    "Push: FCM and APNs both failed to deliver "
                    "(fcm_tokens=%s apns_tokens=%s title=%r)",
                    len(tokens),
                    len(apns),
                    title,
                )
        except Exception:
            logger.exception("FCM/APNs on_commit send failed")

    try:
        transaction.on_commit(_send)
    except Exception:
        logger.exception("FCM on_commit scheduling failed; sending inline")
        _send()


def create_and_push_notification(
    *,
    user_id: int,
    type: str,
    title: str,
    body: str,
    data: dict | None = None,
) -> Notification:
    payload = dict(data or {})
    # Canonical keys always present for clients (inbox JSON + FCM data).
    payload["type"] = type
    if "order_public_id" not in payload and payload.get("order_id"):
        order_id = str(payload["order_id"]).strip()
        if order_id and not order_id.isdigit():
            payload["order_public_id"] = order_id

    notification = Notification.objects.create(
        user_id=user_id,
        type=type,
        title=title,
        body=body,
        data=payload,
    )

    if not _notifications_enabled_for(user_id):
        logger.info(
            "FCM skipped: notifications_enabled=False for user_id=%s "
            "(inbox row %s still created)",
            user_id,
            notification.id,
        )
        return notification

    devices = list(DeviceToken.objects.filter(user_id=user_id))
    tokens = [d.token for d in devices if d.token]
    apns_tokens = [
        d.apns_token
        for d in devices
        if d.platform == DeviceToken.Platform.IOS and (d.apns_token or "").strip()
    ]
    if not tokens and not apns_tokens:
        logger.info(
            "Push skipped: no DeviceToken rows for user_id=%s "
            "(inbox row %s still created)",
            user_id,
            notification.id,
        )
        return notification

    fcm_data = normalize_fcm_data(
        {
            **payload,
            "notification_id": notification.id,
            "type": type,
        }
    )
    _send_fcm_after_commit(
        tokens=tokens,
        title=title,
        body=body,
        data=fcm_data,
        apns_tokens=apns_tokens,
    )
    return notification


def _order_email_lines(order: Order, intro: str) -> list[str]:
    customer_name = ""
    if order.user_id:
        full = f"{order.user.first_name or ''} {order.user.last_name or ''}".strip()
        customer_name = full or order.user.email or ""
    phone = order.customer_phone or getattr(order.user, "phone", None) or "—"
    lines = [
        intro,
        "",
        f"Order: #{str(order.public_id)[:8]}",
        f"Branch: {order.branch.name if order.branch_id else '—'}",
        f"Total: Rs {order.total}",
        f"Fulfillment: {order.get_fulfillment_type_display()}",
        f"Payment: {order.get_payment_method_display()} ({order.get_payment_status_display()})",
        f"Customer: {customer_name or '—'}",
        f"Phone: {phone}",
    ]
    if order.delivery_address_text:
        lines.append(f"Address: {order.delivery_address_text}")
    if order.delivery_landmark:
        lines.append(f"Landmark: {order.delivery_landmark}")
    if order.customer_notes:
        lines.append(f"Notes: {order.customer_notes}")
    lines.extend(["", "Open your Aajhee Business panel to accept or fulfill this order.", "", "— Aajhee"])
    return lines


def _send_business_order_email(order: Order, *, subject: str, intro: str) -> None:
    owner = getattr(order.business, "owner", None)
    email = getattr(owner, "email", None) if owner else None
    if not email:
        return
    try:
        send_mail(
            subject=subject,
            message="\n".join(_order_email_lines(order, intro)),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
            fail_silently=False,
        )
    except Exception:
        logger.exception(
            "Failed to email business %s about order %s", email, order.public_id
        )


def _send_business_order_email_by_id(order_id: int, *, subject: str, intro: str) -> None:
    order = (
        Order.objects.select_related("business", "business__owner", "branch", "user")
        .filter(pk=order_id)
        .first()
    )
    if order is None:
        return
    _send_business_order_email(order, subject=subject, intro=intro)


def _notify_business_whatsapp_by_id(order_id: int, *, event: str) -> None:
    order = (
        Order.objects.select_related("business", "branch")
        .filter(pk=order_id)
        .first()
    )
    if order is None:
        return
    notify_business_whatsapp_or_sms(order, event=event)


def notify_business_whatsapp_or_sms(order: Order, *, event: str) -> None:
    """
    Alert the merchant WhatsApp/notification number when configured.

    Set WHATSAPP_PROVIDER=twilio plus TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN /
    TWILIO_WHATSAPP_FROM to enable delivery. Without credentials this only logs.
    """
    business = order.business
    number = (getattr(business, "notification_whatsapp", None) or "").strip()
    if not number:
        return
    provider = (getattr(settings, "WHATSAPP_PROVIDER", None) or "").strip().lower()
    if not provider:
        logger.info(
            "WhatsApp/SMS notify skipped (no provider configured): "
            "number=%s event=%s order=%s",
            number,
            event,
            order.public_id,
        )
        return

    short_id = str(order.public_id)[:8]
    if event == "payment_proof":
        body = (
            f"Aajhee: payment proof for order #{short_id} "
            f"(Rs {order.total}). Open Business → Orders to review."
        )
    else:
        body = (
            f"Aajhee: new order #{short_id} · Rs {order.total} · "
            f"{order.get_fulfillment_type_display()}. Open Business → Orders."
        )

    if provider == "twilio":
        send_twilio_whatsapp(to=number, body=body)
        return

    logger.warning(
        "WhatsApp provider '%s' is not supported "
        "(number=%s event=%s order=%s). Use WHATSAPP_PROVIDER=twilio.",
        provider,
        number,
        event,
        order.public_id,
    )


def notify_favorited_business_new_offer(offer: Offer) -> int:
    """Create inbox (+ optional push) for users who favorited the offer's business.

    Returns the number of notifications created.
    """
    if not offer.is_enabled:
        return 0

    business = offer.business
    branch = offer.branches.order_by("id").first()
    if branch is None:
        branch = business.branches.order_by("id").first()

    likes = (
        BusinessLike.objects.filter(business=business)
        .select_related("user")
        .only("user_id")
    )
    user_ids = sorted({like.user_id for like in likes})
    if not user_ids:
        return 0

    title = f"New offer from {business.name}"
    body = offer.title
    data = {
        "type": Notification.NotificationType.FAVORITED_BUSINESS_NEW_OFFER,
        "offer_id": offer.id,
        "business_id": business.id,
        "branch_id": branch.id if branch else None,
        "route": "/business-store",
    }

    created = 0
    for user_id in user_ids:
        try:
            create_and_push_notification(
                user_id=user_id,
                type=Notification.NotificationType.FAVORITED_BUSINESS_NEW_OFFER,
                title=title,
                body=body,
                data=data,
            )
            created += 1
        except Exception:
            logger.exception(
                "Failed to notify user %s about offer %s", user_id, offer.id
            )
    return created


def notify_business_new_order(order: Order) -> Notification | None:
    """Notify the business owner that a customer placed an order."""
    owner_id = getattr(order.business, "owner_id", None)
    if not owner_id:
        return None
    notification = None
    try:
        notification = create_and_push_notification(
            user_id=owner_id,
            type=Notification.NotificationType.BUSINESS_NEW_ORDER,
            title="New order",
            body=(
                f"{order.branch.name}: Rs {order.total} · "
                f"{order.get_fulfillment_type_display()} · "
                f"{order.get_payment_method_display()}"
            ),
            data={
                "type": Notification.NotificationType.BUSINESS_NEW_ORDER,
                "order_public_id": str(order.public_id),
                "business_id": order.business_id,
                "branch_id": order.branch_id,
                "route": f"/business/orders/{order.public_id}",
            },
        )
    except Exception:
        logger.exception("Failed to notify business about order %s", order.public_id)

    run_after_commit(
        _send_business_order_email_by_id,
        order.id,
        subject=f"New Aajhee order · Rs {order.total}",
        intro="You have a new order on Aajhee.",
    )
    run_after_commit(_notify_business_whatsapp_by_id, order.id, event="new_order")
    return notification


def notify_business_payment_proof(order: Order) -> Notification | None:
    """Notify the business owner that a payment proof was uploaded."""
    owner_id = getattr(order.business, "owner_id", None)
    if not owner_id:
        return None
    notification = None
    try:
        notification = create_and_push_notification(
            user_id=owner_id,
            type=Notification.NotificationType.BUSINESS_PAYMENT_PROOF,
            title="Payment proof submitted",
            body=f"Order #{str(order.public_id)[:8]} · Rs {order.total} — review the receipt",
            data={
                "type": Notification.NotificationType.BUSINESS_PAYMENT_PROOF,
                "order_public_id": str(order.public_id),
                "business_id": order.business_id,
                "branch_id": order.branch_id,
                "route": f"/business/orders/{order.public_id}",
            },
        )
    except Exception:
        logger.exception(
            "Failed to notify business about payment proof for %s", order.public_id
        )

    run_after_commit(
        _send_business_order_email_by_id,
        order.id,
        subject=f"Payment proof · order #{str(order.public_id)[:8]}",
        intro="A customer submitted a payment proof for review.",
    )
    run_after_commit(_notify_business_whatsapp_by_id, order.id, event="payment_proof")
    return notification


def notify_customer_order_status(order: Order) -> Notification | None:
    """Notify the customer when their order status changes."""
    user_id = getattr(order, "user_id", None)
    if not user_id:
        return None
    try:
        status_label = order.get_status_display()
        store = order.business.name if order.business_id else "store"
        notification = create_and_push_notification(
            user_id=user_id,
            type=Notification.NotificationType.ORDER_STATUS_CHANGED,
            title=f"Order update · {status_label}",
            body=f"Your order from {store} is now: {status_label}.",
            data={
                "type": Notification.NotificationType.ORDER_STATUS_CHANGED,
                "order_public_id": str(order.public_id),
                "order_id": str(order.public_id),
                "status": order.status,
                "payment_status": order.payment_status,
                "business_id": order.business_id,
                "branch_id": order.branch_id,
                "route": f"/orders/{order.public_id}",
            },
        )
        if order.status == Order.Status.COMPLETED:
            notify_customer_rate_prompt(order)
        return notification
    except Exception:
        logger.exception(
            "Failed to notify customer about order status for %s", order.public_id
        )
        return None


def notify_customer_rate_prompt(order: Order) -> Notification | None:
    """Prompt the customer to rate products after delivery."""
    user_id = getattr(order, "user_id", None)
    if not user_id:
        return None
    store = order.business.name if order.business_id else "the store"
    try:
        return create_and_push_notification(
            user_id=user_id,
            type=Notification.NotificationType.ORDER_RATE_PROMPT,
            title="Rate your order",
            body=f"How was your order from {store}? Tap to leave a rating.",
            data={
                "type": Notification.NotificationType.ORDER_RATE_PROMPT,
                "order_public_id": str(order.public_id),
                "order_id": str(order.public_id),
                "business_id": order.business_id,
                "branch_id": order.branch_id,
                "route": f"/orders/{order.public_id}",
            },
        )
    except Exception:
        logger.exception(
            "Failed to send rate prompt for order %s", order.public_id
        )
        return None


def notify_business_new_review(review) -> Notification | None:
    """Notify the merchant when a customer leaves a product review."""
    owner_id = getattr(review.business, "owner_id", None)
    if not owner_id:
        return None
    try:
        stars = "★" * int(review.rating) + "☆" * (5 - int(review.rating))
        return create_and_push_notification(
            user_id=owner_id,
            type=Notification.NotificationType.BUSINESS_NEW_REVIEW,
            title="New product review",
            body=f"{stars} · {review.product.name}",
            data={
                "type": Notification.NotificationType.BUSINESS_NEW_REVIEW,
                "review_id": review.id,
                "product_id": review.product_id,
                "order_public_id": str(review.order.public_id),
                "business_id": review.business_id,
                "route": "/business/reviews",
            },
        )
    except Exception:
        logger.exception("Failed to notify business about review %s", review.id)
        return None


def notify_customer_review_reply(review) -> Notification | None:
    """Notify the customer when a merchant replies to their review."""
    user_id = getattr(review, "user_id", None)
    if not user_id:
        return None
    store = review.business.name if review.business_id else "the store"
    try:
        return create_and_push_notification(
            user_id=user_id,
            type=Notification.NotificationType.REVIEW_MERCHANT_REPLY,
            title=f"{store} replied to your review",
            body=(review.merchant_reply or "")[:160],
            data={
                "type": Notification.NotificationType.REVIEW_MERCHANT_REPLY,
                "review_id": review.id,
                "product_id": review.product_id,
                "business_id": review.business_id,
                "route": f"/products/{review.product_id}",
            },
        )
    except Exception:
        logger.exception(
            "Failed to notify customer about review reply %s", review.id
        )
        return None


def notify_merchant_verification(
    business, *, approved: bool, reason: str = ""
) -> None:
    """Email the merchant when admin approves or rejects verification."""
    owner = getattr(business, "owner", None)
    email = getattr(owner, "email", None) if owner else None
    if not email:
        return
    if approved:
        subject = f"Aajhee: {business.name} is verified"
        lines = [
            f"Good news — {business.name} has been verified on Aajhee.",
            "",
            "Your store can now appear to customers (unless paused).",
            "Open the Aajhee Business panel to manage products and orders.",
            "",
            "— Aajhee",
        ]
    else:
        subject = f"Aajhee: verification update for {business.name}"
        lines = [
            f"Your verification request for {business.name} was not approved.",
            "",
        ]
        if reason:
            lines.extend([f"Reason: {reason}", ""])
        lines.extend(
            [
                "Please update your business profile (phone, WhatsApp, CNIC, "
                "shop photo or Instagram) and wait for another review.",
                "",
                "— Aajhee",
            ]
        )
    try:
        send_mail(
            subject=subject,
            message="\n".join(lines),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
            fail_silently=False,
        )
    except Exception:
        logger.exception(
            "Failed to email merchant %s about verification for business %s",
            email,
            business.id,
        )
