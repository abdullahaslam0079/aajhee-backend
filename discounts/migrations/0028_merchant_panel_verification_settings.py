# Merchant verification, pause/hours, same-day areas, categories seed.
#
# Note: verification_status is added WITHOUT db_index first, then data is updated,
# then the index is created. Postgres rejects CREATE INDEX in the same transaction
# as UPDATEs on the same table ("pending trigger events").

from django.db import migrations, models
from django.utils.text import slugify


MERCHANT_CATEGORIES = [
    ("Grocery & Food", "grocery-food", 10),
    ("Electronics", "electronics", 20),
    ("Fashion", "fashion", 30),
    ("Home", "home", 40),
    ("Beauty", "beauty", 50),
    ("Gifts", "gifts", 60),
    ("Other", "other", 70),
]


def seed_merchant_categories(apps, schema_editor):
    Category = apps.get_model("discounts", "Category")
    Business = apps.get_model("discounts", "Business")
    BusinessCategory = apps.get_model("discounts", "BusinessCategory")
    Product = apps.get_model("discounts", "Product")

    # Rename legacy "Food" root to Grocery & Food when present.
    food = Category.objects.filter(parent__isnull=True, name__iexact="Food").first()
    if food:
        food.name = "Grocery & Food"
        food.slug = "grocery-food"
        food.sort_order = 10
        food.is_active = True
        food.save(update_fields=["name", "slug", "sort_order", "is_active"])

    slug_to_id = {}
    for name, slug, sort_order in MERCHANT_CATEGORIES:
        cat = Category.objects.filter(parent__isnull=True, slug=slug).first()
        if cat is None:
            cat = Category.objects.filter(parent__isnull=True, name__iexact=name).first()
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

    other_id = slug_to_id.get("other")
    if not other_id:
        return

    known_ids = set(slug_to_id.values())
    # Remap businesses / products pointing at unknown root categories to Other.
    for business in Business.objects.exclude(category_id__isnull=True).exclude(
        category_id__in=known_ids
    ):
        old_id = business.category_id
        business.category_id = other_id
        business.save(update_fields=["category_id"])
        BusinessCategory.objects.filter(business_id=business.id, category_id=old_id).delete()
        BusinessCategory.objects.get_or_create(
            business_id=business.id, category_id=other_id
        )

    Product.objects.exclude(category_id__isnull=True).exclude(
        category_id__in=known_ids
    ).exclude(category__parent_id__in=known_ids).update(category_id=other_id)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("discounts", "0027_rename_same_day_fulfillment_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="business",
            name="verification_status",
            field=models.CharField(
                choices=[
                    ("under_review", "Under review"),
                    ("verified", "Verified"),
                    ("suspended", "Suspended"),
                ],
                default="under_review",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="business",
            name="phone",
            field=models.CharField(blank=True, max_length=40),
        ),
        migrations.AddField(
            model_name="business",
            name="instagram_url",
            field=models.URLField(blank=True, max_length=300),
        ),
        migrations.AddField(
            model_name="business",
            name="cnic_image",
            field=models.ImageField(
                blank=True, null=True, upload_to="business_cnic/"
            ),
        ),
        migrations.AddField(
            model_name="business",
            name="shop_photo",
            field=models.ImageField(
                blank=True, null=True, upload_to="business_shop_photos/"
            ),
        ),
        migrations.AddField(
            model_name="business",
            name="notification_whatsapp",
            field=models.CharField(
                blank=True,
                help_text="WhatsApp/SMS number for order alerts (provider optional).",
                max_length=40,
            ),
        ),
        migrations.AddField(
            model_name="business",
            name="is_paused",
            field=models.BooleanField(
                default=False,
                help_text="When true, hide the store from customers without deleting it.",
            ),
        ),
        migrations.AddField(
            model_name="business",
            name="business_hours",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text='Weekly hours, e.g. {"mon": {"open": "10:00", "close": "22:00", "closed": false}}',
            ),
        ),
        migrations.AddField(
            model_name="branchfulfillmentsettings",
            name="same_day_radius_km",
            field=models.DecimalField(
                decimal_places=2,
                default=15.0,
                help_text="Same-day delivery radius in km (e.g. within Lahore).",
                max_digits=6,
            ),
        ),
        migrations.AddField(
            model_name="branchfulfillmentsettings",
            name="same_day_areas",
            field=models.TextField(
                blank=True,
                help_text="Comma-separated delivery areas, e.g. DHA, Gulberg, Johar Town.",
            ),
        ),
        migrations.AddField(
            model_name="branchfulfillmentsettings",
            name="easypaisa_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="branchfulfillmentsettings",
            name="easypaisa_instructions",
            field=models.TextField(blank=True),
        ),
        # Existing merchants stay visible to customers after this rollout.
        migrations.RunSQL(
            sql="UPDATE discounts_business SET verification_status = 'verified';",
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RunPython(seed_merchant_categories, noop_reverse),
        # Create the index only after UPDATEs so Postgres does not hit
        # "cannot CREATE INDEX ... because it has pending trigger events".
        migrations.AlterField(
            model_name="business",
            name="verification_status",
            field=models.CharField(
                choices=[
                    ("under_review", "Under review"),
                    ("verified", "Verified"),
                    ("suspended", "Suspended"),
                ],
                db_index=True,
                default="under_review",
                max_length=20,
            ),
        ),
    ]
