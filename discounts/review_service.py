"""Verified-purchase product review rules and rating aggregates.

Marketplace model: every review is tied to the fulfillment branch.
- BranchEngagementStats: store-location rating shown on map/catalog cards
- ProductBranchEngagementStats: product stars inside a specific store
- ProductEngagementStats / BusinessEngagementStats: overall rollups
New branches start at zero and never inherit sibling location scores.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Avg, Count
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from .models import (
    Branch,
    BranchEngagementStats,
    Business,
    BusinessEngagementStats,
    Order,
    OrderItem,
    Product,
    ProductBranchEngagementStats,
    ProductEngagementStats,
    ProductReview,
    ProductReviewImage,
)

MAX_REVIEW_IMAGES = 5
EDIT_WINDOW_DAYS = 7
PUBLIC_STATUSES = (ProductReview.Status.PUBLISHED, ProductReview.Status.FLAGGED)


def ensure_product_engagement_stats(product: Product) -> ProductEngagementStats:
    stats, _ = ProductEngagementStats.objects.get_or_create(product=product)
    return stats


def ensure_business_engagement_stats(business: Business) -> BusinessEngagementStats:
    stats, _ = BusinessEngagementStats.objects.get_or_create(business=business)
    return stats


def ensure_branch_engagement_stats(branch: Branch) -> BranchEngagementStats:
    stats, _ = BranchEngagementStats.objects.get_or_create(branch=branch)
    return stats


def ensure_product_branch_engagement_stats(
    product: Product, branch: Branch
) -> ProductBranchEngagementStats:
    stats, _ = ProductBranchEngagementStats.objects.get_or_create(
        product=product, branch=branch
    )
    return stats


def _quantize_avg(avg, count: int) -> Decimal:
    if not count:
        return Decimal("0.00")
    return Decimal(str(avg or 0)).quantize(Decimal("0.01"))


def recompute_product_rating(product: Product | int) -> ProductEngagementStats:
    product_id = product if isinstance(product, int) else product.pk
    product_obj = (
        product if isinstance(product, Product) else Product.objects.get(pk=product_id)
    )
    stats = ensure_product_engagement_stats(product_obj)
    agg = ProductReview.objects.filter(
        product_id=product_id, status__in=PUBLIC_STATUSES
    ).aggregate(avg=Avg("rating"), count=Count("id"))
    count = int(agg["count"] or 0)
    avg = _quantize_avg(agg["avg"], count)
    ProductEngagementStats.objects.filter(pk=stats.pk).update(
        rating_avg=avg, rating_count=count
    )
    stats.refresh_from_db()
    return stats


def recompute_business_rating(business: Business | int) -> BusinessEngagementStats:
    business_id = business if isinstance(business, int) else business.pk
    business_obj = (
        business
        if isinstance(business, Business)
        else Business.objects.get(pk=business_id)
    )
    stats = ensure_business_engagement_stats(business_obj)
    agg = ProductReview.objects.filter(
        business_id=business_id, status__in=PUBLIC_STATUSES
    ).aggregate(avg=Avg("rating"), count=Count("id"))
    count = int(agg["count"] or 0)
    avg = _quantize_avg(agg["avg"], count)
    BusinessEngagementStats.objects.filter(pk=stats.pk).update(
        rating_avg=avg, rating_count=count
    )
    stats.refresh_from_db()
    return stats


def recompute_branch_rating(branch: Branch | int) -> BranchEngagementStats | None:
    branch_id = branch if isinstance(branch, int) else getattr(branch, "pk", None)
    if not branch_id:
        return None
    branch_obj = (
        branch if isinstance(branch, Branch) else Branch.objects.get(pk=branch_id)
    )
    stats = ensure_branch_engagement_stats(branch_obj)
    agg = ProductReview.objects.filter(
        branch_id=branch_id, status__in=PUBLIC_STATUSES
    ).aggregate(avg=Avg("rating"), count=Count("id"))
    count = int(agg["count"] or 0)
    avg = _quantize_avg(agg["avg"], count)
    BranchEngagementStats.objects.filter(pk=stats.pk).update(
        rating_avg=avg, rating_count=count
    )
    stats.refresh_from_db()
    return stats


def recompute_product_branch_rating(
    product: Product | int, branch: Branch | int
) -> ProductBranchEngagementStats | None:
    product_id = product if isinstance(product, int) else getattr(product, "pk", None)
    branch_id = branch if isinstance(branch, int) else getattr(branch, "pk", None)
    if not product_id or not branch_id:
        return None
    product_obj = (
        product if isinstance(product, Product) else Product.objects.get(pk=product_id)
    )
    branch_obj = (
        branch if isinstance(branch, Branch) else Branch.objects.get(pk=branch_id)
    )
    stats = ensure_product_branch_engagement_stats(product_obj, branch_obj)
    agg = ProductReview.objects.filter(
        product_id=product_id,
        branch_id=branch_id,
        status__in=PUBLIC_STATUSES,
    ).aggregate(avg=Avg("rating"), count=Count("id"))
    count = int(agg["count"] or 0)
    avg = _quantize_avg(agg["avg"], count)
    ProductBranchEngagementStats.objects.filter(pk=stats.pk).update(
        rating_avg=avg, rating_count=count
    )
    stats.refresh_from_db()
    return stats


def recompute_ratings_for_review(review: ProductReview) -> None:
    recompute_product_rating(review.product_id)
    recompute_business_rating(review.business_id)
    if review.branch_id:
        recompute_branch_rating(review.branch_id)
        recompute_product_branch_rating(review.product_id, review.branch_id)


def branch_rating_payload(branch: Branch | None) -> dict:
    """Serialize branch store rating for catalog/map responses."""
    if branch is None:
        return {"rating_avg": "0.00", "rating_count": 0}
    stats = getattr(branch, "engagement_stats", None)
    if stats is None:
        return {"rating_avg": "0.00", "rating_count": 0}
    return {
        "rating_avg": str(stats.rating_avg or "0.00"),
        "rating_count": int(stats.rating_count or 0),
    }


def can_review_order_item(*, user, order: Order, order_item: OrderItem) -> bool:
    if order.user_id != getattr(user, "id", None):
        return False
    if order.status != Order.Status.COMPLETED:
        return False
    if order_item.order_id != order.id:
        return False
    if not order_item.product_id:
        return False
    return not ProductReview.objects.filter(order_item_id=order_item.id).exists()


def _validate_rating(rating: int) -> int:
    try:
        rating = int(rating)
    except (TypeError, ValueError):
        raise ValidationError({"rating": "Rating must be an integer from 1 to 5."})
    if rating < 1 or rating > 5:
        raise ValidationError({"rating": "Rating must be between 1 and 5."})
    return rating


def _normalize_comment(comment: str | None) -> str:
    text = (comment or "").strip()
    if len(text) > 1000:
        raise ValidationError({"comment": "Comment must be at most 1000 characters."})
    return text


def _attach_images(review: ProductReview, images: list | None, *, replace: bool) -> None:
    if images is None:
        return
    images = [img for img in images if img is not None]
    if len(images) > MAX_REVIEW_IMAGES:
        raise ValidationError(
            {"images": f"You can upload at most {MAX_REVIEW_IMAGES} photos."}
        )
    if replace:
        review.images.all().delete()
        start = 0
    else:
        existing = review.images.count()
        if existing + len(images) > MAX_REVIEW_IMAGES:
            raise ValidationError(
                {
                    "images": (
                        f"You can upload at most {MAX_REVIEW_IMAGES} photos "
                        f"({existing} already attached)."
                    )
                }
            )
        last = (
            review.images.order_by("-sort_order", "-id")
            .values_list("sort_order", flat=True)
            .first()
        )
        start = (last + 1) if last is not None else 0
    for index, image in enumerate(images):
        ProductReviewImage.objects.create(
            review=review, image=image, sort_order=start + index
        )


@transaction.atomic
def create_review(
    *,
    user,
    order: Order,
    order_item: OrderItem,
    rating: int,
    comment: str | None = "",
    images: list | None = None,
) -> ProductReview:
    if order.user_id != user.id:
        raise PermissionDenied("You can only review your own orders.")
    if order.status != Order.Status.COMPLETED:
        raise ValidationError(
            {"detail": "You can only rate products after the order is delivered."}
        )
    if order_item.order_id != order.id:
        raise ValidationError({"detail": "Order item does not belong to this order."})
    if not order_item.product_id:
        raise ValidationError(
            {"detail": "This order item no longer has a product to review."}
        )
    if ProductReview.objects.filter(order_item_id=order_item.id).exists():
        raise ValidationError({"detail": "You have already reviewed this item."})

    rating = _validate_rating(rating)
    comment = _normalize_comment(comment)
    if images is not None and len(images) > MAX_REVIEW_IMAGES:
        raise ValidationError(
            {"images": f"You can upload at most {MAX_REVIEW_IMAGES} photos."}
        )

    review = ProductReview.objects.create(
        user=user,
        business_id=order.business_id,
        branch_id=order.branch_id,
        product_id=order_item.product_id,
        order=order,
        order_item=order_item,
        rating=rating,
        comment=comment,
        status=ProductReview.Status.PUBLISHED,
    )
    _attach_images(review, images, replace=True)
    recompute_ratings_for_review(review)
    return review


def customer_can_edit(review: ProductReview) -> bool:
    if review.status == ProductReview.Status.HIDDEN:
        return False
    deadline = review.created_at + timedelta(days=EDIT_WINDOW_DAYS)
    return timezone.now() <= deadline


@transaction.atomic
def update_review(
    *,
    user,
    review: ProductReview,
    rating: int | None = None,
    comment: str | None = None,
    images: list | None = None,
    replace_images: bool = False,
) -> ProductReview:
    if review.user_id != user.id:
        raise PermissionDenied("You can only edit your own reviews.")
    if not customer_can_edit(review):
        raise ValidationError(
            {
                "detail": (
                    f"Reviews can only be edited within {EDIT_WINDOW_DAYS} days "
                    "of submission."
                )
            }
        )

    update_fields = ["updated_at", "edited_at"]
    if rating is not None:
        review.rating = _validate_rating(rating)
        update_fields.append("rating")
    if comment is not None:
        review.comment = _normalize_comment(comment)
        update_fields.append("comment")
    review.edited_at = timezone.now()
    review.save(update_fields=update_fields)

    if images is not None:
        _attach_images(review, images, replace=replace_images)

    recompute_ratings_for_review(review)
    return review


@transaction.atomic
def merchant_reply(*, business: Business, review: ProductReview, reply: str) -> ProductReview:
    if review.business_id != business.id:
        raise PermissionDenied("This review does not belong to your business.")
    text = (reply or "").strip()
    if not text:
        raise ValidationError({"reply": "Reply cannot be empty."})
    if len(text) > 1000:
        raise ValidationError({"reply": "Reply must be at most 1000 characters."})
    review.merchant_reply = text
    review.merchant_replied_at = timezone.now()
    review.save(update_fields=["merchant_reply", "merchant_replied_at", "updated_at"])
    return review


@transaction.atomic
def merchant_flag(
    *, business: Business, review: ProductReview, reason: str = ""
) -> ProductReview:
    if review.business_id != business.id:
        raise PermissionDenied("This review does not belong to your business.")
    if review.status == ProductReview.Status.HIDDEN:
        raise ValidationError({"detail": "This review is already hidden by admin."})
    text = (reason or "").strip()
    if len(text) > 500:
        raise ValidationError({"reason": "Flag reason must be at most 500 characters."})
    review.status = ProductReview.Status.FLAGGED
    review.flagged_at = timezone.now()
    review.flag_reason = text
    review.save(
        update_fields=["status", "flagged_at", "flag_reason", "updated_at"]
    )
    # Flagged stays publicly visible — aggregates unchanged.
    return review


@transaction.atomic
def admin_hide_review(review: ProductReview) -> ProductReview:
    review.status = ProductReview.Status.HIDDEN
    review.save(update_fields=["status", "updated_at"])
    recompute_ratings_for_review(review)
    return review


@transaction.atomic
def admin_restore_review(review: ProductReview) -> ProductReview:
    review.status = ProductReview.Status.PUBLISHED
    review.flagged_at = None
    review.flag_reason = ""
    review.save(
        update_fields=["status", "flagged_at", "flag_reason", "updated_at"]
    )
    recompute_ratings_for_review(review)
    return review


@transaction.atomic
def admin_dismiss_flag(review: ProductReview) -> ProductReview:
    if review.status != ProductReview.Status.FLAGGED:
        raise ValidationError({"detail": "Review is not flagged."})
    review.status = ProductReview.Status.PUBLISHED
    review.flagged_at = None
    review.flag_reason = ""
    review.save(
        update_fields=["status", "flagged_at", "flag_reason", "updated_at"]
    )
    return review


def public_reviews_qs():
    return ProductReview.objects.filter(status__in=PUBLIC_STATUSES).select_related(
        "user", "product", "business", "branch", "order", "order_item"
    ).prefetch_related("images")


def parse_optional_branch_id(raw) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None
