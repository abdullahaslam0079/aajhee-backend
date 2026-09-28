# Generated manually for checkout / order flow improvements

from decimal import Decimal

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("discounts", "0025_fulfillment_stripe_jazzcash"),
    ]

    operations = [
        migrations.AddField(
            model_name="address",
            name="landmark",
            field=models.CharField(blank=True, default="", max_length=160),
        ),
        migrations.AddField(
            model_name="order",
            name="customer_phone",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Normalized Pakistani mobile (+923…) at place time.",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="order",
            name="delivery_house_number",
            field=models.CharField(blank=True, default="", max_length=40),
        ),
        migrations.AddField(
            model_name="order",
            name="delivery_landmark",
            field=models.CharField(blank=True, default="", max_length=160),
        ),
        migrations.AddField(
            model_name="order",
            name="payment_status",
            field=models.CharField(
                choices=[
                    ("unpaid", "Unpaid"),
                    ("awaiting_confirmation", "Awaiting confirmation"),
                    ("paid", "Paid"),
                ],
                default="unpaid",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="branchfulfillmentsettings",
            name="nationwide_delivery_fee",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("300.00"),
                help_text="Standard / nationwide delivery fee in PKR (default Rs 300).",
                max_digits=10,
            ),
        ),
        migrations.AlterField(
            model_name="notification",
            name="type",
            field=models.CharField(
                choices=[
                    ("favorited_business_new_offer", "Favorited business new offer"),
                    ("offer_expiring_soon", "Offer expiring soon"),
                    ("redemption_confirmation", "Redemption confirmation"),
                    ("business_new_order", "Business new order"),
                    ("business_payment_proof", "Business payment proof submitted"),
                    ("order_status_changed", "Order status changed"),
                    ("generic", "Generic"),
                ],
                default="generic",
                max_length=64,
            ),
        ),
        migrations.AlterField(
            model_name="order",
            name="fulfillment_type",
            field=models.CharField(
                choices=[
                    ("pickup", "In-store pickup"),
                    ("local_same_day", "Same-day delivery"),
                    ("nationwide", "Nationwide / standard delivery"),
                ],
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="order",
            name="payment_method",
            field=models.CharField(
                choices=[
                    ("cash_on_pickup", "Cash on pickup"),
                    ("cash_on_delivery", "Cash on delivery"),
                    ("bank_transfer", "Bank transfer"),
                    ("stripe", "Stripe"),
                    ("jazzcash", "Mobile wallet (JazzCash / Easypaisa)"),
                ],
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="order",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("accepted", "Accepted"),
                    ("cancelled", "Cancelled"),
                    ("awaiting_payment", "Awaiting payment"),
                    ("payment_submitted", "Payment submitted"),
                    ("paid_confirmed", "Payment confirmed"),
                    ("preparing", "Preparing"),
                    ("ready_for_pickup", "Ready for pickup"),
                    ("out_for_delivery", "Out for delivery"),
                    ("completed", "Delivered"),
                ],
                default="pending",
                max_length=32,
            ),
        ),
        migrations.CreateModel(
            name="OrderProblemReport",
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
                ("message", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "order",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="problem_reports",
                        to="discounts.order",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="order_problem_reports",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at", "-id"],
            },
        ),
    ]
