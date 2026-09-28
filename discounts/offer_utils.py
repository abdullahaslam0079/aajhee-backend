from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.db.models import Case, DecimalField, F, Max, Prefetch, Q, When
from django.utils import timezone

from .models import Offer, OfferBranchStats, OfferRedemption, OfferScan, Product


def active_offer_q(now=None, prefix=""):
    now = now or timezone.now()
    field = f"{prefix}__" if prefix else ""

    return Q(**{f"{field}is_enabled": True}) & (
        Q(**{f"{field}is_time_limited": False})
        | (
            Q(**{f"{field}is_time_limited": True})
            & (
                Q(**{f"{field}starts_at__isnull": True})
                | Q(**{f"{field}starts_at__lte": now})
            )
            & (
                Q(**{f"{field}ends_at__isnull": True})
                | Q(**{f"{field}ends_at__gte": now})
            )
        )
    )


def active_product_q(prefix="products"):
    """Enabled + available catalog products linked to a branch."""
    field = f"{prefix}__" if prefix else ""
    return Q(**{f"{field}is_enabled": True, f"{field}is_available": True})


def filter_active_offers(queryset, now=None):
    return queryset.filter(active_offer_q(now))


def period_start(limit_type: str, now=None) -> datetime | None:
    now = now or timezone.now()
    if limit_type.endswith("_week"):
        start = now - timedelta(days=now.weekday())
        return start.replace(hour=0, minute=0, second=0, microsecond=0)
    if limit_type.endswith("_month"):
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return None


