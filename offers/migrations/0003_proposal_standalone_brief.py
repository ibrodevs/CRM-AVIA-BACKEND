from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("offers", "0002_proposalitem_service_kind"),
    ]

    operations = [
        migrations.AlterField(
            model_name="proposal",
            name="order",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="proposals",
                to="orders.order",
            ),
        ),
        migrations.AddField(
            model_name="proposal",
            name="source",
            field=models.CharField(default="manual", max_length=16),
        ),
        migrations.AddField(
            model_name="proposal",
            name="source_text",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="proposal",
            name="recipient",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="proposal",
            name="payment_terms",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="proposal",
            name="brief",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
