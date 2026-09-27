from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("discounts", "0024_address_delivery_instructions"),
    ]

    operations = [
        migrations.AddField(
            model_name="branchfulfillmentsettings",
            name="jazzcash_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="branchfulfillmentsettings",
            name="jazzcash_instructions",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="branchfulfillmentsettings",
            name="stripe_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="branchfulfillmentsettings",
            name="stripe_instructions",
            field=models.TextField(blank=True),
        ),
    ]
