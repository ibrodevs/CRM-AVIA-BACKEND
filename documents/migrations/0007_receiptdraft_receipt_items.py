from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("documents", "0006_receiptdraft_fare_breakdown"),
    ]

    operations = [
        migrations.AddField(
            model_name="receiptdraft",
            name="receipt_items",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
