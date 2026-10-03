# Marketplace branch-scoped ratings: per-location store + product@branch aggregates.

from decimal import Decimal

import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Avg, Count


PUBLIC_STATUSES = ("published", "flagged")


def backfill_review_branches_and_aggregates(apps, schema_editor):
    ProductReview = apps.get_model("discounts", "ProductReview")
    BranchEngagementStats = apps.get_model("discounts", "BranchEngagementStats")
    ProductBranchEngagementStats = apps.get_model(
        "discounts", "ProductBranchEngagementStats"
    )

    # Copy fulfillment branch from the order onto each review.
    for review in ProductReview.objects.select_related("order").iterator():
        order_branch_id = getattr(review.order, "branch_id", None)
        if review.branch_id != order_branch_id:
            review.branch_id = order_branch_id
            review.save(update_fields=["branch_id"])

    def _avg_count(qs):
        agg = qs.aggregate(avg=Avg("rating"), count=Count("id"))
        count = int(agg["count"] or 0)
        avg = (
            Decimal(str(agg["avg"] or 0)).quantize(Decimal("0.01"))
            if count
            else Decimal("0.00")
        )
        return avg, count

    branch_ids = (
        ProductReview.objects.filter(branch_id__isnull=False)
        .values_list("branch_id", flat=True)
        .distinct()
    )
    for branch_id in branch_ids:
        avg, count = _avg_count(
            ProductReview.objects.filter(
                branch_id=branch_id, status__in=PUBLIC_STATUSES
            )
        )
        stats, _ = BranchEngagementStats.objects.get_or_create(branch_id=branch_id)
        BranchEngagementStats.objects.filter(pk=stats.pk).update(
            rating_avg=avg, rating_count=count
        )

    pairs = (
        ProductReview.objects.filter(branch_id__isnull=False)
        .values_list("product_id", "branch_id")
        .distinct()
    )
    for product_id, branch_id in pairs:
        avg, count = _avg_count(
            ProductReview.objects.filter(
                product_id=product_id,
                branch_id=branch_id,
                status__in=PUBLIC_STATUSES,
            )
        )
        stats, _ = ProductBranchEngagementStats.objects.get_or_create(
            product_id=product_id, branch_id=branch_id
        )
        ProductBranchEngagementStats.objects.filter(pk=stats.pk).update(
            rating_avg=avg, rating_count=count
        )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("discounts", "0035_seed_l2_categories"),
    ]

    operations = [
        migrations.CreateModel(
            name="BranchEngagementStats",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "rating_avg",
                    models.DecimalField(
                        decimal_places=2, default=Decimal("0.00"), max_digits=3
                    ),
                ),
                ("rating_count", models.PositiveIntegerField(default=0)),
                (
                    "branch",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="engagement_stats",
                        to="discounts.branch",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="ProductBranchEngagementStats",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "rating_avg",
                    models.DecimalField(
                        decimal_places=2, default=Decimal("0.00"), max_digits=3
                    ),
                ),
                ("rating_count", models.PositiveIntegerField(default=0)),
                (
                    "branch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="product_engagement_stats",
                        to="discounts.branch",
                    ),
                ),
                (
                    "product",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="branch_engagement_stats",
                        to="discounts.product",
                    ),
                ),
            ],
            options={
                "verbose_name_plural": "product branch engagement stats",
            },
        ),
        migrations.AddField(
            model_name="productreview",
            name="branch",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="product_reviews",
                to="discounts.branch",
            ),
        ),
        migrations.AddIndex(
            model_name="productreview",
            index=models.Index(
                fields=["branch", "status", "-created_at"],
                name="discounts_p_branch__8f1a2c_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="productreview",
            index=models.Index(
                fields=["product", "branch", "status"],
                name="discounts_p_product_b7e4d1_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="productbranchengagementstats",
            constraint=models.UniqueConstraint(
                fields=("product", "branch"),
                name="unique_product_branch_engagement_stats",
            ),
        ),
        migrations.RunPython(backfill_review_branches_and_aggregates, noop_reverse),
    ]
