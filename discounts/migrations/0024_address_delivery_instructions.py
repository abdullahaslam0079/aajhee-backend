from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("discounts", "0023_migrate_offers_to_products"),
    ]

    operations = [
        migrations.AddField(
            model_name="address",
            name="delivery_instructions",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AlterField(
            model_name="address",
            name="county",
            field=models.CharField(blank=True, default="", max_length=80),
        ),
    ]
