from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("documents", "0004_alter_documentversion_file"),
    ]

    operations = [
        migrations.AddField(
            model_name="receiptdraft",
            name="fee_breakdown",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="receiptdraft",
            name="tax_breakdown",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
