"""Shared helpers for scaled store catalog endpoints (header / home / deals / category)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response

from .models import Branch, Business
from .offer_utils import build_media_url
from .serializers_commerce import BranchContactSerializer, serialize_delivery_options
from .visibility import resolve_business_visibility


DEFAULT_PREVIEW_LIMIT = 8


@dataclass
class StoreContext:
    business: Business
    branch: Branch | None
    channels: Any
    location: Any


def resolve_store_context(
    view,
    *,
    business_id: int | None = None,
    branch_id: int | None = None,
) -> StoreContext | Response:
    """
    Resolve business/branch and visibility for store endpoints.

    Returns StoreContext or a ready-to-return 404 Response.
    """
    location = view.get_user_location()
    if branch_id:
        branch = get_object_or_404(
            Branch.objects.select_related(
                "business", "business__engagement_stats", "engagement_stats"
            ).prefetch_related("contacts"),
            pk=branch_id,
        )
        business = branch.business
    else:
        business = get_object_or_404(
            Business.objects.select_related("engagement_stats"), pk=business_id
        )
        branch = (
            business.branches.select_related("engagement_stats")
            .prefetch_related("contacts")
            .order_by("id")
            .first()
        )

    channels = resolve_business_visibility(business, location)
    if not (channels.show_online or channels.show_instore):
        return Response(
            {"detail": "Business is not available in your area."},
            status=status.HTTP_404_NOT_FOUND,
        )

    return StoreContext(
        business=business,
        branch=branch,
        channels=channels,
        location=location,
    )


def store_products_qs(view, business: Business, branch: Branch | None):
    """Enabled/available products for a business, optionally scoped to a branch."""
    qs = view.base_product_qs().filter(business=business)
    if branch:
        qs = qs.annotate(_branch_count=Count("branches")).filter(
            Q(_branch_count=0) | Q(branches=branch)
        ).distinct()
    return qs


def store_deals_qs(qs):
    """Products currently on sale (sale_price < base_price)."""
    return (
        qs.filter(sale_price__isnull=False)
        .extra(where=["sale_price < base_price"])
        .order_by("-discount_percent", "name")
    )


def product_serializer_context(request, branch: Branch | None) -> dict:
    ctx = {"request": request}
    if branch:
        ctx["branch_id"] = branch.id
    return ctx


def build_store_header_payload(
    request,
    *,
    ctx: StoreContext,
) -> dict:
    from .review_service import branch_rating_payload

    business = ctx.business
    branch = ctx.branch
    channels = ctx.channels
    location = ctx.location

    contacts = []
    if branch:
        contacts = BranchContactSerializer(branch.contacts.all(), many=True).data

    delivery_options = []
    if branch and location:
        delivery_options = serialize_delivery_options(branch, location)

    branch_ratings = branch_rating_payload(branch) if branch else {
        "rating_avg": "0.00",
        "rating_count": 0,
    }

    logo_url = build_media_url(request, business.logo)
    is_verified = (
        business.verification_status == Business.VerificationStatus.VERIFIED
    )

    return {
        "business": {
            "id": business.id,
            "name": business.name,
            "presence_mode": business.presence_mode,
            "online_coverage": business.online_coverage,
            "show_online": channels.show_online,
            "show_instore": channels.show_instore,
            "is_verified": is_verified,
            "verification_status": business.verification_status,
            "logo_url": logo_url,
            "business_hours": business.business_hours or {},
            "rating_avg": str(
                getattr(
                    getattr(business, "engagement_stats", None),
                    "rating_avg",
                    "0.00",
                )
                or "0.00"
            ),
            "rating_count": int(
                getattr(
                    getattr(business, "engagement_stats", None),
                    "rating_count",
                    0,
                )
                or 0
            ),
        },
        "branch": (
            {
                "id": branch.id,
                "name": branch.name,
                "city": branch.city,
                "latitude": branch.latitude,
                "longitude": branch.longitude,
                "formatted_address": branch.formatted_address,
                "rating_avg": branch_ratings["rating_avg"],
                "rating_count": branch_ratings["rating_count"],
            }
            if branch
            else None
        ),
        "contacts": contacts,
        "delivery_options": delivery_options,
    }


def build_store_home_payload(
    request,
    *,
    view,
    ctx: StoreContext,
    preview_limit: int = DEFAULT_PREVIEW_LIMIT,
) -> dict:
    from .serializers_commerce import ProductSerializer

    qs = store_products_qs(view, ctx.business, ctx.branch)
    ser_ctx = product_serializer_context(request, ctx.branch)

    deals_qs = store_deals_qs(qs)
    deals_count = deals_qs.count()
    deals_preview = list(deals_qs[:preview_limit])

    # Category shelves include discounted products (unlike legacy /catalog).
    by_category: dict[int, dict] = {}
    for product in qs.order_by("category__name", "name"):
        cat_id = product.category_id
        if cat_id is None:
            continue
        bucket = by_category.get(cat_id)
        if bucket is None:
            bucket = {
                "category_id": cat_id,
                "category_name": product.category.name,
                "products": [],
            }
            by_category[cat_id] = bucket
        bucket["products"].append(product)

    categories = []
    for bucket in by_category.values():
        products = bucket["products"]
        categories.append(
            {
                "category_id": bucket["category_id"],
                "category_name": bucket["category_name"],
                "product_count": len(products),
                "preview": ProductSerializer(
                    products[:preview_limit], many=True, context=ser_ctx
                ).data,
            }
        )

    return {
        "deals": {
            "count": deals_count,
            "preview": ProductSerializer(
                deals_preview, many=True, context=ser_ctx
            ).data,
        },
        "categories": categories,
    }


def parse_preview_limit(request, *, default: int = DEFAULT_PREVIEW_LIMIT) -> int:
    raw = request.query_params.get("preview_limit")
    if raw is None:
        return default
    try:
        return min(max(int(raw), 1), 24)
    except (TypeError, ValueError):
        return default
