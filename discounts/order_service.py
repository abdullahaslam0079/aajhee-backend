from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .delivery_options import (
    compute_promised_by,
    get_or_create_fulfillment_settings,
    resolve_delivery_options,
)
from .geo_utils import get_or_create_city
from .location_utils import UserLocation
from .models import (
    Branch,
    Cart,
    CartItem,
    Order,
    OrderDeliverySnapshot,
    OrderItem,
    OrderPaymentProof,
    Product,
    ProductEngagementStats,
)
from .phone_utils import normalize_pakistani_mobile


def get_or_create_cart(user) -> Cart:
    cart, _ = Cart.objects.get_or_create(user=user)
    return cart


def _unit_prices(product: Product) -> tuple[Decimal, Decimal | None, Decimal]:
    base = product.base_price
    sale = product.sale_price if product.has_discount else None
    percent = product.effective_discount_percent if product.has_discount else Decimal("0.00")
    return base, sale, percent


def _line_total(product: Product, quantity: int) -> Decimal:
    return (product.effective_price * quantity).quantize(Decimal("0.01"))


PAYMENT_PROOF_METHODS = frozenset(
    {
        Order.PaymentMethod.BANK_TRANSFER,
        Order.PaymentMethod.STRIPE,
        Order.PaymentMethod.JAZZCASH,
    }
)


def requires_payment_proof(payment_method: str | None) -> bool:
    """True when the customer must upload a transaction screenshot."""
    return payment_method in PAYMENT_PROOF_METHODS


def initial_payment_status(payment_method: str, *, has_proof: bool) -> str:
    if requires_payment_proof(payment_method):
        return (
            Order.PaymentStatus.AWAITING_CONFIRMATION
            if has_proof
            else Order.PaymentStatus.UNPAID
        )
    return Order.PaymentStatus.UNPAID


def add_or_update_cart_item(
    cart: Cart,
    product: Product,
    quantity: int,
    branch: Branch | None = None,
) -> CartItem:
    if quantity < 1:
        raise ValidationError({"quantity": "Quantity must be at least 1."})
    if not product.is_enabled or not product.is_available:
        raise ValidationError({"product_id": "Product is not available."})
    if product.business_id != (branch.business_id if branch else product.business_id):
        raise ValidationError({"branch_id": "Branch does not belong to this product's business."})
    if branch and product.branches.exists() and not product.branches.filter(id=branch.id).exists():
        raise ValidationError({"branch_id": "Product is not available at this branch."})

    item, created = CartItem.objects.get_or_create(
        cart=cart,
        product=product,
        branch=branch,
        defaults={"quantity": quantity},
    )
    if not created:
        item.quantity = quantity
        item.save(update_fields=["quantity", "updated_at"])
    return item


def _build_delivery_address_text(
    *,
    base: str,
    house_number: str,
    landmark: str,
) -> str:
    parts: list[str] = []
    base_clean = (base or "").strip()
    house = (house_number or "").strip()
    mark = (landmark or "").strip()
    if base_clean:
        parts.append(base_clean)
    elif house:
        parts.append(f"House/Flat {house}")
    if house and base_clean and house not in base_clean:
        parts.append(f"House/Flat {house}")
    if mark:
        parts.append(f"Landmark: {mark}")
    return "\n".join(parts)


def _attach_payment_proof(order: Order, proof_file, note: str = "") -> OrderPaymentProof:
    if hasattr(proof_file, "read"):
        content = proof_file.read()
        name = getattr(proof_file, "name", "receipt.jpg") or "receipt.jpg"
        stored = ContentFile(content, name=name.split("/")[-1])
    else:
        raise ValidationError({"payment_proof": "Invalid payment receipt file."})
    return OrderPaymentProof.objects.create(order=order, file=stored, note=note or "")


