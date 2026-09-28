# Rename same-day fulfillment settings to city-neutral field names.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("discounts", "0026_checkout_order_flow_v2"),
    ]

    operations = [
        migrations.RenameField(
            model_name="branchfulfillmentsettings",
            old_name="local_same_day_enabled",
            new_name="same_day_enabled",
        ),
        migrations.RenameField(
            model_name="branchfulfillmentsettings",
            old_name="local_delivery_fee",
            new_name="same_day_fee",
        ),
        migrations.RenameField(
            model_name="branchfulfillmentsettings",
            old_name="local_max_delivery_hours",
            new_name="same_day_max_delivery_hours",
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
    ]
