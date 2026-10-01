"""Helpers for marketplace category tree filtering and validation."""

from __future__ import annotations

from django.db.models import Q
from rest_framework import serializers

from .models import Category

# Primary + up to 3 secondary business verticals.
MAX_BUSINESS_CATEGORIES = 4


def business_vertical_q(category_id: int | str, *, business_prefix: str = "business") -> Q:
    """
    Match businesses whose primary FK or M2M verticals include category_id.

    business_prefix:
      - \"business\" for Branch/Offer querysets
      - \"\" for Business querysets (uses bare field names)
    """
    cid = int(category_id)
    if business_prefix:
        primary = f"{business_prefix}__category_id"
        m2m = f"{business_prefix}__categories__id"
    else:
        primary = "category_id"
        m2m = "categories__id"
    return Q(**{primary: cid}) | Q(**{m2m: cid})


def product_category_q(category_id: int | str) -> Q:
    """Match products in category_id or any descendant."""
    ids = Category.descendant_ids(int(category_id), include_self=True)
    return Q(category_id__in=ids)


def ensure_root_category(category: Category, *, field: str = "category_id") -> Category:
    if category.parent_id is not None:
        raise serializers.ValidationError(
            {field: "Business category must be a top-level (root) category."}
        )
    if not category.is_active:
        raise serializers.ValidationError({field: "Selected category is inactive."})
    return category


def validate_business_root_categories(
    categories: list[Category],
    *,
    field: str = "category_ids",
) -> list[Category]:
    if len(categories) > MAX_BUSINESS_CATEGORIES:
        raise serializers.ValidationError(
            {
                field: (
                    f"Select at most {MAX_BUSINESS_CATEGORIES} business categories "
                    "(1 primary + up to 3 secondary)."
                )
            }
        )
    seen: set[int] = set()
    unique: list[Category] = []
    for category in categories:
        ensure_root_category(category, field=field)
        if category.id in seen:
            continue
        seen.add(category.id)
        unique.append(category)
    return unique


def sync_business_categories(
    business,
    *,
    primary: Category | None = None,
    categories: list[Category] | None = None,
) -> None:
    """
    Keep Business.category (primary) and Business.categories (M2M) in sync.

    - categories (M2M) always includes primary
    - if primary missing but M2M set, primary becomes first M2M entry
    - M2M capped at MAX_BUSINESS_CATEGORIES roots
    """
    if categories is not None:
        categories = validate_business_root_categories(categories)
        if primary is None:
            primary = business.category
        if primary is not None and primary.parent_id is None and primary.is_active:
            if primary not in categories:
                categories = [primary, *categories][:MAX_BUSINESS_CATEGORIES]
        elif categories:
            primary = categories[0]
        business.categories.set(categories)
        if primary is not None and business.category_id != primary.id:
            business.category = primary
            business.save(update_fields=["category"])
        return

    if primary is not None:
        ensure_root_category(primary)
        business.category = primary
        business.save(update_fields=["category"])
        ids = list(business.categories.values_list("id", flat=True))
        if primary.id not in ids:
            current = list(business.categories.all())
            merged = [primary, *[c for c in current if c.id != primary.id]]
            business.categories.set(merged[:MAX_BUSINESS_CATEGORIES])
        elif not ids:
            business.categories.add(primary)