@transaction.atomic
def place_orders_from_cart(
    *,
    user,
    cart: Cart,
    groups: list[dict],
    location: UserLocation | None,
    customer_phone: str | None = None,
    proof_files: dict[int, object] | None = None,
) -> list[Order]:
    """
    groups: [
      {
        "branch_id": int,
        "item_ids": [cart_item_id, ...],
        "fulfillment_type": str,
        "payment_method": str,
        "customer_notes": str,
        "delivery_address_text": str,
        "delivery_house_number": str,
        "delivery_landmark": str,
        "customer_phone": str (optional per-group override),
      },
      ...
    ]
    proof_files: optional map of group index -> uploaded file for transfer/wallet.
    """
    if not groups:
        raise ValidationError({"groups": "At least one checkout group is required."})

    phone = normalize_pakistani_mobile(
        customer_phone or (groups[0].get("customer_phone") if groups else None) or user.phone
    )
    # Persist on profile for next checkout (contact number, not auth re-bind).
    if user.phone != phone:
        conflict = type(user).objects.filter(phone=phone).exclude(pk=user.pk).exists()
        if conflict:
            raise ValidationError(
                {"customer_phone": "This mobile number is already used by another account."}
            )
        user.phone = phone
        user.save(update_fields=["phone"])

    cart_items = {
        item.id: item
        for item in cart.items.select_related("product", "branch", "product__business").all()
    }
    orders: list[Order] = []
    used_item_ids: set[int] = set()
    proof_files = proof_files or {}

    for index, group in enumerate(groups):
        branch_id = group.get("branch_id")
        item_ids = group.get("item_ids") or []
        fulfillment_type = group.get("fulfillment_type")
        payment_method = group.get("payment_method")
        if not branch_id or not item_ids:
            raise ValidationError({"groups": "Each group needs branch_id and item_ids."})
        if fulfillment_type not in Order.FulfillmentType.values:
            raise ValidationError({"fulfillment_type": "Invalid fulfillment type."})
        if payment_method not in Order.PaymentMethod.values:
            raise ValidationError({"payment_method": "Invalid payment method."})

        try:
            branch = Branch.objects.select_related(
                "business", "city_ref", "business__primary_city", "business__primary_country"
            ).get(pk=branch_id)
        except Branch.DoesNotExist as exc:
            raise ValidationError({"branch_id": "Branch not found."}) from exc

        settings = get_or_create_fulfillment_settings(branch)
        options = {
            opt.fulfillment_type: opt
            for opt in resolve_delivery_options(branch, location)
        }
        option = options.get(fulfillment_type)
        if option is None or not option.available:
            raise ValidationError(
                {
                    "fulfillment_type": option.reason
                    if option
                    else "Fulfillment type not available."
                }
            )

        # Payment method guards
        if payment_method == Order.PaymentMethod.CASH_ON_PICKUP:
            if fulfillment_type != Order.FulfillmentType.PICKUP or not settings.cash_on_pickup_enabled:
                raise ValidationError({"payment_method": "Cash on pickup is not available."})
        elif payment_method == Order.PaymentMethod.CASH_ON_DELIVERY:
            if fulfillment_type == Order.FulfillmentType.PICKUP or not settings.cash_on_delivery_enabled:
                raise ValidationError({"payment_method": "Cash on delivery is not available."})
        elif payment_method == Order.PaymentMethod.BANK_TRANSFER:
            if not settings.bank_transfer_enabled:
                raise ValidationError({"payment_method": "Bank transfer is not enabled."})
        elif payment_method == Order.PaymentMethod.STRIPE:
            if not settings.stripe_enabled:
                raise ValidationError({"payment_method": "Card / Stripe payment is not enabled."})
        elif payment_method == Order.PaymentMethod.JAZZCASH:
            if not settings.jazzcash_enabled:
                raise ValidationError({"payment_method": "Mobile wallet is not enabled."})

        needs_proof = requires_payment_proof(payment_method)
        proof_file = proof_files.get(index)
        if needs_proof and proof_file is None:
            raise ValidationError(
                {
                    "payment_proof": (
                        "Upload a payment receipt before placing this order "
                        "(bank transfer or mobile wallet)."
                    )
                }
            )

        is_delivery = fulfillment_type in (
            Order.FulfillmentType.LOCAL_SAME_DAY,
            Order.FulfillmentType.NATIONWIDE,
        )
        house_number = (group.get("delivery_house_number") or "").strip()
        landmark = (group.get("delivery_landmark") or "").strip()
        address_text = (group.get("delivery_address_text") or "").strip()
        if is_delivery:
            if not address_text and not house_number:
                raise ValidationError(
                    {"delivery_address_text": "Delivery address is required."}
                )
            if not house_number:
                raise ValidationError(
                    {"delivery_house_number": "House / flat number is required for delivery."}
                )
            address_text = _build_delivery_address_text(
                base=address_text,
                house_number=house_number,
                landmark=landmark,
            )
        else:
            address_text = ""
            house_number = ""
            landmark = ""

        lines: list[CartItem] = []
        for item_id in item_ids:
            if item_id in used_item_ids:
                raise ValidationError({"item_ids": f"Cart item {item_id} used twice."})
            item = cart_items.get(item_id)
            if item is None:
                raise ValidationError({"item_ids": f"Cart item {item_id} not found."})
            if item.product.business_id != branch.business_id:
                raise ValidationError(
                    {"item_ids": "All items in a group must belong to the branch business."}
                )
            lines.append(item)
            used_item_ids.add(item_id)

        # Server-authoritative totals
        subtotal = Decimal("0.00")
        for item in lines:
            subtotal += _line_total(item.product, item.quantity)
        delivery_fee = option.fee
        total = (subtotal + delivery_fee).quantize(Decimal("0.01"))

        cancel_allowed = (
            settings.customer_cancel_policy
            == settings.CustomerCancelPolicy.WINDOW_MINUTES
        )
        cancel_until = None
        if cancel_allowed:
            cancel_until = timezone.now() + timedelta(
                minutes=settings.customer_cancel_window_minutes
            )

        delivery_city = None
        if location and location.city:
            delivery_city = get_or_create_city(location.city)

        payment_status = initial_payment_status(payment_method, has_proof=bool(proof_file))

        order = Order.objects.create(
            user=user,
            business=branch.business,
            branch=branch,
            status=Order.Status.PENDING,
            payment_status=payment_status,
            fulfillment_type=fulfillment_type,
            payment_method=payment_method,
            subtotal=subtotal,
            delivery_fee=delivery_fee,
            total=total,
            delivery_address_text=address_text,
            delivery_house_number=house_number,
            delivery_landmark=landmark,
            delivery_city=delivery_city,
            customer_phone=phone,
            customer_notes=group.get("customer_notes") or "",
            customer_cancel_allowed=cancel_allowed,
            customer_cancel_until=cancel_until,
        )

        for item in lines:
            base, sale, percent = _unit_prices(item.product)
            OrderItem.objects.create(
                order=order,
                product=item.product,
                product_name=item.product.name,
                unit_base_price=base,
                unit_sale_price=sale,
                unit_discount_percent=percent,
                quantity=item.quantity,
                line_total=_line_total(item.product, item.quantity),
            )
            stats, _ = ProductEngagementStats.objects.get_or_create(product=item.product)
            ProductEngagementStats.objects.filter(pk=stats.pk).update(
                order_count=stats.order_count + item.quantity
            )

        promised_by = compute_promised_by(option.max_delivery_hours)
        OrderDeliverySnapshot.objects.create(
            order=order,
            fulfillment_type=fulfillment_type,
            delivery_fee=delivery_fee,
            max_delivery_hours=option.max_delivery_hours,
            promised_by=promised_by,
            branch_city_id=branch.city_ref_id,
            branch_city_name=branch.city,
            customer_city_id=location.city_id if location else None,
            customer_city_name=location.city if location else "",
            pickup_radius_km=settings.pickup_radius_km,
            settings_json={
                "same_day_enabled": settings.same_day_enabled,
                "same_day_fee": str(settings.same_day_fee),
                "nationwide_fee": str(settings.nationwide_delivery_fee),
                "same_day_max_delivery_hours": settings.same_day_max_delivery_hours,
                "nationwide_max_delivery_hours": settings.nationwide_max_delivery_hours,
                "customer_cancel_policy": settings.customer_cancel_policy,
                "customer_cancel_window_minutes": settings.customer_cancel_window_minutes,
                "bank_transfer_enabled": settings.bank_transfer_enabled,
                "stripe_enabled": settings.stripe_enabled,
                "jazzcash_enabled": settings.jazzcash_enabled,
            },
        )

        if proof_file is not None:
            _attach_payment_proof(order, proof_file)
            # Proof already submitted at place — treat like payment_submitted for merchant review.
            order.status = Order.Status.PENDING
            order.payment_status = Order.PaymentStatus.AWAITING_CONFIRMATION
            order.save(update_fields=["status", "payment_status", "updated_at"])

        orders.append(order)

    CartItem.objects.filter(id__in=used_item_ids).delete()
    return orders


