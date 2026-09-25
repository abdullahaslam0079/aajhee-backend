from decimal import Decimal

from django.db import migrations
from django.utils.text import slugify


def forwards(apps, schema_editor):
    Country = apps.get_model("discounts", "Country")
    City = apps.get_model("discounts", "City")
    Business = apps.get_model("discounts", "Business")
    BusinessCategory = apps.get_model("discounts", "BusinessCategory")
    Branch = apps.get_model("discounts", "Branch")
    Address = apps.get_model("discounts", "Address")
    Offer = apps.get_model("discounts", "Offer")
    Product = apps.get_model("discounts", "Product")
    ProductEngagementStats = apps.get_model("discounts", "ProductEngagementStats")
    BranchFulfillmentSettings = apps.get_model("discounts", "BranchFulfillmentSettings")
    Category = apps.get_model("discounts", "Category")

    country, _ = Country.objects.get_or_create(
        code="PK", defaults={"name": "Pakistan"}
    )

    def normalize(name: str) -> str:
        return (name or "").strip().casefold()

    city_cache: dict[str, object] = {}

    def ensure_city(name: str):
        key = normalize(name)
        if not key:
            return None
        if key in city_cache:
            return city_cache[key]
        city, _ = City.objects.get_or_create(
            country=country,
            name_normalized=key,
            defaults={"name": name.strip()},
        )
        city_cache[key] = city
        return city

    for category in Category.objects.filter(slug=""):
        category.slug = slugify(category.name)[:100] or f"cat-{category.id}"
        category.save(update_fields=["slug"])

    for business in Business.objects.all():
        if business.category_id:
            BusinessCategory.objects.get_or_create(
                business=business, category_id=business.category_id
            )
        # Infer primary city from first branch
        branch = Branch.objects.filter(business=business).order_by("id").first()
        if branch:
            city = ensure_city(branch.city)
            if city:
                business.primary_city_id = city.id
                business.primary_country_id = country.id
                business.save(update_fields=["primary_city_id", "primary_country_id"])

    for branch in Branch.objects.all():
        city = ensure_city(branch.city)
        if city and not branch.city_ref_id:
            branch.city_ref_id = city.id
            branch.save(update_fields=["city_ref_id"])
        BranchFulfillmentSettings.objects.get_or_create(branch=branch)

    for address in Address.objects.all():
        city = ensure_city(address.city)
        if city and not address.city_ref_id:
            address.city_ref_id = city.id
            address.save(update_fields=["city_ref_id"])

    for offer in Offer.objects.select_related("business").all():
        if Product.objects.filter(source_offer_id=offer.id).exists():
            continue
        category_id = offer.business.category_id
        if not category_id:
            # Skip if business has no category
            link = BusinessCategory.objects.filter(business_id=offer.business_id).first()
            if not link:
                continue
            category_id = link.category_id

        base_price = offer.original_price or offer.discounted_price or Decimal("0.00")
        if base_price <= 0:
            base_price = Decimal("1.00")
        sale_price = offer.discounted_price
        discount_percent = offer.discount_percent
        if sale_price is not None and sale_price >= base_price:
            sale_price = None
            discount_percent = None

        name = (offer.item_name or offer.title or "Product").strip()[:160]
        product = Product.objects.create(
            business_id=offer.business_id,
            category_id=category_id,
            name=name,
            description=offer.description or "",
            detailed_description=offer.detailed_description or "",
            image=offer.image,
            base_price=base_price,
            discount_percent=discount_percent if sale_price is not None else None,
            sale_price=sale_price,
            is_available=offer.is_enabled,
            is_enabled=offer.is_enabled,
            source_offer_id=offer.id,
        )
        branch_ids = list(offer.branches.values_list("id", flat=True))
        if branch_ids:
            product.branches.set(branch_ids)
        ProductEngagementStats.objects.get_or_create(product=product)


def backwards(apps, schema_editor):
    Product = apps.get_model("discounts", "Product")
    Product.objects.filter(source_offer__isnull=False).delete()
    BusinessCategory = apps.get_model("discounts", "BusinessCategory")
    BusinessCategory.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("discounts", "0022_commerce_platform"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
