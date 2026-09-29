# Canonicalize root merchant categories (merge duplicates, rename Home → Home & Living).

from django.db import migrations
from django.utils.text import slugify


CANONICAL_ROOTS = [
    ("Grocery & Food", "grocery-food", 10),
    ("Electronics", "electronics", 20),
    ("Fashion", "fashion", 30),
    ("Home & Living", "home-living", 40),
    ("Beauty", "beauty", 50),
    ("Gifts", "gifts", 60),
    ("Health", "health", 70),
    ("Other", "other", 80),
]

# Extra name/slug aliases that should merge into a canonical slug.
ALIAS_TO_SLUG = {
    "home": "home-living",
    "home-living": "home-living",
    "home & living": "home-living",
    "grocery & food": "grocery-food",
    "grocery-food": "grocery-food",
    "food": "grocery-food",
    "electronics": "electronics",
    "fashion": "fashion",
    "beauty": "beauty",
    "gifts": "gifts",
    "health": "health",
    "other": "other",
}


def _alias_key(name: str = "", slug: str = "") -> str | None:
    for raw in (slug or "", name or ""):
        key = (raw or "").strip().lower()
        if key in ALIAS_TO_SLUG:
            return ALIAS_TO_SLUG[key]
    return None


def canonicalize_categories(apps, schema_editor):
    Category = apps.get_model("discounts", "Category")
    Business = apps.get_model("discounts", "Business")
    BusinessCategory = apps.get_model("discounts", "BusinessCategory")
    Product = apps.get_model("discounts", "Product")

    # Ensure each canonical root exists (prefer slug, then name).
    slug_to_id: dict[str, int] = {}
    for name, slug, sort_order in CANONICAL_ROOTS:
        cat = Category.objects.filter(parent__isnull=True, slug=slug).first()
        if cat is None:
            cat = Category.objects.filter(parent__isnull=True, name__iexact=name).first()
        # Prefer renaming legacy "Home" root into Home & Living.
        if cat is None and slug == "home-living":
            cat = Category.objects.filter(parent__isnull=True, slug="home").first()
            if cat is None:
                cat = Category.objects.filter(
                    parent__isnull=True, name__iexact="Home"
                ).first()
        if cat is None:
            cat = Category.objects.create(
                name=name,
                slug=slug or slugify(name),
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
        slug_to_id[slug] = cat.id

    canonical_ids = set(slug_to_id.values())

    def reassign_category(old_id: int, new_id: int) -> None:
        if old_id == new_id:
            return
        Business.objects.filter(category_id=old_id).update(category_id=new_id)
        for link in BusinessCategory.objects.filter(category_id=old_id):
            BusinessCategory.objects.get_or_create(
                business_id=link.business_id, category_id=new_id
            )
            link.delete()
        if hasattr(Product, "category_id"):
            Product.objects.filter(category_id=old_id).update(category_id=new_id)
        # Re-parent children under the canonical root when the duplicate is a root.
        Category.objects.filter(parent_id=old_id).update(parent_id=new_id)

    # Merge any other root categories that match aliases or duplicate names/slugs.
    for cat in list(Category.objects.filter(parent__isnull=True)):
        if cat.id in canonical_ids:
            continue
        target_slug = _alias_key(cat.name, cat.slug)
        if target_slug and target_slug in slug_to_id:
            reassign_category(cat.id, slug_to_id[target_slug])
            # Drop empty duplicate root (children already reparented).
            if not Category.objects.filter(parent_id=cat.id).exists():
                cat.delete()
            continue
        # Unknown roots → Other
        reassign_category(cat.id, slug_to_id["other"])
        if not Category.objects.filter(parent_id=cat.id).exists():
            cat.delete()

    # Remap businesses / products still pointing at non-canonical roots.
    other_id = slug_to_id["other"]
    for business in Business.objects.exclude(category_id__isnull=True).exclude(
        category_id__in=canonical_ids
    ):
        old_id = business.category_id
        # Keep child categories; only remap if primary points at a deleted/unknown root.
        parent_id = (
            Category.objects.filter(pk=old_id)
            .values_list("parent_id", flat=True)
            .first()
        )
        if parent_id is None or parent_id not in canonical_ids:
            # If it's a child of a canonical root, leave it; else → Other.
            if parent_id is not None and parent_id in canonical_ids:
                continue
            reassign_category(old_id, other_id)

    Product.objects.exclude(category_id__isnull=True).exclude(
        category_id__in=canonical_ids
    ).exclude(category__parent_id__in=canonical_ids).update(category_id=other_id)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("discounts", "0031_admin_trust_ops"),
    ]

    operations = [
        migrations.RunPython(canonicalize_categories, noop_reverse),
    ]