def period_end(limit_type: str, now=None) -> datetime | None:
    now = now or timezone.now()
    if limit_type.endswith("_week"):
        return period_start(limit_type, now) + timedelta(days=7)
    if limit_type.endswith("_month"):
        if now.month == 12:
            return now.replace(
                year=now.year + 1,
                month=1,
                day=1,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
        return now.replace(
            month=now.month + 1,
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
    return None


def get_max_uses_for_offer(offer: Offer) -> int:
    limit_type = offer.usage_limit_type
    if limit_type in {
        Offer.UsageLimitType.ONE_TIME,
        Offer.UsageLimitType.ONCE_PER_WEEK,
        Offer.UsageLimitType.ONCE_PER_MONTH,
    }:
        return 1
    return max(offer.usage_limit_count, 1)


def get_user_redemption_count(user, offer, limit_type, now=None) -> int:
    queryset = OfferRedemption.objects.filter(user=user, offer=offer)
    period = period_start(limit_type, now)
    if period:
        queryset = queryset.filter(redeemed_at__gte=period)
    return queryset.count()


@dataclass
class UserOfferUsageStatus:
    redemption_count: int
    remaining_uses: int
    max_uses: int
    is_available_for_user: bool
    last_redeemed_at: datetime | None
    period_resets_at: datetime | None
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_redemption_count": self.redemption_count,
            "user_remaining_uses": self.remaining_uses,
            "max_uses": self.max_uses,
            "is_available_for_user": self.is_available_for_user,
            "last_redeemed_at": self.last_redeemed_at,
            "period_resets_at": self.period_resets_at,
            "message": self.message,
        }


def _redemptions_in_period(
    redemptions: list[OfferRedemption], limit_type: str, now=None
) -> list[OfferRedemption]:
    period = period_start(limit_type, now)
    if not period:
        return redemptions
    return [redemption for redemption in redemptions if redemption.redeemed_at >= period]


def get_user_offer_usage_status(
    user, offer: Offer, *, now=None, offer_active: bool | None = None
) -> UserOfferUsageStatus:
    now = now or timezone.now()
    limit_type = offer.usage_limit_type
    max_uses = get_max_uses_for_offer(offer)
    redemptions = list(
        OfferRedemption.objects.filter(user=user, offer=offer).order_by("-redeemed_at")
    )
    period_redemptions = _redemptions_in_period(redemptions, limit_type, now)
    used = len(period_redemptions)
    remaining = max(0, max_uses - used)
    is_active = offer.is_active if offer_active is None else offer_active
    last_redeemed_at = period_redemptions[0].redeemed_at if period_redemptions else None
    period_resets_at = period_end(limit_type, now) if limit_type.endswith(("_week", "_month")) else None

    if not is_active:
        message = "This offer is not currently active."
        is_available = False
    elif remaining <= 0:
        is_available = False
        message = _limit_reached_message(offer, limit_type, max_uses)
    else:
        is_available = True
        message = ""

    return UserOfferUsageStatus(
        redemption_count=used,
        remaining_uses=remaining,
        max_uses=max_uses,
        is_available_for_user=is_available,
        last_redeemed_at=last_redeemed_at,
        period_resets_at=period_resets_at,
        message=message,
    )


def build_user_redemption_map(user, offer_ids: list[int], now=None) -> dict[int, UserOfferUsageStatus]:
    if not offer_ids:
        return {}

    now = now or timezone.now()
    redemptions = (
        OfferRedemption.objects.filter(user=user, offer_id__in=offer_ids)
        .select_related("offer")
        .order_by("offer_id", "-redeemed_at")
    )
    by_offer: dict[int, list[OfferRedemption]] = defaultdict(list)
    for redemption in redemptions:
        by_offer[redemption.offer_id].append(redemption)

    offers = Offer.objects.in_bulk(offer_ids)
    result: dict[int, UserOfferUsageStatus] = {}
    for offer_id in offer_ids:
        offer = offers.get(offer_id)
        if offer is None:
            continue
        limit_type = offer.usage_limit_type
        max_uses = get_max_uses_for_offer(offer)
        offer_redemptions = by_offer.get(offer_id, [])
        period_redemptions = _redemptions_in_period(offer_redemptions, limit_type, now)
        used = len(period_redemptions)
        remaining = max(0, max_uses - used)
        last_redeemed_at = (
            period_redemptions[0].redeemed_at if period_redemptions else None
        )
        period_resets_at = (
            period_end(limit_type, now) if limit_type.endswith(("_week", "_month")) else None
        )
        if not offer.is_active:
            message = "This offer is not currently active."
            is_available = False
        elif remaining <= 0:
            is_available = False
            message = _limit_reached_message(offer, limit_type, max_uses)
        else:
            is_available = True
            message = ""

        result[offer_id] = UserOfferUsageStatus(
            redemption_count=used,
            remaining_uses=remaining,
            max_uses=max_uses,
            is_available_for_user=is_available,
            last_redeemed_at=last_redeemed_at,
            period_resets_at=period_resets_at,
            message=message,
        )
    return result


def _limit_reached_message(offer: Offer, limit_type: str, max_uses: int) -> str:
    if limit_type == Offer.UsageLimitType.ONE_TIME:
        return "You have already used this offer."
    if limit_type == Offer.UsageLimitType.ONCE_PER_WEEK:
        return "You can use this offer once per week. Try again next week."
    if limit_type == Offer.UsageLimitType.ONCE_PER_MONTH:
        return "You can use this offer once per month. Try again next month."
    if limit_type == Offer.UsageLimitType.N_TIMES_PER_WEEK:
        return f"You have reached the limit of {max_uses} uses per week for this offer."
    if limit_type == Offer.UsageLimitType.N_TIMES_PER_MONTH:
        return f"You have reached the limit of {max_uses} uses per month for this offer."
    if limit_type == Offer.UsageLimitType.N_TIMES_TOTAL:
        return f"You have reached the maximum of {max_uses} uses for this offer."
    return "You have reached the usage limit for this offer."


def can_user_redeem_offer(user, offer, branch):
    if offer.redemption_mode != Offer.RedemptionMode.SCANNABLE:
        return False, "This offer is view-only and cannot be redeemed in the app."

    if not offer.is_active:
        return False, "This offer is not currently active."

    if not offer.branches.filter(pk=branch.pk).exists():
        return False, "This offer is not available at the selected branch."

    if branch.business_id != offer.business_id:
        return False, "This branch does not belong to the offer business."

    usage = get_user_offer_usage_status(user, offer)
    if not usage.is_available_for_user:
        return False, usage.message or "You have reached the usage limit for this offer."

    return True, ""


def payment_record_fields(payment) -> dict:
    return {
        "bill_amount": payment.bill_amount,
        "original_amount": payment.original_amount,
        "discount_amount": payment.discount_amount,
        "amount_to_pay": payment.amount_to_pay,
    }


def increment_branch_stat(offer, branch, *, scan=False, avail=False):
    stats, _ = OfferBranchStats.objects.get_or_create(offer=offer, branch=branch)
    if scan:
        OfferBranchStats.objects.filter(pk=stats.pk).update(
            scan_count=F("scan_count") + 1
        )
    if avail:
        OfferBranchStats.objects.filter(pk=stats.pk).update(
            avail_count=F("avail_count") + 1
        )


def record_offer_redemption(user, offer, branch, payment, *, record_scan=False):
    locked_offer = Offer.objects.select_for_update().get(pk=offer.pk)
    can_redeem, message = can_user_redeem_offer(user, locked_offer, branch)
    if not can_redeem:
        return None, message

    fields = payment_record_fields(payment)
    if record_scan:
        OfferScan.objects.create(
            offer=locked_offer, branch=branch, user=user, **fields
        )
        increment_branch_stat(locked_offer, branch, scan=True)
    OfferRedemption.objects.create(
        offer=locked_offer, branch=branch, user=user, **fields
    )
    increment_branch_stat(locked_offer, branch, avail=True)
    return get_user_offer_usage_status(user, locked_offer), ""


def get_highest_discount_active_offer(branch, now=None):
    now = now or timezone.now()
    active_offers = [
        offer
        for offer in branch.offers.all()
        if offer.is_enabled
        and (
            not offer.is_time_limited
            or (
                (offer.starts_at is None or offer.starts_at <= now)
                and (offer.ends_at is None or offer.ends_at >= now)
            )
        )
    ]
    if not active_offers:
        return None
    return max(active_offers, key=lambda offer: offer.discount_percent)


def get_highest_discount_active_product(branch):
    """Best enabled product discount for map/store highlights."""
    products = [
        product
        for product in branch.products.all()
        if product.is_enabled and product.is_available
    ]
    if not products:
        return None

    def sort_key(product: Product):
        percent = product.discount_percent
        if percent is None:
            percent = product.effective_discount_percent
        return percent or 0

    return max(products, key=sort_key)


def build_media_url(request, file_field) -> str | None:
    if not file_field:
        return None
    url = file_field.url
    # Absolute URLs (S3/R2/CDN) must not be re-hosted under the API origin.
    if url.startswith(("http://", "https://")):
        return url
    if request:
        return request.build_absolute_uri(url)
    return url


def build_offer_image_urls(offer: Offer, request) -> list[str]:
    urls: list[str] = []
    for gallery_image in offer.gallery_images.all():
        if gallery_image.image:
            url = build_media_url(request, gallery_image.image)
            if url:
                urls.append(url)
                continue
        source = (gallery_image.source_url or "").strip()
        if source:
            urls.append(source)
    if not urls:
        legacy = build_media_url(request, offer.image)
        if legacy:
            urls.append(legacy)
    return urls


def annotate_branch_highlights(queryset, now=None):
    """
    Annotate map/store highlight discount from active offers and/or products.

    Commerce-first: branches with catalog products must appear even when no
    legacy Offer rows exist.
    """
    now = now or timezone.now()
    percent_field = DecimalField(max_digits=5, decimal_places=2)
    return queryset.annotate(
        offer_highest_discount_percent=Max(
            "offers__discount_percent",
            filter=active_offer_q(now, prefix="offers"),
        ),
        product_highest_discount_percent=Max(
            "products__discount_percent",
            filter=active_product_q(prefix="products"),
        ),
        highest_discount_percent=Case(
            When(
                offer_highest_discount_percent__isnull=True,
                then=F("product_highest_discount_percent"),
            ),
            When(
                product_highest_discount_percent__isnull=True,
                then=F("offer_highest_discount_percent"),
            ),
            When(
                offer_highest_discount_percent__gte=F(
                    "product_highest_discount_percent"
                ),
                then=F("offer_highest_discount_percent"),
            ),
            default=F("product_highest_discount_percent"),
            output_field=percent_field,
        ),
    )


def prefetch_branch_offers(queryset, now=None):
    now = now or timezone.now()
    return queryset.prefetch_related(
        Prefetch(
            "offers",
            queryset=Offer.objects.filter(active_offer_q(now)).prefetch_related(
                "gallery_images"
            ),
        ),
        Prefetch(
            "products",
            queryset=Product.objects.filter(
                is_enabled=True, is_available=True
            ).prefetch_related("gallery_images"),
        ),
    )


def branch_highlight_queryset(queryset, now=None):
    now = now or timezone.now()
    return annotate_branch_highlights(
        prefetch_branch_offers(
            queryset.select_related("business", "business__category"),
            now,
        ),
        now,
    )


def map_visible_branches_q():
    """Branches that should appear on Discover/Stores (offers or products)."""
    return Q(highest_discount_percent__isnull=False) | active_product_q(
        prefix="products"
    )
