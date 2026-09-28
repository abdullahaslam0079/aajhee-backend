# Add verification_status index in a separate transaction from 0028 data UPDATEs.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("discounts", "0028_merchant_panel_verification_settings"),
    ]

    operations = [
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
