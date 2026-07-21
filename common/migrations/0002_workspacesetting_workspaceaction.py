import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("common", "0001_initial"),
        ("tenancy", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="WorkspaceSetting",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("version", models.PositiveIntegerField(default=1)),
                ("archived_at", models.DateTimeField(blank=True, null=True)),
                ("namespace", models.CharField(max_length=100)),
                ("value", models.JSONField(blank=True, default=dict)),
                ("created_by", models.ForeignKey(blank=True, editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("owner", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("tenant", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="+", to="tenancy.organization")),
                ("updated_by", models.ForeignKey(blank=True, editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={"db_table": "common_workspace_setting"},
        ),
        migrations.CreateModel(
            name="WorkspaceAction",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("version", models.PositiveIntegerField(default=1)),
                ("archived_at", models.DateTimeField(blank=True, null=True)),
                ("action", models.CharField(max_length=100)),
                ("resource_type", models.CharField(blank=True, max_length=100)),
                ("resource_id", models.CharField(blank=True, max_length=64)),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("status", models.CharField(default="completed", max_length=16)),
                ("result", models.JSONField(blank=True, default=dict)),
                ("created_by", models.ForeignKey(blank=True, editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("tenant", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="+", to="tenancy.organization")),
                ("updated_by", models.ForeignKey(blank=True, editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={"db_table": "common_workspace_action"},
        ),
        migrations.AddConstraint(model_name="workspacesetting", constraint=models.UniqueConstraint(fields=("tenant", "namespace", "owner"), name="uniq_workspace_setting_scope")),
        migrations.AddIndex(model_name="workspacesetting", index=models.Index(fields=["tenant", "namespace"], name="common_work_tenant__b4f74c_idx")),
        migrations.AddIndex(model_name="workspaceaction", index=models.Index(fields=["tenant", "action", "-created_at"], name="common_work_tenant__d501e7_idx")),
        migrations.AddIndex(model_name="workspaceaction", index=models.Index(fields=["tenant", "resource_type", "resource_id"], name="common_work_tenant__7c2f59_idx")),
    ]
