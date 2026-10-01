# Generated manually for UserPreferences.marketing_notifications_enabled

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("discounts", "0033_devicetoken_apns_token"),
    ]

    operations = [
        migrations.AddField(
            model_name="userpreferences",
            name="marketing_notifications_enabled",
            field=models.BooleanField(default=True),
        ),
    ]
