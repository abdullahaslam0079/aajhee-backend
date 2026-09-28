from __future__ import annotations

import logging

from .fcm import send_fcm_to_tokens
from .models import BusinessLike, DeviceToken, Notification, Offer, Order, UserPreferences

logger = logging.getLogger(__name__)


def _notifications_enabled_for(user_id: int) -> bool:
    prefs = UserPreferences.objects.filter(user_id=user_id).only("notifications_enabled").first()
    if prefs is None:
        return True
    return prefs.notifications_enabled


def create_and_push_notification(
    *,
    user_id: int,
    type: str,
    title: str,
    body: str,
    data: dict | None = None,
) -> Notification:
    notification = Notification.objects.create(
        user_id=user_id,
        type=type,
        title=title,
        body=body,
        data=data or {},
    )

    if not _notifications_enabled_for(user_id):
        return notification

    tokens = list(
        DeviceToken.objects.filter(user_id=user_id).values_list("token", flat=True)
    )
    if tokens:
        send_fcm_to_tokens(
            tokens=tokens,
            title=title,
            body=body,
            data={
                **(data or {}),
                "notification_id": notification.id,
                "type": type,
            },
        )
    return notification


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
    try:
        return create_and_push_notification(
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
        return None


def notify_business_payment_proof(order: Order) -> Notification | None:
    """Notify the business owner that a payment proof was uploaded."""
    owner_id = getattr(order.business, "owner_id", None)
    if not owner_id:
        return None
    try:
        return create_and_push_notification(
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
        return None


def notify_customer_order_status(order: Order) -> Notification | None:
    """Notify the customer when their order status changes."""
    user_id = getattr(order, "user_id", None)
    if not user_id:
        return None
    try:
        status_label = order.get_status_display()
        store = order.business.name if order.business_id else "store"
        return create_and_push_notification(
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
    except Exception:
        logger.exception(
            "Failed to notify customer about order status for %s", order.public_id
        )
        return None
