from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("documents", "0005_receiptdraft_breakdowns"),
    ]

    operations = [
        migrations.AddField(
            model_name="receiptdraft",
            name="fare_breakdown",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
