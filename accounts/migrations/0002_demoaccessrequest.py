import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0001_initial")]

    operations = [
        migrations.CreateModel(
            name="DemoAccessRequest",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=150)),
                ("company", models.CharField(max_length=255)),
                ("email", models.EmailField(max_length=254)),
                ("phone", models.CharField(blank=True, max_length=32)),
                ("status", models.CharField(choices=[("new", "New"), ("contacted", "Contacted"), ("activated", "Activated"), ("closed", "Closed")], default="new", max_length=12)),
                ("source_ip", models.GenericIPAddressField(blank=True, null=True)),
                ("user_agent", models.CharField(blank=True, max_length=512)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "db_table": "accounts_demo_access_request",
                "indexes": [models.Index(fields=["status", "-created_at"], name="accounts_de_status_37ce89_idx"), models.Index(fields=["email"], name="accounts_de_email_3f33cd_idx")],
            },
        )
    ]
