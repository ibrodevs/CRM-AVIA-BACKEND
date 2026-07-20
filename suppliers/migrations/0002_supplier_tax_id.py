from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("suppliers", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="supplier",
            name="tax_id",
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.AddIndex(
            model_name="supplier",
            index=models.Index(fields=["tenant", "tax_id"], name="suppliers_s_tenant__dc1df9_idx"),
        ),
    ]