def customer_can_cancel(order: Order) -> bool:
    """Customers may cancel only while the order is still pending."""
    if order.status != Order.Status.PENDING:
        return False
    if not order.customer_cancel_allowed:
        return False
    if order.customer_cancel_until and timezone.now() > order.customer_cancel_until:
        return False
    return True


def cancel_order(order: Order, *, by: str, reason: str = "") -> Order:
    if order.status in (Order.Status.CANCELLED, Order.Status.COMPLETED):
        raise ValidationError({"status": "Order cannot be cancelled."})
    if by == Order.CancelledBy.CUSTOMER and not customer_can_cancel(order):
        raise ValidationError(
            {
                "status": (
                    "You can only cancel while the order is pending. "
                    "Once the shop accepts it, contact the shop instead."
                )
            }
        )
    if by == Order.CancelledBy.CUSTOMER and not (reason or "").strip():
        raise ValidationError({"reason": "Please provide a cancellation reason."})
    previous_status = order.status
    order.status = Order.Status.CANCELLED
    order.cancelled_by = by
    order.cancel_reason = reason
    order.cancelled_at = timezone.now()
    order.save(
        update_fields=[
            "status",
            "cancelled_by",
            "cancel_reason",
            "cancelled_at",
            "updated_at",
        ]
    )
    if previous_status != Order.Status.CANCELLED:
        from .notification_utils import notify_customer_order_status

        notify_customer_order_status(order)
    return order


