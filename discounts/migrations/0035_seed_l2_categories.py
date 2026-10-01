# Generated manually for marketplace L2 category tree.

from django.db import migrations


def seed_l2_and_remap(apps, schema_editor):
    Category = apps.get_model("discounts", "Category")
    Product = apps.get_model("discounts", "Product")

    from discounts.seed_utils import (
        L2_CATEGORIES,
        ROOT_CATEGORIES,
        ensure_l2_categories,
        resolve_product_l2_slug,
    )

    # Ensure canonical roots exist (idempotent).
    for name, slug, sort_order in ROOT_CATEGORIES:
        cat = Category.objects.filter(parent__isnull=True, slug=slug).first()
        if cat is None:
            cat = Category.objects.filter(parent__isnull=True, name__iexact=name).first()
        if cat is None:
            Category.objects.create(
                name=name,
                slug=slug,
                parent=None,
                sort_order=sort_order,
                is_active=True,
            )
        else:
            cat.name = name
            cat.slug = slug
            cat.sort_order = sort_order
            cat.is_active = True
            cat.save(update_fields=["name", "slug", "sort_order", "is_active"])

    l2_by_slug = ensure_l2_categories(Category)

    # Remap products that still sit on a root when an L2 hint matches.
    root_ids = set(
        Category.objects.filter(parent__isnull=True).values_list("id", flat=True)
    )
    for product in Product.objects.select_related("category").all().iterator():
        if product.category_id not in root_ids:
            continue
        hint = resolve_product_l2_slug(product.name)
        if not hint:
            continue
        target = l2_by_slug.get(hint)
        if target is None:
            continue
        # Only remap into a child of the product's current root when possible.
        if (
            product.category_id
            and target.parent_id
            and target.parent_id != product.category_id
        ):
            # Allow remap if product has no category root match constraint —
            # keep same-root remaps only.
            continue
        product.category_id = target.id
        product.save(update_fields=["category_id"])


def noop_reverse(apps, schema_editor):
    # Keep L2 nodes; safe no-op for reverse.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("discounts", "0034_userpreferences_marketing_notifications_enabled"),
    ]

    operations = [
        migrations.RunPython(seed_l2_and_remap, noop_reverse),
    ]
