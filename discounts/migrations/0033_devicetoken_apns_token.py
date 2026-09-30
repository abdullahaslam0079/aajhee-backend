# Generated manually for DeviceToken.apns_token

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("discounts", "0032_categories_canonicalize"),
    ]

    operations = [
        migrations.AddField(
            model_name="devicetoken",
            name="apns_token",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
    ]