BUSINESS_STATUS_TRANSITIONS: dict[str, set[str]] = {
    Order.Status.PENDING: {Order.Status.ACCEPTED, Order.Status.CANCELLED},
    Order.Status.ACCEPTED: {
        Order.Status.AWAITING_PAYMENT,
        Order.Status.PREPARING,
        Order.Status.READY_FOR_PICKUP,
        Order.Status.OUT_FOR_DELIVERY,
        Order.Status.CANCELLED,
    },
    Order.Status.AWAITING_PAYMENT: {
        Order.Status.PAYMENT_SUBMITTED,
        Order.Status.CANCELLED,
    },
    Order.Status.PAYMENT_SUBMITTED: {
        Order.Status.PAID_CONFIRMED,
        Order.Status.AWAITING_PAYMENT,
        Order.Status.CANCELLED,
    },
    Order.Status.PAID_CONFIRMED: {
        Order.Status.PREPARING,
        Order.Status.READY_FOR_PICKUP,
        Order.Status.OUT_FOR_DELIVERY,
        Order.Status.CANCELLED,
    },
    Order.Status.PREPARING: {
        Order.Status.READY_FOR_PICKUP,
        Order.Status.OUT_FOR_DELIVERY,
        Order.Status.CANCELLED,
    },
    Order.Status.READY_FOR_PICKUP: {Order.Status.COMPLETED, Order.Status.CANCELLED},
    Order.Status.OUT_FOR_DELIVERY: {Order.Status.COMPLETED, Order.Status.CANCELLED},
}


def is_pickup_order(order: Order) -> bool:
    return order.fulfillment_type == Order.FulfillmentType.PICKUP


def is_delivery_order(order: Order) -> bool:
    return order.fulfillment_type in (
        Order.FulfillmentType.LOCAL_SAME_DAY,
        Order.FulfillmentType.NATIONWIDE,
    )


def allowed_business_transitions(order: Order) -> set[str]:
    """Status targets a merchant may apply, filtered by fulfillment + payment."""
    allowed = set(BUSINESS_STATUS_TRANSITIONS.get(order.status, set()))

    if order.status in (
        Order.Status.ACCEPTED,
        Order.Status.PAID_CONFIRMED,
        Order.Status.PREPARING,
    ):
        if is_pickup_order(order):
            allowed.discard(Order.Status.OUT_FOR_DELIVERY)
        else:
            allowed.discard(Order.Status.READY_FOR_PICKUP)

    if order.status == Order.Status.ACCEPTED:
        if requires_payment_proof(order.payment_method):
            # Proof was required at place; merchant confirms payment then fulfills.
            if order.payment_status != Order.PaymentStatus.PAID:
                allowed.discard(Order.Status.PREPARING)
                allowed.discard(Order.Status.READY_FOR_PICKUP)
                allowed.discard(Order.Status.OUT_FOR_DELIVERY)
            allowed.discard(Order.Status.AWAITING_PAYMENT)
        else:
            allowed.discard(Order.Status.AWAITING_PAYMENT)

    if order.status == Order.Status.AWAITING_PAYMENT:
        allowed.discard(Order.Status.PAYMENT_SUBMITTED)

    return allowed


def transition_order_status(order: Order, new_status: str, *, payment_method: str | None = None) -> Order:
    if new_status == Order.Status.CANCELLED:
        return cancel_order(order, by=Order.CancelledBy.BUSINESS)

    allowed = allowed_business_transitions(order)
    if new_status not in allowed:
        raise ValidationError(
            {"status": f"Cannot transition from {order.status} to {new_status}."}
        )

    if (
        order.status == Order.Status.ACCEPTED
        and new_status in (
            Order.Status.PREPARING,
            Order.Status.READY_FOR_PICKUP,
            Order.Status.OUT_FOR_DELIVERY,
        )
        and requires_payment_proof(order.payment_method)
        and order.payment_status != Order.PaymentStatus.PAID
    ):
        raise ValidationError(
            {"status": "Confirm payment before fulfilling this order."}
        )

    if order.status == Order.Status.PENDING and new_status == Order.Status.ACCEPTED:
        order.status = Order.Status.ACCEPTED
        order.customer_cancel_allowed = False
        # Cash stays unpaid until delivered; proof methods wait for confirmation.
        if requires_payment_proof(order.payment_method):
            if order.payment_status == Order.PaymentStatus.UNPAID:
                order.payment_status = Order.PaymentStatus.AWAITING_CONFIRMATION
                order.status = Order.Status.AWAITING_PAYMENT
        order.save(
            update_fields=[
                "status",
                "payment_status",
                "customer_cancel_allowed",
                "updated_at",
            ]
        )
        from .notification_utils import notify_customer_order_status

        notify_customer_order_status(order)
        return order

    order.status = new_status
    if new_status == Order.Status.COMPLETED:
        # COD / cash becomes paid on delivery / pickup completion.
        if not requires_payment_proof(order.payment_method):
            order.payment_status = Order.PaymentStatus.PAID
            order.save(update_fields=["status", "payment_status", "updated_at"])
        else:
            order.save(update_fields=["status", "updated_at"])
    else:
        order.save(update_fields=["status", "updated_at"])

    from .notification_utils import notify_customer_order_status

    notify_customer_order_status(order)
    return order
